"""The tracer: wraps pandas so every operation leaves a lineage record.

Three rules the implementation follows:
  1. Never change what the user's code returns. We observe, then hand back the
     original object untouched.
  2. Never raise. If lineage cannot be derived, degrade to an approximate
     record and keep going -- a debugging tool must not break the pipeline.
  3. Record only user-visible operations. pandas methods call each other
     constantly; a depth guard keeps the inner calls out of the trace.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
import threading
import time
import uuid
import warnings
import weakref
from pathlib import Path

import numpy as np

from . import _lineage as lin
from ._graph import FrameNode, Step
from ._store import Store

_ACTIVE: Tracer | None = None

# Guards every transition of _ACTIVE and _PATCHES together, so the "one trace
# at a time" rule and the patch state can never disagree.
#
# Reading _ACTIVE is deliberately left unlocked: _active_here() runs on every
# patched pandas call, and taking a lock there would serialise the whole
# process. A lone reference read is safe -- the reader either sees the tracer
# or does not, and the thread-ownership check decides the rest.
#
# Reentrant because stop() calls uninstall() while already holding it, and
# both are reachable on their own.
_LOCK = threading.RLock()

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _pandas_dir() -> str:
    """Where pandas itself is installed.

    Frames are attributed to pandas by comparing against this directory, never
    by looking for "pandas" in the path: a user working in ~/pandas-tutorial,
    or in an unpacked pandas_blame sdist, would have every operation silently
    attributed to pandas internals and nothing would be recorded at all.
    """
    import pandas

    return os.path.dirname(os.path.abspath(pandas.__file__))


_PANDAS_DIR = _pandas_dir()
_LPOS = "__blame_lpos__"
_RPOS = "__blame_rpos__"
_POS = "__blame_pos__"


def active() -> Tracer | None:
    return _ACTIVE


def _active_here() -> Tracer | None:
    """The running tracer, but only for the thread that opened it.

    The patches are global, so a pandas call on an unrelated thread reaches
    them too. Recording it would put operations the user never ran into the
    pipeline, and add source frames it never read. Other threads see no active
    tracer and pass straight through to pandas.
    """
    t = _ACTIVE
    if t is None or t.thread != threading.get_ident():
        return None
    return t


def _caller_loc() -> str:
    frame = inspect.currentframe()
    while frame is not None:
        fname = frame.f_code.co_filename
        if not fname.startswith((_PKG_DIR, _PANDAS_DIR)):
            return f"{Path(fname).name}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


def _guess_name(obj) -> str | None:
    """Find the user's variable name for a frame that entered from outside."""
    frame = inspect.currentframe()
    while frame is not None:
        fname = frame.f_code.co_filename
        if not fname.startswith((_PKG_DIR, _PANDAS_DIR)):
            for scope in (frame.f_locals, frame.f_globals):
                for name, value in list(scope.items()):
                    if value is obj and not name.startswith("_"):
                        return name
        frame = frame.f_back
    return None


def _short(value, limit: int = 40) -> str:
    try:
        text = repr(value)
    except Exception:
        text = "<unrepr>"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _detail(args: tuple, kwargs: dict) -> str:
    import pandas as pd

    parts = []
    for a in args:
        parts.append("<frame>" if isinstance(a, (pd.DataFrame, pd.Series)) else _short(a))
    for k, v in kwargs.items():
        if v is None or (isinstance(v, bool) and v is False):
            continue
        parts.append(f"{k}={'<frame>' if isinstance(v, (pd.DataFrame, pd.Series)) else _short(v)}")
    return ", ".join(parts)


