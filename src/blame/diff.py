"""Comparing two runs: which output rows changed, and which input rows did it.

A diff without lineage tells you row 812 changed. That is the easy half, and
on its own it is not much better than a spreadsheet compare. The half that
matters is the attribution: row 812 changed *because* input row 91 changed its
price, and here is the operation that carried it across.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .query import Run

_MAX_SHOW = 12


# ---------------------------------------------------------------------------
# aligning rows across two runs
# ---------------------------------------------------------------------------


def _fmt(value) -> str:
    """numpy scalars repr as np.float64(72.0), which helps nobody."""
    import pandas as pd

    if isinstance(value, np.generic):
        value = value.item()
    if value is None or (
        not isinstance(value, (list, tuple, dict, set, np.ndarray)) and pd.isna(value)
    ):
        return "NA"
    return repr(value)


def _keys_for(frame, on):
    """A per-row identity for a frame, and the name of the scheme used.

    Positions are not identity across runs: insert one row at the top and every
    position below it shifts, which would report the whole table as changed.
    `on` is a list of candidate columns -- a report is keyed by region while
    the orders that fed it are keyed by order_id, and one diff spans both.
    """
    import pandas as pd

    if on:
        names = [on] if isinstance(on, str) else list(on)
        present = [n for n in names if n in frame.columns]
        if present:
            values = (
                frame[present[0]].to_numpy().tolist()
                if len(present) == 1
                else list(frame[present].itertuples(index=False, name=None))
            )
            return (
                values,
                f"column{'s' if len(present) > 1 else ''} "
                f"{present if len(present) > 1 else present[0]!r}",
            )

    index = frame.index
    if index.is_unique and not isinstance(index, pd.RangeIndex):
        values = index.to_numpy()
        # 0..n-1 is position wearing a disguise; say so rather than claim a key
        if not (values.dtype.kind in "iu" and np.array_equal(values, np.arange(len(frame)))):
            return list(index), "index"
    return list(range(len(frame))), "position"


def _unique(keys) -> bool:
    return len(set(keys)) == len(keys)


def _align(left, right, on=None):
    """Pair the rows of two frames, saying which identity it managed to use.

    A candidate key that is not unique in a given frame is no identity at all,
    so fall back rather than pair rows wrongly: `region` keys the report but
    repeats in the customer table that fed it.
    """
    for attempt in [on, None] if on else [None]:
        lkeys, how = _keys_for(left, attempt)
        rkeys, _ = _keys_for(right, attempt)
        if _unique(lkeys) and _unique(rkeys):
            if attempt is None and on:
                how += ", the given key not being unique here"
            return lkeys, rkeys, how
    return list(range(len(left))), list(range(len(right))), "position (no unique key)"


def _ne_mask(a, b, rtol: float):
    """Positional inequality between two equal-length columns, NA == NA.

    Vectorized on purpose: comparing row by row in Python made the diff scale
    with the size of the inputs rather than with the size of the difference,
    which is the wrong thing entirely for a tool you reach for when a report
    of a million rows moved by three.
    """
    import pandas as pd

    sa, sb = pd.Series(a.to_numpy()), pd.Series(b.to_numpy())
    both_na = (sa.isna() & sb.isna()).to_numpy()
    if rtol and sa.dtype.kind in "fc" and sb.dtype.kind in "fc":
        close = np.isclose(sa.to_numpy(), sb.to_numpy(), rtol=rtol, atol=0.0, equal_nan=True)
        return ~close
    try:
        ne = sa.ne(sb).to_numpy()
    except Exception:  # ragged objects: fall back elementwise
        ne = np.array([_differs(x, y, rtol) for x, y in zip(sa, sb, strict=True)], dtype=bool)
    return ne & ~both_na


def _differs(a, b, rtol: float) -> bool:
    import pandas as pd

    a_na, b_na = pd.isna(a), pd.isna(b)
    if isinstance(a_na, np.ndarray) or isinstance(b_na, np.ndarray):
        return not np.array_equal(np.asarray(a), np.asarray(b))
    if a_na and b_na:
        return False
    if a_na != b_na:
        return True
    if rtol and isinstance(a, (int, float, np.number)) and isinstance(b, (int, float, np.number)):
        return not np.isclose(float(a), float(b), rtol=rtol, atol=0.0)
    return bool(a != b)


@dataclass
class CellChange:
    column: str
    before: object
    after: object

    def __repr__(self) -> str:
        return f"{self.column}: {_fmt(self.before)} -> {_fmt(self.after)}"


@dataclass
class RowChange:
    key: object
    kind: str  # "changed" | "added" | "removed"
    cells: list[CellChange] = field(default_factory=list)
    causes: list[Cause] = field(default_factory=list)


@dataclass
class Cause:
    source: str
    key: object
    kind: str
    cells: list[CellChange] = field(default_factory=list)

    def __repr__(self) -> str:
        what = ", ".join(repr(c) for c in self.cells) if self.cells else self.kind
        return f"{self.source} row {_fmt(self.key)}: {what}"


def _frame_delta(left, right, on, rtol):
    """Added, removed and changed rows between two frames, keyed for pairing."""
    lkeys, rkeys, how = _align(left, right, on)
    lpos = {k: i for i, k in enumerate(lkeys)}
    rpos = {k: i for i, k in enumerate(rkeys)}
    shared = [c for c in right.columns if c in left.columns]

    added = [k for k in rkeys if k not in lpos]
    removed = [k for k in lkeys if k not in rpos]
    common = [k for k in rkeys if k in lpos]

    changed: dict[object, list[CellChange]] = {}
    if common and shared:
        a = left.take(np.fromiter((lpos[k] for k in common), np.int64, len(common)))
        b = right.take(np.fromiter((rpos[k] for k in common), np.int64, len(common)))
        hits: dict[int, list] = {}
        for col in shared:
            for i in np.flatnonzero(_ne_mask(a[col], b[col], rtol)):
                hits.setdefault(int(i), []).append(col)
        for pos, cols in sorted(hits.items()):
            ra, rb = a.iloc[pos], b.iloc[pos]
            changed[common[pos]] = [CellChange(str(c), ra[c], rb[c]) for c in cols]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "how": how,
        "lpos": lpos,
        "rpos": rpos,
        "lkeys": lkeys,
        "rkeys": rkeys,
        "gained": [c for c in right.columns if c not in left.columns],
        "lost": [c for c in left.columns if c not in right.columns],
    }


# ---------------------------------------------------------------------------
# the diff
# ---------------------------------------------------------------------------


@dataclass
class Diff:
    left: Run
    right: Run
    label: str
    shape_before: tuple[int, int]
    shape_after: tuple[int, int]
    aligned_by: str
    changes: list[RowChange]
    gained_columns: list[str]
    lost_columns: list[str]
    explained: int
    unexplained: int
    source_summary: dict[str, str]

    def __bool__(self) -> bool:
        return bool(self.changes or self.gained_columns or self.lost_columns)

    def unexplained_rows(self) -> list[RowChange]:
        """Rows that differ with nothing in the inputs to account for them.

        The assertion worth putting in a test: inputs that did not move should
        not produce outputs that did, and when they do it is the pipeline that
        changed, not the data.
        """
        return [c for c in self.changes if not c.causes]

    def __repr__(self) -> str:
        lines = [
            f"diff {self.left.run_id} -> {self.right.run_id}",
            f"  {self.label}: {self.shape_before[0]}x{self.shape_before[1]}"
            f" -> {self.shape_after[0]}x{self.shape_after[1]}"
            f"   (rows paired by {self.aligned_by})",
        ]
        if not self:
            lines.append("\n  no differences")
            return "\n".join(lines)

        if self.gained_columns:
            lines.append(f"  + columns {self.gained_columns}")
        if self.lost_columns:
            lines.append(f"  - columns {self.lost_columns}")

        counts = {
            k: sum(1 for c in self.changes if c.kind == k) for k in ("changed", "added", "removed")
        }
        lines.append("  " + ", ".join(f"{n} {k}" for k, n in counts.items() if n))
        lines.append("")

        for change in self.changes[:_MAX_SHOW]:
            mark = {"changed": "~", "added": "+", "removed": "-"}[change.kind]
            what = "; ".join(repr(c) for c in change.cells) or change.kind
            lines.append(f"  {mark} row {_fmt(change.key)}  {what}")
            for cause in change.causes[:4]:
                lines.append(f"      caused by {cause!r}")
            if not change.causes:
                lines.append(
                    "      no changed input row explains this "
                    "(the pipeline itself changed, or a step is approximate)"
                )
        if len(self.changes) > _MAX_SHOW:
            lines.append(f"  ... {len(self.changes) - _MAX_SHOW} more")

        lines.append("")
        lines.append(
            f"  {self.explained} of {self.explained + self.unexplained} "
            f"differing rows traced to a changed input row"
        )
        for name, summary in self.source_summary.items():
            lines.append(f"    {name}: {summary}")
        return "\n".join(lines)


def _source_frames(run):
    """Source frames of a run, keyed by label so two runs can be matched up."""
    out: dict[str, str] = {}
    for fid in run.sources:
        out.setdefault(run.nodes[fid].label, fid)
    return out


def diff(left, right, target=None, on=None, rtol: float = 0.0, explain: int = 50) -> Diff:
    """Compare the same frame across two runs and attribute the differences.

    `explain` caps how many differing rows get lineage attribution, which costs
    one `why` per row per run.
    """
    lfid, rfid = left._resolve(target), right._resolve(target)
    before, after = left.frame(lfid), right.frame(rfid)
    delta = _frame_delta(before, after, on, rtol)

    # the same treatment for every source, so a changed output row can be
    # matched against the input rows that actually moved
    lsources, rsources = _source_frames(left), _source_frames(right)
    source_delta, summary = {}, {}
    for name, lsrc in lsources.items():
        rsrc = rsources.get(name)
        if rsrc is None:
            summary[name] = "missing from the second run"
            continue
        a, b = left.frame(lsrc), right.frame(rsrc)
        sd = _frame_delta(a, b, on, rtol)
        source_delta[name] = (lsrc, rsrc, sd)
        bits = []
        if sd["added"]:
            bits.append(f"{len(sd['added'])} added")
        if sd["removed"]:
            bits.append(f"{len(sd['removed'])} removed")
        if sd["changed"]:
            bits.append(f"{len(sd['changed'])} changed")
        summary[name] = (
            (f"{len(a)} -> {len(b)} rows, " + ", ".join(bits))
            if bits
            else f"{len(a)} rows, unchanged"
        ) + f"  (by {sd['how']})"

    changes: list[RowChange] = []
    for key, cells in delta["changed"].items():
        changes.append(RowChange(key, "changed", cells))
    for key in delta["added"]:
        changes.append(RowChange(key, "added"))
    for key in delta["removed"]:
        changes.append(RowChange(key, "removed"))

    explained = 0
    for change in changes[:explain]:
        change.causes = _attribute(change, left, right, lfid, rfid, delta, source_delta)
        explained += bool(change.causes)

    return Diff(
        left=left,
        right=right,
        label=right.nodes[rfid].label,
        shape_before=(len(before), before.shape[1]),
        shape_after=(len(after), after.shape[1]),
        aligned_by=delta["how"],
        changes=changes,
        gained_columns=[str(c) for c in delta["gained"]],
        lost_columns=[str(c) for c in delta["lost"]],
        explained=explained,
        unexplained=min(len(changes), explain) - explained,
        source_summary=summary,
    )


def _attribute(change, left, right, lfid, rfid, delta, source_delta) -> list[Cause]:
    """Which changed input rows feed this output row?

    Take the rows that produced it in each run, translate them into the keys
    the two runs are paired by, and keep only those the source diff flagged.
    """
    causes: list[Cause] = []
    contributing: dict[str, set] = {}

    for run, fid, pos_map, side in (
        (right, rfid, delta["rpos"], "r"),
        (left, lfid, delta["lpos"], "l"),
    ):
        pos = pos_map.get(change.key)
        if pos is None:
            continue
        try:
            sources = run.why(row=pos, target=fid).sources
        except Exception:
            continue
        for src_fid, rows in sources.items():
            name = run.nodes[src_fid].label
            entry = source_delta.get(name)
            if entry is None:
                continue
            keys = entry[2]["lkeys" if side == "l" else "rkeys"]
            contributing.setdefault(name, set()).update(
                keys[int(r)] for r in rows if int(r) < len(keys)
            )

    for name, keys in contributing.items():
        sd = source_delta[name][2]
        for key in sorted(keys, key=repr):
            if key in sd["changed"]:
                causes.append(Cause(name, key, "changed", sd["changed"][key]))
            elif key in set(sd["added"]):
                causes.append(Cause(name, key, "added"))
            elif key in set(sd["removed"]):
                causes.append(Cause(name, key, "removed"))
    # a row that vanished from a source cannot be reached by why() in the new
    # run, so look for removals among the rows that fed the old one
    return causes
