"""Lineage vectors: how output rows map back to input rows, per operation.

Every traced operation produces one Lineage object. It answers two questions:
  parents_of(out_rows)      -> which input rows produced these output rows
  children_of(slot, rows)   -> which output rows did these input rows reach

Row identity is positional throughout: row i means the i-th row of that frame
as it existed at that step. Positions compose cleanly across a pipeline;
pandas index labels do not (they get reset, duplicated and reused).
"""

from __future__ import annotations

import numpy as np

MISSING = -1


class Lineage:
    kind: str = "abstract"
    approximate: bool = False

    def parents_of(self, out_rows: np.ndarray) -> dict[int, np.ndarray]:
        raise NotImplementedError

    def children_of(self, slot: int, in_rows: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def take_for(self, slot: int) -> "np.ndarray | None | bool":
        """The row mapping for one input, as an array to index the parent with.

        None  -- identity, the parent column is the output column unchanged.
        False -- this operation creates new values; the column must be stored.
        """
        return False

    def arrays(self) -> dict[str, np.ndarray]:
        return {}

    @classmethod
    def rebuild(cls, meta: dict, arrays: dict[str, np.ndarray]) -> "Lineage":
        raise NotImplementedError

    def meta(self) -> dict:
        return {"kind": self.kind}


def _clean(a: np.ndarray) -> np.ndarray:
    a = a[a != MISSING]
    return np.unique(a)


class Identity(Lineage):
    """Output row i came from input row i. Projections, renames, astype, fillna."""

    kind = "identity"

    def __init__(self, n: int, approximate: bool = False):
        self.n = int(n)
        self.approximate = approximate

    def parents_of(self, out_rows):
        return {0: np.unique(np.asarray(out_rows, dtype=np.int64))}

    def children_of(self, slot, in_rows):
        return np.unique(np.asarray(in_rows, dtype=np.int64))

    def take_for(self, slot):
        return None

    def meta(self):
        return {"kind": self.kind, "n": self.n, "approximate": self.approximate}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(meta["n"], meta.get("approximate", False))


class Select(Lineage):
    """Output row i came from input row take[i]. Filters, sorts, head, dropna,
    drop_duplicates, sample, iloc — anything that picks and/or reorders rows."""

    kind = "select"

    def __init__(self, take: np.ndarray, approximate: bool = False):
        self.take = np.asarray(take, dtype=np.int64)
        self.approximate = approximate

    def parents_of(self, out_rows):
        rows = np.asarray(out_rows, dtype=np.int64)
        return {0: _clean(self.take[rows])}

    def children_of(self, slot, in_rows):
        return np.flatnonzero(np.isin(self.take, np.asarray(in_rows))).astype(np.int64)

    def take_for(self, slot):
        return self.take

    def arrays(self):
        return {"take": self.take}

    def meta(self):
        return {"kind": self.kind, "approximate": self.approximate}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(arrays["take"], meta.get("approximate", False))


class Join(Lineage):
    """Output row i came from left[ltake[i]] and right[rtake[i]].
    MISSING (-1) on either side for unmatched rows in an outer join."""

    kind = "join"

    def __init__(self, ltake: np.ndarray, rtake: np.ndarray):
        self.ltake = np.asarray(ltake, dtype=np.int64)
        self.rtake = np.asarray(rtake, dtype=np.int64)

    def parents_of(self, out_rows):
        rows = np.asarray(out_rows, dtype=np.int64)
        return {0: _clean(self.ltake[rows]), 1: _clean(self.rtake[rows])}

    def children_of(self, slot, in_rows):
        take = self.ltake if slot == 0 else self.rtake
        return np.flatnonzero(np.isin(take, np.asarray(in_rows))).astype(np.int64)

    def take_for(self, slot):
        return self.ltake if slot == 0 else self.rtake

    def arrays(self):
        return {"ltake": self.ltake, "rtake": self.rtake}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(arrays["ltake"], arrays["rtake"])


class Group(Lineage):
    """Output row i aggregates input rows indices[offsets[i]:offsets[i+1]].
    CSR layout, because groups are a ragged partition of the input."""

    kind = "group"

    def __init__(self, offsets: np.ndarray, indices: np.ndarray, n_in: int):
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.indices = np.asarray(indices, dtype=np.int64)
        self.n_in = int(n_in)
        self._owner: np.ndarray | None = None

    def parents_of(self, out_rows):
        rows = np.asarray(out_rows, dtype=np.int64)
        parts = [self.indices[self.offsets[r] : self.offsets[r + 1]] for r in rows]
        if not parts:
            return {0: np.empty(0, dtype=np.int64)}
        return {0: np.unique(np.concatenate(parts))}

    def children_of(self, slot, in_rows):
        if self._owner is None:
            owner = np.full(self.n_in, MISSING, dtype=np.int64)
            sizes = np.diff(self.offsets)
            owner[self.indices] = np.repeat(np.arange(len(sizes), dtype=np.int64), sizes)
            self._owner = owner
        rows = np.asarray(in_rows, dtype=np.int64)
        rows = rows[(rows >= 0) & (rows < self.n_in)]
        return _clean(self._owner[rows])

    def arrays(self):
        return {"offsets": self.offsets, "indices": self.indices}

    def meta(self):
        return {"kind": self.kind, "n_in": self.n_in}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(arrays["offsets"], arrays["indices"], meta["n_in"])


class Concat(Lineage):
    """Output rows are the inputs stacked in order. lengths[slot] rows each."""

    kind = "concat"

    def __init__(self, lengths: list[int]):
        self.lengths = np.asarray(lengths, dtype=np.int64)
        self.starts = np.concatenate([[0], np.cumsum(self.lengths)])

    def parents_of(self, out_rows):
        rows = np.asarray(out_rows, dtype=np.int64)
        out: dict[int, np.ndarray] = {}
        slots = np.searchsorted(self.starts, rows, side="right") - 1
        for slot in np.unique(slots):
            local = rows[slots == slot] - self.starts[slot]
            out[int(slot)] = np.unique(local)
        return out

    def children_of(self, slot, in_rows):
        return np.asarray(in_rows, dtype=np.int64) + self.starts[slot]

    def arrays(self):
        return {"lengths": self.lengths}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(arrays["lengths"].tolist())


class Unknown(Lineage):
    """We could not determine the mapping (opaque UDF that changed the row count).
    Honest answer: any input row could have contributed. Always flagged."""

    kind = "unknown"
    approximate = True

    def __init__(self, n_out: int, n_in: int):
        self.n_out = int(n_out)
        self.n_in = int(n_in)

    def parents_of(self, out_rows):
        return {0: np.arange(self.n_in, dtype=np.int64)}

    def children_of(self, slot, in_rows):
        return np.arange(self.n_out, dtype=np.int64)

    def meta(self):
        return {"kind": self.kind, "n_out": self.n_out, "n_in": self.n_in}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(meta["n_out"], meta["n_in"])


class Source(Lineage):
    """A frame that entered the pipeline from outside (read_csv, DataFrame(...))."""

    kind = "source"

    def __init__(self, n: int):
        self.n = int(n)

    def parents_of(self, out_rows):
        return {}

    def children_of(self, slot, in_rows):
        return np.empty(0, dtype=np.int64)

    def meta(self):
        return {"kind": self.kind, "n": self.n}

    @classmethod
    def rebuild(cls, meta, arrays):
        return cls(meta["n"])


_KINDS = {c.kind: c for c in (Identity, Select, Join, Group, Concat, Unknown, Source)}


def rebuild(meta: dict, arrays: dict[str, np.ndarray]) -> Lineage:
    return _KINDS[meta["kind"]].rebuild(meta, arrays)