class Tracer:
    def __init__(self, store: Store, run_id: str, label: str = ""):
        self.store = store
        self.run_id = run_id
        self.label = label
        self.nodes: dict[str, FrameNode] = {}
        self.steps: list[Step] = []
        self.lineages: dict[int, lin.Lineage] = {}
        self.warnings: list[str] = []
        self._obj_to_fid: dict[int, str] = {}
        self._refs: dict[int, weakref.ref] = {}
        self._gb_parent: dict[int, str] = {}
        self._derived: dict[int, tuple] = {}
        self._depth = 0
        self._n = 0
        # Patching pandas is process-wide, but a trace describes one pipeline.
        # Work arriving from any other thread is somebody else's and is ignored.
        self.thread = threading.get_ident()
        self.started = time.time()
        self.capture_seconds = 0.0

    # -- frame registry --------------------------------------------------

    def _new_fid(self) -> str:
        self._n += 1
        return f"f{self._n - 1}"

    def _forget(self, key: int):
        self._obj_to_fid.pop(key, None)
        self._refs.pop(key, None)

    def note_derived(self, new, other, method) -> None:
        """pandas telling us one frame came from another, during an operation
        we do not trace.

        The origin is resolved now, not later: pandas discards the frames it
        builds on the way through an operation the moment it is done with them,
        so a chain of weak references to those intermediates is already broken
        by the time anyone asks. Each link instead carries the fid it descends
        from, so the chain collapses as it is built.
        """
        name = str(method or "?")
        fid = self._live_fid(other)
        if fid is not None:
            methods = [name]
        else:
            prior = self._derived.get(id(other))
            if prior is None:
                return  # nothing upstream that we know about
            fid, earlier = prior[0], prior[1]
            methods = earlier if earlier and earlier[-1] == name else [*earlier, name]
            if len(methods) > 8:  # a long internal chain tells us nothing
                methods = methods[:8]
        key = id(new)
        try:
            keeper = weakref.ref(new, lambda _r, k=key: self._derived.pop(k, None))  # type: ignore[misc]
        except TypeError:
            return
        self._derived[key] = (fid, methods, keeper)

    def _live_fid(self, obj) -> str | None:
        fid = self._obj_to_fid.get(id(obj))
        if fid is None:
            return None
        ref = self._refs.get(id(obj))
        return fid if ref is None or ref() is not None else None

    def _untraced_origin(self, obj):
        """The registered frame this one descends from, and how it got here."""
        entry = self._derived.get(id(obj))
        if entry is None:
            return None
        fid, methods, _keeper = entry
        return (fid, methods) if fid in self.nodes else None

    def _record_untraced(self, obj, ancestor_fid: str, methods: list[str]) -> str:
        """Insert an explicit approximate hop rather than letting an
        intermediate frame masquerade as a source."""
        name = methods[-1] if methods else "?"
        self.warn(
            f"{name}: not traced, so lineage through it is approximate "
            f"(every row of the input is reported as a candidate)"
        )
        self.record(
            f"<untraced {name}>",
            [ancestor_fid],
            obj,
            lin.Unknown(len(obj), self.nodes[ancestor_fid].data.nrows),
            " -> ".join(methods) if len(methods) > 1 else "",
        )
        return self._obj_to_fid[id(obj)]

    def register(self, obj, label: str, step: int | None, plan: dict | None = None) -> str:
        import pandas as pd

        frame = obj.to_frame() if isinstance(obj, pd.Series) else obj
        fid = self._new_fid()
        self._depth += 1  # storing a frame uses pandas; do not re-trace it
        try:
            data = self.store.put_frame(frame, plan)
        finally:
            self._depth -= 1
        self.nodes[fid] = FrameNode(
            fid=fid, label=label, data=data, step=step, columns=[str(c) for c in frame.columns]
        )
        if data.sampled:
            self.warn(
                f"{label}: stored the first {data.stored_rows:,} of {data.nrows:,} rows "
                f"(sample_rows); lineage stays exact, materialized rows beyond that are NA"
            )
        key = id(obj)
        self._obj_to_fid[key] = fid
        try:
            self._refs[key] = weakref.ref(obj, lambda _r, k=key: self._forget(k))  # type: ignore[misc]
        except TypeError:
            pass
        return fid

    def fid_of(self, obj, label: str | None = None) -> str:
        """Find the node for an object, or for a fid passed straight through.

        An unknown frame is a source only if nothing we know about produced it.
        If pandas derived it from a traced frame through an operation we do not
        cover, say so instead of presenting it as an input to the pipeline.
        """
        if isinstance(obj, str):
            return obj  # already resolved by the caller
        fid = self._live_fid(obj)
        if fid is not None:
            return fid
        origin = self._untraced_origin(obj)
        if origin is not None:
            return self._record_untraced(obj, origin[0], origin[1])
        return self.register(obj, label or _guess_name(obj) or "<external>", None)

    # -- recording -------------------------------------------------------

    def record(
        self,
        op: str,
        inputs: list,
        output,
        lineage: lin.Lineage,
        detail: str,
        col_map: dict | None = None,
        elapsed: float = 0.0,
    ) -> None:
        t0 = time.perf_counter()
        self._depth += 1
        try:
            self._record_inner(op, inputs, output, lineage, detail, col_map, elapsed)
        finally:
            self._depth -= 1
            self.capture_seconds += time.perf_counter() - t0

    @staticmethod
    def _is_minor(op: str, output) -> bool:
        """Column access that pandas performs on the way to something else.
        Still traced, but hidden from the step table by default."""
        import pandas as pd

        return isinstance(output, pd.Series) and op in {"select", "copy", "slice", "filter"}

    @staticmethod
    def _plan(idx: int, lineage: lin.Lineage, col_map: dict) -> dict:
        """Which output columns are just a parent column seen through a row
        mapping, and so need no bytes of their own."""
        plan: dict[str, dict] = {}
        for out_col, sources in (col_map or {}).items():
            if len(sources) != 1:
                continue  # ambiguous (suffixed join column) -- store it
            slot, parent_col = int(sources[0][0]), sources[0][1]
            if lineage.take_for(slot) is False:
                continue  # the operation created new values
            plan[out_col] = {"step": idx, "slot": slot, "name": parent_col}
        if lineage.kind in ("identity", "select"):
            plan["__index__"] = {"step": idx, "slot": 0, "name": "__index__"}
        return plan

    def _record_inner(self, op, inputs, output, lineage, detail, col_map, elapsed) -> None:
        in_fids = [self.fid_of(o) for o in inputs]
        idx = len(self.steps)
        out_fid = self.register(output, op, idx, self._plan(idx, lineage, col_map))
        self.lineages[idx] = lineage
        self.store.put_lineage(self.run_id, idx, lineage.arrays())
        self.steps.append(
            Step(
                idx=idx,
                op=op,
                inputs=in_fids,
                output=out_fid,
                detail=detail,
                lineage_meta=lineage.meta(),
                approximate=lineage.approximate,
                minor=self._is_minor(op, output),
                col_map=col_map or {},
                duration_ms=elapsed * 1000,
                loc=_caller_loc(),
            )
        )

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # -- persistence -----------------------------------------------------

    def manifest(self) -> dict:
        return {
            "run_id": self.run_id,
            "label": self.label,
            "created": self.started,
            "wall_seconds": round(time.time() - self.started, 3),
            "capture_seconds": round(self.capture_seconds, 3),
            "warnings": self.warnings,
            "nodes": {fid: n.to_json() for fid, n in self.nodes.items()},
            "steps": [s.to_json() for s in self.steps],
        }

    def flush(self) -> Path:
        return self.store.put_manifest(self.run_id, self.manifest())


