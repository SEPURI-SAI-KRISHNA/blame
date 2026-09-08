"""Querying a recorded run: why, forward, steps, at."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import _lineage as lin
from ._graph import FrameNode, Step
from ._store import Store

_MAX_SHOW = 20


def _apply_take(column, take: np.ndarray):
    """Index a parent column, turning -1 (an unmatched outer-join row) into NA."""
    import pandas as pd

    values = column.to_numpy()
    missing = take < 0
    safe = np.where(missing, 0, take)
    if len(values) and safe.max(initial=0) >= len(values):
        safe = np.clip(safe, 0, len(values) - 1)     # parent was sampled
        missing = missing | (take >= len(values))
    out = pd.Series(values[safe]) if len(values) else pd.Series([None] * len(take))
    return out.mask(pd.Series(missing)) if missing.any() else out


@dataclass
class Hop:
    step: int
    op: str
    detail: str
    loc: str
    approximate: bool
    out_fid: str
    out_rows: int
    contributions: dict[str, int] = field(default_factory=dict)

    def __repr__(self) -> str:
        flag = "  ~approximate" if self.approximate else ""
        via = f" [{self.loc}]" if self.loc else ""
        parts = ", ".join(f"{k}:{v} row{'s' if v != 1 else ''}" for k, v in self.contributions.items())
        return f"step {self.step:>3} {self.op}({self.detail}){via} <- {parts}{flag}"


@dataclass
class Explanation:
    run: "Run"
    target_fid: str
    target_rows: np.ndarray
    target_col: str | None
    sources: dict[str, np.ndarray]
    hops: list[Hop]
    approximate: bool

    def frames(self) -> dict[str, "object"]:
        """Materialize the contributing source rows, one DataFrame per source."""
        out = {}
        for fid, rows in self.sources.items():
            frame = self.run.frame(fid)
            keep = rows[rows < len(frame)]
            out[self.run.nodes[fid].label] = frame.iloc[keep]
        return out

    def __repr__(self) -> str:
        node = self.run.nodes[self.target_fid]
        cell = f", column {self.target_col!r}" if self.target_col else ""
        shown_rows = [int(r) for r in self.target_rows[:_MAX_SHOW]]
        head = (f"why({node.label} [{self.target_fid}] "
                f"{node.data.nrows}x{node.data.ncols}, "
                f"row{'s' if len(self.target_rows) != 1 else ''} "
                f"{shown_rows}{cell})")
        lines = [head, ""]
        if self.approximate:
            lines.append("  ! this path crosses an approximate step; treat rows as candidates")
            lines.append("")
        lines.append("  derivation:")
        for hop in self.hops:
            lines.append(f"    {hop!r}")
        lines.append("")
        lines.append("  source rows:")
        for fid, rows in self.sources.items():
            label = self.run.nodes[fid].label
            shown = ", ".join(str(r) for r in rows[:_MAX_SHOW])
            more = f" (+{len(rows) - _MAX_SHOW} more)" if len(rows) > _MAX_SHOW else ""
            lines.append(f"    {label} [{fid}]: {len(rows)} row(s) -> [{shown}]{more}")
        return "\n".join(lines)


class Run:
    """A recorded pipeline run, loaded from .blame/ or held in memory."""

    def __init__(self, manifest: dict, store: Store, tracer=None):
        self.store = store
        # only set for a run recorded in this process: lets `why(target=df)`
        # resolve an actual frame object rather than guessing which one you meant
        self._tracer = tracer
        self.run_id = manifest["run_id"]
        self.label = manifest.get("label", "")
        self.wall_seconds = manifest.get("wall_seconds", 0.0)
        self.capture_seconds = manifest.get("capture_seconds", 0.0)
        self.warnings = manifest.get("warnings", [])
        self.nodes: dict[str, FrameNode] = {
            fid: FrameNode.from_json(d) for fid, d in manifest["nodes"].items()
        }
        self.steps: list[Step] = [Step.from_json(d) for d in manifest["steps"]]
        self._lineage_cache: dict[int, lin.Lineage] = {}
        self._column_cache: dict[tuple[str, str], object] = {}

    # -- loading ---------------------------------------------------------

    @staticmethod
    def load(run_id: str | None = None, root: str | Path = ".blame") -> "Run":
        store = Store(root)
        runs = store.list_runs()
        if not runs:
            raise FileNotFoundError(f"no runs recorded under {Path(root).resolve()}")
        if run_id is None:
            run_id = max(runs, key=lambda r: store.get_manifest(r).get("created", 0))
        return Run(store.get_manifest(run_id), store)

    # -- structure -------------------------------------------------------

    @property
    def sources(self) -> list[str]:
        return [fid for fid, n in self.nodes.items() if n.step is None]

    @property
    def result_fid(self) -> str:
        consumed = {i for s in self.steps for i in s.inputs}
        leaves = [s.output for s in self.steps if s.output not in consumed and not s.minor]
        if leaves:
            return leaves[-1]
        major = [s.output for s in self.steps if not s.minor]
        return major[-1] if major else (self.steps[-1].output if self.steps else self.sources[-1])

    def lineage(self, step: int) -> lin.Lineage:
        if step not in self._lineage_cache:
            meta = self.steps[step].lineage_meta
            arrays = self.store.get_lineage(self.run_id, step)
            self._lineage_cache[step] = lin.rebuild(meta, arrays)
        return self._lineage_cache[step]

    def frame(self, fid: str):
        """Materialize a frame, resolving referenced columns through the
        lineage arrays of the steps that produced them."""
        import pandas as pd

        node = self.nodes[fid]
        cols = {c.name: self._column(fid, c.name) for c in node.data.columns}
        frame = pd.DataFrame({k: v for k, v in cols.items() if v is not None})
        index = self._column(fid, "__index__")
        if index is not None and len(index) == len(frame):
            frame.index = pd.Index(index)
            frame.index.name = None
        if node.data.sampled:
            frame.attrs["blame_sampled"] = (node.data.stored_rows, node.data.nrows)
        return frame

    def _colref(self, fid: str, name: str):
        data = self.nodes[fid].data
        if name == "__index__":
            return data.index
        for c in data.columns:
            if c.name == name:
                return c
        return None

    def _column(self, fid: str, name: str):
        """One column of one frame, following references back to stored bytes."""
        import pandas as pd

        key = (fid, name)
        if key in self._column_cache:
            return self._column_cache[key]
        ref = self._colref(fid, name)
        if ref is None:
            return None
        if ref.kind == "data":
            value = self.store.get_column(ref.hash).to_pandas()
        elif ref.kind == "range":
            r = ref.ref
            value = pd.Series(range(r["start"], r["stop"], r["step"]))
        else:
            step = self.steps[ref.ref["step"]]
            parent_fid = step.inputs[ref.ref["slot"]]
            parent = self._column(parent_fid, ref.ref["name"])
            if parent is None:
                return None
            take = self.lineage(step.idx).take_for(ref.ref["slot"])
            value = parent if take is None else _apply_take(parent, take)
        value = value.reset_index(drop=True)
        self._column_cache[key] = value
        return value

    def at(self, step: int):
        """The output frame of a step."""
        return self.frame(self.steps[step].output)

    def _fid_of_object(self, obj) -> str | None:
        """The node for a live frame object, if this run was recorded here."""
        tracer = self._tracer
        if tracer is None:
            return None
        fid = tracer._obj_to_fid.get(id(obj))
        if fid is None:
            return None
        ref = tracer._refs.get(id(obj))
        return fid if ref is None or ref() is obj else None   # guard id reuse

    def _resolve(self, target) -> str:
        if target is None:
            return self.result_fid
        if not isinstance(target, (str, int)) and hasattr(target, "index"):
            fid = self._fid_of_object(target)
            if fid is not None:
                return fid
            raise KeyError(
                "that frame is not in this run -- whatever produced it is not an "
                "operation blame traces, so it has no recorded lineage. "
                "run.table() lists the frames that are."
            )
        if isinstance(target, str):
            if target in self.nodes:
                return target
            matches = [f for f, n in self.nodes.items() if n.label == target]
            if matches:
                return matches[-1]
            raise KeyError(f"no frame {target!r}; known frames: {list(self.nodes)}")
        if isinstance(target, int):
            return self.steps[target].output
        raise TypeError("target must be a frame id, a label, a step index or None")

    # -- the questions ---------------------------------------------------

    def why(self, row: int | list[int], col: str | None = None, target=None,
            stop_at: str | None = None) -> Explanation:
        """Which source rows produced this output cell (or rows)."""
        fid = self._resolve(target)
        rows = np.atleast_1d(np.asarray(row, dtype=np.int64))
        frontier: dict[str, np.ndarray] = {fid: rows}
        hops: list[Hop] = []
        approximate = False

        for step in reversed(self.steps):
            if step.output not in frontier:
                continue
            if stop_at is not None and step.output == stop_at:
                continue
            out_rows = frontier.pop(step.output)
            parents = self.lineage(step.idx).parents_of(out_rows)
            contributions: dict[str, int] = {}
            for slot, prows in parents.items():
                if slot >= len(step.inputs):
                    continue
                pfid = step.inputs[slot]
                prev = frontier.get(pfid)
                frontier[pfid] = np.union1d(prev, prows) if prev is not None else prows
                contributions[pfid] = len(prows)
            approximate |= step.approximate
            if not step.minor:
                hops.append(Hop(step.idx, step.op, step.detail, step.loc, step.approximate,
                                step.output, len(out_rows), contributions))

        sources = {f: np.sort(r) for f, r in frontier.items()}
        return Explanation(self, fid, rows, col, sources, hops, approximate)

    def highlight(self, row: int | list[int], target=None) -> dict[str, np.ndarray]:
        """Every frame's rows connected to these rows, upstream and downstream.

        `why` answers "which source rows", stopping at the sources. This keeps
        the whole frontier -- every intermediate frame too -- and unions it with
        the forward closure, so a caller can paint one row's path through the
        entire pipeline in both directions. This is what the UI draws.
        """
        fid = self._resolve(target)
        rows = np.atleast_1d(np.asarray(row, dtype=np.int64))
        marks: dict[str, np.ndarray] = {fid: rows}

        def add(f: str, r: np.ndarray) -> None:
            prev = marks.get(f)
            marks[f] = np.union1d(prev, r) if prev is not None else r

        # upstream: reverse step order is reverse topological order, so a frame's
        # marks are complete by the time we reach the step that produced it.
        for step in reversed(self.steps):
            if step.output not in marks:
                continue
            for slot, prows in self.lineage(step.idx).parents_of(marks[step.output]).items():
                if slot < len(step.inputs):
                    add(step.inputs[slot], prows)

        # downstream: only from the rows actually asked about, not from their
        # ancestors -- an ancestor's other descendants did not touch this cell.
        down: dict[str, np.ndarray] = {fid: rows}
        for step in self.steps:
            hits = [self.lineage(step.idx).children_of(slot, down[in_fid])
                    for slot, in_fid in enumerate(step.inputs) if in_fid in down]
            if hits:
                merged = np.unique(np.concatenate(hits))
                if len(merged):
                    down[step.output] = merged
        for f, r in down.items():
            add(f, r)

        return {f: np.sort(r) for f, r in marks.items() if len(r)}

    def diff(self, other: "Run", target=None, on=None, rtol: float = 0.0,
             explain: int = 50):
        """What changed going from this run to `other`, and which input rows
        did it. Pass `on` to pair rows by a key column instead of the index."""
        from .diff import diff as _diff

        return _diff(self, other, target=target, on=on, rtol=rtol, explain=explain)

    def ui(self, port: int = 7654, open_browser: bool = True) -> None:
        """Serve this run on a local page and open it in a browser."""
        from .ui import serve

        serve(self, port=port, open_browser=open_browser)

    def forward(self, row: int | list[int], target=None) -> dict[str, np.ndarray]:
        """Everything a source row touched, downstream."""
        fid = self._resolve(target if target is not None else (self.sources[0] if self.sources else None))
        reached: dict[str, np.ndarray] = {fid: np.atleast_1d(np.asarray(row, dtype=np.int64))}
        for step in self.steps:
            hits: list[np.ndarray] = []
            for slot, in_fid in enumerate(step.inputs):
                if in_fid in reached:
                    hits.append(self.lineage(step.idx).children_of(slot, reached[in_fid]))
            if hits:
                merged = np.unique(np.concatenate(hits))
                if len(merged):
                    reached[step.output] = merged
        return {f: r for f, r in reached.items() if len(r)}

    # -- reporting -------------------------------------------------------

    def summary(self) -> str:
        overhead = (f"{self.capture_seconds:.2f}s capture on {self.wall_seconds:.2f}s wall"
                    if self.wall_seconds else "")
        usage = self.store.disk_usage()
        mb = (usage["columns"] + usage["runs"]) / 1e6
        parts = [f"run {self.run_id}"]
        if self.label:
            parts.append(f"({self.label})")
        parts.append(f"- {len(self.steps)} steps, {len(self.nodes)} frames, {mb:.1f} MB on disk")
        if overhead:
            parts.append(f"- {overhead}")
        return " ".join(parts)

    def table(self, all_steps: bool = False) -> str:
        steps = self.steps if all_steps else [s for s in self.steps if not s.minor]
        rows = [("step", "op", "detail", "shape", "where", "")]
        for s in steps:
            node = self.nodes[s.output]
            rows.append((
                str(s.idx), s.op, s.detail[:38],
                f"{node.data.nrows}x{node.data.ncols}",
                s.loc, "~" if s.approximate else "",
            ))
        widths = [max(len(r[i]) for r in rows) for i in range(6)]
        out = []
        for i, r in enumerate(rows):
            out.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip())
            if i == 0:
                out.append("  ".join("-" * w for w in widths).rstrip())
        hidden = len(self.steps) - len(steps)
        if hidden and not all_steps:
            out.append(f"({hidden} internal column access step(s) hidden; table(all_steps=True) to show)")
        return "\n".join(out)