# ---------------------------------------------------------------------------
# lineage derivation helpers
# ---------------------------------------------------------------------------


def _positions_by_index(parent, out) -> np.ndarray | None:
    """Map each output row back to a parent row using index labels.

    Works for every op that preserves the index (filter, sort, dropna,
    drop_duplicates, sample, head, take...). Returns None when the parent
    index has duplicates, which would make the mapping ambiguous.

    The general answer is Index.get_indexer, but that builds a hash table over
    the whole parent index -- on a million rows it costs more than the
    operation being traced. Two fast paths cover most real pipelines.
    """
    import pandas as pd

    try:
        pidx, oidx = parent.index, out.index
        if len(oidx) == 0:
            return np.empty(0, dtype=np.int64)

        # a fresh frame: the label is the position
        if isinstance(pidx, pd.RangeIndex) and pidx.start == 0 and pidx.step == 1:
            take = oidx.to_numpy()
            if take.dtype.kind in "iu" and (
                len(take) == 0 or (take.min() >= 0 and take.max() < len(pidx))
            ):
                return take.astype(np.int64, copy=False)

        # anything derived from one by row selection stays sorted
        if pidx.is_monotonic_increasing and pidx.is_unique:
            pvals = pidx.to_numpy()
            ovals = oidx.to_numpy()
            if pvals.dtype.kind in "iuf" and ovals.dtype.kind in "iuf":
                pos = np.searchsorted(pvals, ovals)
                clipped = np.clip(pos, 0, len(pvals) - 1)
                if np.array_equal(pvals[clipped], ovals):
                    return clipped.astype(np.int64, copy=False)

        if not pidx.is_unique:
            return None
        take = pidx.get_indexer(oidx)
    except Exception:
        return None
    if len(take) != len(out):
        return None
    return np.asarray(take, dtype=np.int64)


_POSITIONAL_OPS = {"head", "tail"}


def _positional_take(name: str, parent, args, kwargs):
    """head and tail select by position, so no index is needed to map them.
    Mirrors pandas: head is iloc[:n], tail is iloc[-n:] (empty when n is 0)."""
    n = kwargs.get("n", args[0] if args else 5)
    if not isinstance(n, (int, np.integer)) or isinstance(n, bool):
        return None
    rows = np.arange(len(parent), dtype=np.int64)
    if name == "head":
        return rows[:n]
    return rows[-n:] if n else rows[:0]


def _replay_is_safe(name: str, args, kwargs) -> bool:
    """Can this operation tolerate one extra column without changing what it
    selects? Anything that inspects every column cannot."""
    if name in (
        "sort_values",
        "sort_index",
        "nlargest",
        "nsmallest",
        "take",
        "query",
        "truncate",
        "reindex",
        "drop",
    ):
        return True
    if name == "dropna":  # `all` and `thresh` count the columns
        return kwargs.get("how", "any") != "all" and kwargs.get("thresh") is None
    if name == "drop_duplicates":  # without a subset, the tag makes every row unique
        return kwargs.get("subset", args[0] if args else None) is not None
    return False


def _positions_by_replay(orig, name, parent, out, args, kwargs):
    """Exact positions for a frame whose index cannot identify its own rows.

    Carries a hidden position column through a second run of the operation --
    the trick the merge handler uses -- and reads it back afterwards. Costs one
    extra execution of that one step, and only on the path that would otherwise
    give up and report every input row as a candidate.
    """
    import pandas as pd

    if orig is None or not isinstance(parent, pd.DataFrame) or _POS in parent.columns:
        return None
    if not _replay_is_safe(name, args, kwargs):
        return None
    tagged = parent.copy(deep=False)
    tagged[_POS] = np.arange(len(parent), dtype=np.int64)
    replay = orig(tagged, *args, **kwargs)
    if not isinstance(replay, pd.DataFrame) or _POS not in replay.columns:
        return None
    if len(replay) != len(out):
        return None
    take = replay[_POS].to_numpy(dtype="float64", na_value=np.nan)
    return np.nan_to_num(take, nan=lin.MISSING).astype(np.int64)


def _row_lineage(parent, out, name=None, args=(), kwargs=None, orig=None) -> lin.Lineage:
    """Best-effort row mapping for a single-parent operation.

    Three attempts, cheapest first: positional ops need nothing, the index
    answers for most of the rest, and replaying the step with tagged positions
    rescues frames whose index has duplicates.
    """
    n_in, n_out = len(parent), len(out)
    kwargs = kwargs or {}

    take = None
    if name in _POSITIONAL_OPS:
        take = _positional_take(name, parent, args, kwargs)
        if take is not None and len(take) != n_out:
            take = None
    if take is None:
        take = _positions_by_index(parent, out)
    if take is None:
        try:
            take = _positions_by_replay(orig, name, parent, out, args, kwargs)
        except Exception:
            take = None

    # -1 means "no parent row", which reindex genuinely produces. All of them
    # missing means we failed to map rather than that nothing matched.
    if take is not None and n_out and (take == lin.MISSING).all():
        take = None
    if take is not None:
        if n_out == n_in and np.array_equal(take, np.arange(n_in)):
            return lin.Identity(n_in)
        return lin.Select(take)
    if n_out == n_in:
        return lin.Identity(n_in, approximate=True)
    return lin.Unknown(n_out, n_in)


def _column_map(parent, out) -> dict[str, list[list]]:
    """Column-level provenance by name: an output column that exists in the
    parent came from there. Columns that do not are marked derived."""
    pcols = {str(c) for c in getattr(parent, "columns", [])}
    return {str(c): [["0", str(c)]] if str(c) in pcols else [] for c in getattr(out, "columns", [])}


# ---------------------------------------------------------------------------
# patching
# ---------------------------------------------------------------------------

_PATCHES: list[tuple[object, str, object]] = []


def _install(owner, name: str, factory) -> None:
    try:
        orig = getattr(owner, name)
    except AttributeError:
        return
    wrapper = factory(orig, name)
    functools.update_wrapper(wrapper, orig)
    wrapper.__blame_original__ = orig
    setattr(owner, name, wrapper)
    _PATCHES.append((owner, name, orig))


def _simple(handler):
    """Build a wrapper that runs the original op, then derives lineage."""

    def factory(orig, name):
        def wrapper(self, *args, **kwargs):
            t = _active_here()
            if t is None or t._depth > 0 or not _user_call():
                return orig(self, *args, **kwargs)
            # the guard is held across lineage derivation too: deriving
            # lineage uses pandas, and none of that may become a step itself
            t._depth += 1
            try:
                start = time.perf_counter()
                try:
                    out = orig(self, *args, **kwargs)
                finally:
                    elapsed = time.perf_counter() - start
                try:
                    handler(t, name, self, out, args, kwargs, elapsed, orig)
                except Exception as exc:  # never break the user's pipeline
                    t.warn(f"{name}: lineage capture failed ({type(exc).__name__}: {exc})")
            finally:
                t._depth -= 1
            return out

        return wrapper

    return factory


def _user_call() -> bool:
    """Did the user's own code invoke this, or did pandas invoke it internally?

    pandas implements many of its methods with the ones we patch: explode goes
    through reindex, stack through take. Those are not steps the user wrote,
    and recording them fabricates a pipeline that never existed. One frame
    lookup, far cheaper than the capture it guards.
    """
    try:
        caller = sys._getframe(2)  # 0 this, 1 the wrapper, 2 whoever called it
    except ValueError:
        return True
    name = caller.f_code.co_filename
    return not name.startswith((_PANDAS_DIR, _PKG_DIR))


def _traceable(obj) -> bool:
    import pandas as pd

    return isinstance(obj, (pd.DataFrame, pd.Series))


def _h_rows(t, name, parent, out, args, kwargs, elapsed, orig=None):
    """Ops with one frame input whose rows map back positionally."""
    if not _traceable(out) or not _traceable(parent):
        return
    lineage = _row_lineage(parent, out, name, args, kwargs, orig)
    if lineage.approximate:
        t.warn(
            f"{name}: could not identify rows exactly "
            f"(duplicate index and no safe replay); lineage is approximate"
        )
    t.record(name, [parent], out, lineage, _detail(args, kwargs), _column_map(parent, out), elapsed)


def _h_getitem(t, name, parent, out, args, kwargs, elapsed, orig=None):
    import pandas as pd

    if not _traceable(out) or not _traceable(parent):
        return
    key = args[0] if args else None
    if isinstance(key, (pd.Series, np.ndarray, list)) and not isinstance(key, str):
        mask = np.asarray(key)
        if mask.dtype == bool and len(mask) == len(parent):
            take = np.flatnonzero(mask).astype(np.int64)
            t.record(
                "filter",
                [parent],
                out,
                lin.Select(take),
                _detail(args, kwargs),
                _column_map(parent, out),
                elapsed,
            )
            return
    if isinstance(key, slice):
        bounds = (key.start, key.stop, key.step)
        if all(
            b is None or (isinstance(b, (int, np.integer)) and not isinstance(b, bool))
            for b in bounds
        ):
            take = np.arange(len(parent), dtype=np.int64)[key]
            t.record(
                "slice",
                [parent],
                out,
                lin.Select(take),
                _detail(args, kwargs),
                _column_map(parent, out),
                elapsed,
            )
            return
        # a label slice (dates, strings): let the index map it instead
        _h_rows(t, "slice", parent, out, args, kwargs, elapsed, orig)
        return
    if len(out) == len(parent):
        t.record(
            "select",
            [parent],
            out,
            lin.Identity(len(parent)),
            _detail(args, kwargs),
            _column_map(parent, out),
            elapsed,
        )
        return
    _h_rows(t, name, parent, out, args, kwargs, elapsed, orig)


def _h_identity(t, name, parent, out, args, kwargs, elapsed, orig=None):
    """Row-preserving, order-preserving ops: assign, rename, astype, fillna..."""
    if not _traceable(out) or not _traceable(parent):
        return
    if len(out) != len(parent):
        _h_rows(t, name, parent, out, args, kwargs, elapsed, orig)
        return
    t.record(
        name,
        [parent],
        out,
        lin.Identity(len(parent)),
        _detail(args, kwargs),
        _column_map(parent, out),
        elapsed,
    )


def _membership(keys, out_index, n_in, all_rows_key=None) -> lin.Group | None:
    """CSR group membership: which parent rows land in each output row.

    Every reshape that widens a table -- pivot, pivot_table, unstack -- moves
    values into columns and leaves one output row per distinct index key. So
    the row-level answer is the same shape as a group-by: output row i was made
    from every parent row carrying that key.
    """
    import pandas as pd

    # the grouper must be positional. A Series grouper is aligned on its own
    # index, which is silently wrong whenever the frame is not RangeIndexed.
    positions = pd.Series(np.arange(n_in, dtype=np.int64))
    groups = positions.groupby(keys, sort=False, dropna=False).indices

    every = np.arange(n_in, dtype=np.int64)
    offsets = np.zeros(len(out_index) + 1, dtype=np.int64)
    chunks = []
    for i, key in enumerate(out_index):
        found: object
        if all_rows_key is not None and key == all_rows_key:
            found = every  # a margins total spans everything
        else:
            found = groups.get(key)
            if found is None and isinstance(key, tuple) and len(key) == 1:
                found = groups.get(key[0])
        rows = np.asarray([] if found is None else found, dtype=np.int64)
        chunks.append(rows)
        offsets[i + 1] = offsets[i] + len(rows)
    if not any(len(c) for c in chunks):
        return None  # nothing matched: we read the keys wrong
    indices = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.int64)
    return lin.Group(offsets, indices, n_in)


def _reshape_keys(parent, kwargs, args):
    """The per-parent-row key that decides which output row it feeds."""
    index = kwargs.get("index")
    if index is None and args:
        index = args[0]
    if index is None:  # omitted: pivot uses the frame's own index
        return parent.index.to_numpy()
    names = [index] if isinstance(index, str) else list(index)
    if not all(n in getattr(parent, "columns", []) for n in names):
        return None
    values = [parent[n].to_numpy() for n in names]
    return values if len(values) > 1 else values[0]


def _h_reshape(t, name, parent, out, args, kwargs, elapsed, orig=None):
    """pivot, pivot_table, unstack: one output row per distinct index key."""
    if not _traceable(out) or not _traceable(parent):
        return
    lineage = None
    try:
        if name == "unstack":
            level = kwargs.get("level", args[0] if args else -1)
            idx = parent.index
            keys = idx.droplevel(level).to_numpy() if getattr(idx, "nlevels", 1) > 1 else None
        else:
            keys = _reshape_keys(parent, kwargs, args)
        if keys is not None:
            margin = kwargs.get("margins_name", "All") if kwargs.get("margins") else None
            lineage = _membership(keys, out.index, len(parent), margin)
    except Exception:
        lineage = None

    if lineage is None:
        t.warn(
            f"{name}: could not read the reshape keys; "
            f"lineage is approximate (every input row is a candidate)"
        )
        lineage = lin.Unknown(len(out), len(parent))
    t.record(name, [parent], out, lineage, _detail(args, kwargs), {}, elapsed)


def _h_melt(t, name, parent, out, args, kwargs, elapsed, orig=None):
    """melt stacks the value columns one after another, so output row i came
    from input row i % len(parent)."""
    if not _traceable(out) or not _traceable(parent):
        return
    n = len(parent)
    if n and len(out) % n == 0:
        take = np.tile(np.arange(n, dtype=np.int64), len(out) // n)
        lineage = lin.Select(take)
    else:
        t.warn(f"{name}: output is not a whole multiple of the input; lineage is approximate")
        lineage = lin.Unknown(len(out), n)
    t.record(name, [parent], out, lineage, _detail(args, kwargs), {}, elapsed)


def _h_apply(t, name, parent, out, args, kwargs, elapsed, orig=None):
    if not _traceable(out) or not _traceable(parent):
        return
    if kwargs.get("axis") in (1, "columns") or len(out) == len(parent):
        lineage = lin.Identity(len(parent), approximate=True)
        t.warn(f"{name}: row identity assumed through a user function (approximate lineage)")
    else:
        lineage = lin.Unknown(len(out), len(parent))
        t.warn(f"{name}: opaque user function changed the row count (lineage unknown)")
    t.record(name, [parent], out, lineage, _detail(args, kwargs), {}, elapsed)


def _merge_factory(orig, name):
    """Joins get exact lineage by carrying hidden position columns through."""

    def wrapper(left, right=None, *args, **kwargs):
        t = _active_here()
        if t is None or t._depth > 0 or not _user_call() or right is None:
            return orig(left, right, *args, **kwargs) if right is not None else orig(left, **kwargs)
        import pandas as pd

        if not (isinstance(left, pd.DataFrame) and isinstance(right, pd.DataFrame)):
            return orig(left, right, *args, **kwargs)
        if _LPOS in left.columns or _RPOS in right.columns:
            return orig(left, right, *args, **kwargs)

        t._depth += 1
        try:
            start = time.perf_counter()
            try:
                lp = left.copy(deep=False)
                rp = right.copy(deep=False)
                lp[_LPOS] = np.arange(len(left), dtype=np.int64)
                rp[_RPOS] = np.arange(len(right), dtype=np.int64)
                tagged = orig(lp, rp, *args, **kwargs)
                ltake = tagged[_LPOS].to_numpy(dtype="float64", na_value=np.nan)
                rtake = tagged[_RPOS].to_numpy(dtype="float64", na_value=np.nan)
                out = tagged.drop(columns=[_LPOS, _RPOS])
            except Exception as exc:
                # the position-tagged merge did not work here; fall back to the
                # plain one and say that this join is not in the graph
                t.warn(
                    f"merge: could not carry row positions through "
                    f"({type(exc).__name__}: {exc}); this join is untraced"
                )
                return orig(left, right, *args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
            try:
                lineage = lin.Join(
                    np.nan_to_num(ltake, nan=lin.MISSING).astype(np.int64),
                    np.nan_to_num(rtake, nan=lin.MISSING).astype(np.int64),
                )
                cmap = {}
                lcols = {str(c) for c in left.columns}
                rcols = {str(c) for c in right.columns}
                for c in out.columns:
                    s = str(c)
                    src = []
                    if s in lcols:
                        src.append(["0", s])
                    if s in rcols:
                        src.append(["1", s])
                    cmap[s] = src
                t.record("merge", [left, right], out, lineage, _detail(args, kwargs), cmap, elapsed)
            except Exception as exc:
                t.warn(f"merge: lineage capture failed ({type(exc).__name__}: {exc})")
        finally:
            t._depth -= 1
        return out

    return wrapper


def _concat_factory(orig, name):
    def wrapper(objs, *args, **kwargs):
        t = _active_here()
        if t is None or t._depth > 0 or not _user_call():
            return orig(objs, *args, **kwargs)
        import pandas as pd

        t._depth += 1
        try:
            start = time.perf_counter()
            try:
                items = list(objs)
                out = orig(items, *args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
            try:
                axis = kwargs.get("axis", args[0] if args else 0)
                if axis in (0, "index") and all(
                    isinstance(o, (pd.DataFrame, pd.Series)) for o in items
                ):
                    lineage = lin.Concat([len(o) for o in items])
                    cmap = {}
                    for c in getattr(out, "columns", []):
                        s = str(c)
                        cmap[s] = [
                            [str(i), s]
                            for i, o in enumerate(items)
                            if s in {str(x) for x in getattr(o, "columns", [])}
                        ]
                    t.record("concat", items, out, lineage, _detail(args, kwargs), cmap, elapsed)
            except Exception as exc:
                t.warn(f"concat: lineage capture failed ({type(exc).__name__}: {exc})")
        finally:
            t._depth -= 1
        return out

    return wrapper


def _guard_factory(orig, name):
    """Plotting and display: the frames these build internally are matplotlib's
    business, not steps in the user's pipeline. Run them with tracing off."""

    def wrapper(self, *args, **kwargs):
        t = _active_here()
        if t is None:
            return orig(self, *args, **kwargs)
        t._depth += 1
        try:
            return orig(self, *args, **kwargs)
        finally:
            t._depth -= 1

    return wrapper


def _groupby_factory(orig, name):
    def wrapper(self, *args, **kwargs):
        t = _active_here()
        if t is None:
            return orig(self, *args, **kwargs)
        t._depth += 1
        try:
            gb = orig(self, *args, **kwargs)
        finally:
            t._depth -= 1
        try:
            gb.__dict__["_blame_parent"] = self
            gb.__dict__["_blame_keys"] = _detail(args, kwargs)
        except Exception:
            pass
        return gb

    return wrapper


def _gb_getitem_factory(orig, name):
    """df.groupby(k)["col"] returns a new GroupBy; carry the parent across."""

    def wrapper(self, *args, **kwargs):
        t = _active_here()
        if t is None:
            return orig(self, *args, **kwargs)
        t._depth += 1
        try:
            sub = orig(self, *args, **kwargs)
        finally:
            t._depth -= 1
        try:
            sub.__dict__["_blame_parent"] = self.__dict__.get("_blame_parent")
            sub.__dict__["_blame_keys"] = self.__dict__.get("_blame_keys", "")
        except Exception:
            pass
        return sub

    return wrapper


def _group_keys(gb, out) -> list:
    """The group label for each output row, in output order.

    With as_index=True the labels are the output index. With as_index=False
    they are columns of the output named after the grouping keys.
    """
    grouper = getattr(gb, "_grouper", None) or getattr(gb, "grouper", None)
    names = [n for n in (grouper.names if grouper is not None else []) if n is not None]
    cols = {str(c) for c in getattr(out, "columns", [])}
    if names and all(str(n) in cols for n in names):
        frame = out[names] if len(names) > 1 else None
        if frame is not None:
            return [tuple(r) for r in frame.itertuples(index=False, name=None)]
        return list(out[names[0]])
    return list(out.index)


def _agg_factory(orig, name):
    def wrapper(self, *args, **kwargs):
        t = _active_here()
        if t is None or t._depth > 0 or not _user_call():
            return orig(self, *args, **kwargs)
        # held across the whole body: reading the group keys back off the
        # result is a column access, and must not be recorded as a step
        t._depth += 1
        try:
            start = time.perf_counter()
            try:
                out = orig(self, *args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
            try:
                parent = self.__dict__.get("_blame_parent")
                if parent is None or not _traceable(out):
                    return out
                groups = self.indices  # label -> positional indices into parent
                keys = _group_keys(self, out)
                offsets = np.zeros(len(keys) + 1, dtype=np.int64)
                chunks = []
                for i, k in enumerate(keys):
                    idx = groups.get(k)
                    if idx is None and isinstance(k, tuple) and len(k) == 1:
                        idx = groups.get(k[0])
                    idx = np.asarray([] if idx is None else idx, dtype=np.int64)
                    chunks.append(idx)
                    offsets[i + 1] = offsets[i] + len(idx)
                indices = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.int64)
                lineage = lin.Group(offsets, indices, len(parent))
                label = f"groupby.{name}"
                detail = self.__dict__.get("_blame_keys", "") or _detail(args, kwargs)
                t.record(label, [parent], out, lineage, detail, _column_map(parent, out), elapsed)
            except Exception as exc:
                t.warn(f"groupby.{name}: lineage capture failed ({type(exc).__name__}: {exc})")
        finally:
            t._depth -= 1
        return out

    return wrapper


_FINALIZE_NOISE = {
    "__finalize__",
    "wrapper",
    "f",
    "new_func",
    "_constructor",
    "__init__",
    "__call__",
    "pipe",
    "apply",
    "_apply",
}


def _derive_method(method) -> str:
    """The name of the pandas operation that produced a frame.

    pandas passes `method` to __finalize__ only sometimes. When it does not,
    the outermost public pandas function on the stack is the operation the user
    called -- "pivot" reads a great deal better than "?".
    """
    if method:
        return str(method)
    found = None
    frame = inspect.currentframe()
    for _ in range(40):
        if frame is None:
            break
        code = frame.f_code
        if (
            "pandas" in code.co_filename
            and not code.co_name.startswith("_")
            and code.co_name not in _FINALIZE_NOISE
        ):
            found = code.co_name
        frame = frame.f_back
    return found or "?"


def _finalize_factory(orig, name):
    """pandas calls __finalize__ whenever it derives one frame from another.
    At depth 0 that means an operation we do not trace just happened."""

    def wrapper(self, other, method=None, **kwargs):
        result = orig(self, other, method=method, **kwargs)
        t = _active_here()
        if t is not None and t._depth == 0 and other is not self:
            try:
                if _traceable(other) and _traceable(result):
                    t.note_derived(result, other, _derive_method(method))
            except Exception:
                pass
        return result

    return wrapper


def _reader_factory(orig, name):
    def wrapper(*args, **kwargs):
        out = orig(*args, **kwargs)
        t = _active_here()
        if t is not None and t._depth == 0 and _traceable(out):
            try:
                src = _short(args[0], 60) if args else ""
                t.register(out, f"{name}({src})", None)
            except Exception as exc:
                t.warn(f"{name}: source registration failed ({exc})")
        return out

    return wrapper


_ROW_OPS = [
    "query",
    "dropna",
    "drop_duplicates",
    "sample",
    "head",
    "tail",
    "take",
    "nlargest",
    "nsmallest",
    "sort_values",
    "sort_index",
    "drop",
    "reindex",
    "filter",
    "first",
    "last",
    "truncate",
]
_ID_OPS = [
    "assign",
    "rename",
    "astype",
    "fillna",
    "copy",
    "round",
    "clip",
    "replace",
    "reset_index",
    "set_index",
    "eval",
    "ffill",
    "bfill",
    "abs",
    "rank",
    "add_prefix",
    "add_suffix",
    "sort_index_",
    "where",
    "mask",
]
_AGG_OPS = [
    "agg",
    "aggregate",
    "sum",
    "mean",
    "count",
    "size",
    "min",
    "max",
    "median",
    "std",
    "var",
    "nunique",
    "first",
    "last",
    "prod",
    "sem",
]
_READERS = ["read_csv", "read_parquet", "read_json", "read_excel", "read_feather"]
# not data operations: whatever frames they build inside are not pipeline steps
_OPAQUE_OPS = ["hist", "boxplot", "info", "to_string", "_repr_html_", "__repr__"]
_RESHAPE_OPS = ["pivot", "pivot_table", "unstack"]


def install() -> None:
    with _LOCK:
        _install_all()


def _install_all() -> None:
    if _PATCHES:
        return
    import pandas as pd
    from pandas.core.groupby.generic import DataFrameGroupBy, SeriesGroupBy

    for cls in (pd.DataFrame, pd.Series):
        _install(cls, "__getitem__", _simple(_h_getitem))
        for op in _ROW_OPS:
            _install(cls, op, _simple(_h_rows))
        for op in _ID_OPS:
            _install(cls, op, _simple(_h_identity))
        for op in ("apply", "map", "transform", "pipe"):
            _install(cls, op, _simple(_h_apply))
        _install(cls, "merge", _merge_factory)
        _install(cls, "join", _merge_factory)
        _install(cls, "groupby", _groupby_factory)
        _install(cls, "resample", _groupby_factory)
        for op in _RESHAPE_OPS:
            _install(cls, op, _simple(_h_reshape))
        _install(cls, "melt", _simple(_h_melt))
        for op in _OPAQUE_OPS:
            _install(cls, op, _guard_factory)

    for cls in (pd.DataFrame, pd.Series):
        _install(cls, "__finalize__", _finalize_factory)
    _install(pd, "merge", _merge_factory)
    _install(pd, "concat", _concat_factory)
    _install(pd, "melt", _simple(_h_melt))
    _install(pd, "pivot", _simple(_h_reshape))
    _install(pd, "pivot_table", _simple(_h_reshape))
    _install(DataFrameGroupBy, "__getitem__", _gb_getitem_factory)
    for op in _AGG_OPS:
        _install(DataFrameGroupBy, op, _agg_factory)
        _install(SeriesGroupBy, op, _agg_factory)
    try:
        from pandas.plotting import PlotAccessor
    except ImportError:
        from pandas.plotting._core import PlotAccessor
    _install(PlotAccessor, "__call__", _guard_factory)

    # resample is a group-by over time bins: same machinery, and `.indices`
    # gives the bin membership directly. __getitem__ must carry the parent
    # across or df.resample("D")["v"].sum() loses its link to the source.
    from pandas.core.resample import Resampler

    _install(Resampler, "__getitem__", _gb_getitem_factory)
    for op in _AGG_OPS:
        _install(Resampler, op, _agg_factory)
    for r in _READERS:
        _install(pd, r, _reader_factory)


def uninstall() -> None:
    failed: list[str] = []
    with _LOCK:
        while _PATCHES:
            owner, name, orig = _PATCHES.pop()
            try:
                setattr(owner, name, orig)
            except Exception as exc:
                # Keep restoring the rest: leaving the remaining patches in
                # place would be worse. But say so -- a method left wrapped
                # affects every later pandas call in the process, and a silent
                # pass here made that indistinguishable from a clean exit.
                failed.append(f"{getattr(owner, '__name__', owner)}.{name} ({exc})")
    if failed:
        warnings.warn(
            "blame: could not restore " + ", ".join(failed) + " -- pandas is "
            "still patched for these; restart the process to be sure of it",
            stacklevel=2,
        )


def start(store: Store, label: str = "") -> Tracer:
    global _ACTIVE
    # The check and the assignment have to be one step. install() sits between
    # them and touches 150-odd attributes, which is more than enough of a
    # window for a second thread to pass a check the first has not yet
    # invalidated -- and then two traces run at once, each missing the other's
    # steps, with whichever finishes first unpatching pandas underneath the
    # other.
    with _LOCK:
        if _ACTIVE is not None:
            raise RuntimeError("a blame trace is already running")
        install()  # reacquires _LOCK; it is reentrant for exactly this
        _ACTIVE = Tracer(store, uuid.uuid4().hex[:12], label)
        return _ACTIVE


def stop() -> Tracer | None:
    global _ACTIVE
    with _LOCK:
        t = _ACTIVE
        _ACTIVE = None
        uninstall()  # outside a trace, pandas is exactly as we found it
    # Flushing writes to disk and warn() runs whatever the user's warning
    # filters do, so neither belongs under the lock: the trace is already
    # closed and pandas already restored, and another thread should be free to
    # start its own trace while this one is still writing.
    if t is not None:
        t.flush()
        for msg in t.warnings:
            warnings.warn(f"blame: {msg}", stacklevel=2)
    return t
