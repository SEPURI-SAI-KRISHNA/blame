"""Content-addressed storage for intermediate frames and lineage arrays.

Frames are stored one column at a time, each column addressed by the hash of
its serialized bytes. A pipeline step that touches two columns of a forty
column frame therefore stores two columns, not forty -- the rest are already
on disk under the same hashes.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc

# The on-disk layout's version, written into every run manifest.
#
# It buys one thing: the ability to fail clearly. Without it, the first
# incompatible change to this layout reaches a user as a KeyError from
# somewhere inside query.py, on a trace they recorded weeks ago. A manifest
# with no `format` key is version 1 -- that is what every trace written before
# this existed is.
#
# Raise it only for a change a reader of the previous version cannot cope with.
# Adding a key nobody has to read is not one of those.
FORMAT = 1

# A local debugging cache is bound by write time, not disk. zstd shrinks an
# int64 column 2.3x but costs 14x the time to write it, so compression is off
# by default and available for people who want the trade.
_CODECS = {
    "none": None,
    "lz4": "lz4_frame",
    "zstd": "zstd",
}


def _write_options(compression: str | None):
    codec = _CODECS.get(compression or "none")
    if codec is None:
        return ipc.IpcWriteOptions(compression=None)
    return ipc.IpcWriteOptions(compression=pa.Codec(codec, compression_level=1))


_WRITE_OPTS = _write_options("none")


def _to_arrow(col) -> pa.Array:
    """Get an Arrow array out of a pandas column without a numpy detour.
    pandas 3 backs strings with Arrow already; converting to object arrays and
    back was costing more than the compression."""
    try:
        arr = pa.array(col, from_pandas=True)
        return arr.combine_chunks() if isinstance(arr, pa.ChunkedArray) else arr
    except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError):
        pass
    try:
        return pa.array(col.to_numpy(copy=False))
    except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError):
        return pa.array([repr(v) for v in col], type=pa.string())


@dataclass
class ColumnRef:
    """Where a column's values live.

    kind="data"  -- stored on disk under `hash`
    kind="ref"   -- equal to a parent frame's column, seen through a step's
                    row mapping; nothing is stored
    kind="range" -- a RangeIndex, described by three numbers
    """

    name: str
    dtype: str
    kind: str = "data"
    hash: str | None = None
    ref: dict | None = None


@dataclass
class FrameData:
    nrows: int
    ncols: int
    columns: list[ColumnRef] = field(default_factory=list)
    index: ColumnRef | None = None
    sampled: bool = False
    stored_rows: int = 0

    def to_json(self) -> dict:
        return {
            "nrows": self.nrows,
            "ncols": self.ncols,
            "columns": [asdict(c) for c in self.columns],
            "index": asdict(self.index) if self.index else None,
            "sampled": self.sampled,
            "stored_rows": self.stored_rows,
        }

    @staticmethod
    def from_json(d: dict) -> FrameData:
        return FrameData(
            nrows=d["nrows"],
            ncols=d["ncols"],
            columns=[ColumnRef(**c) for c in d["columns"]],
            index=ColumnRef(**d["index"]) if d.get("index") else None,
            sampled=d.get("sampled", False),
            stored_rows=d.get("stored_rows", 0),
        )


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` so another process sees all of it or none.

    The temporary file has to be named for the *writer*, not the destination.
    A content-addressed store invites the opposite: two processes holding the
    same column compute the same digest, so a temp path derived from it is the
    same path for both. One renames it away while the other is still about to,
    and the loser fails with FileNotFoundError -- which surfaced as "lineage
    capture failed" and a run with steps missing from it.

    os.replace is atomic and overwrites, so the loser of the race writes the
    same bytes over the same bytes instead of raising.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.replace(tmp, path)
        except OSError:
            # os.replace overwrites silently on POSIX but not on Windows, which
            # refuses with WinError 5 while any process has the destination
            # open -- and a reader of this store holds it open for as long as
            # it is loading that column.
            #
            # Losing that race is not a failure here. The filename is a content
            # address, so a destination that already exists holds exactly these
            # bytes: the write is done, somebody else did it. Anything else is
            # a real error and still raises.
            if not path.exists():
                raise
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Store:
    def __init__(
        self,
        root: str | Path = ".blame",
        sample_rows: int | None = None,
        compression: str | None = "none",
    ):
        self.write_options = _write_options(compression)
        self.root = Path(root)
        self.columns_dir = self.root / "columns"
        self.runs_dir = self.root / "runs"
        self.sample_rows = sample_rows or float("inf")
        self.columns_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = set()

    # -- columns ---------------------------------------------------------

    @staticmethod
    def _address(arr: pa.Array) -> str:
        """Content address computed from the raw Arrow buffers, so a column we
        already hold costs a hash and nothing else -- no serializing, no
        compressing, no write."""
        h = hashlib.blake2b(digest_size=10)
        h.update(f"{arr.type}|{len(arr)}|".encode())
        for buf in arr.buffers():
            if buf is not None:
                h.update(memoryview(buf))
        return h.hexdigest()

    def _put_array(self, name: str, arr: pa.Array) -> str:
        digest = self._address(arr)
        if digest in self._seen:
            return digest
        path = self.columns_dir / f"{digest}.arrow"
        if not path.exists():
            table = pa.table({name: arr})
            sink = io.BytesIO()
            with ipc.new_stream(sink, table.schema, options=self.write_options) as w:
                w.write_table(table)
            _atomic_write(path, sink.getvalue())
        self._seen.add(digest)
        return digest

    def get_column(self, digest: str) -> pa.ChunkedArray:
        path = self.columns_dir / f"{digest}.arrow"
        with ipc.open_stream(path.read_bytes()) as reader:
            return reader.read_all().column(0)

    # -- frames ----------------------------------------------------------

    def put_frame(self, df, plan: dict | None = None) -> FrameData:
        """Store a frame. Columns named in `plan` are recorded as references to
        a parent frame instead of being written out -- a filtered or joined
        column is its parent's column seen through a row mapping, so storing
        the bytes again would be pure waste."""
        plan = plan or {}
        nrows = len(df)
        sampled = nrows > self.sample_rows
        view = df.iloc[: int(self.sample_rows)] if sampled else df

        refs: list[ColumnRef] = []
        for name in view.columns:
            if str(name) in plan:
                refs.append(ColumnRef(str(name), str(view[name].dtype), "ref", ref=plan[str(name)]))
                continue
            col = view[name]
            arr = _to_arrow(col)
            refs.append(
                ColumnRef(str(name), str(col.dtype), "data", self._put_array(str(name), arr))
            )

        index = self._put_index(view, plan)

        return FrameData(
            nrows=nrows,
            ncols=len(view.columns),
            columns=refs,
            index=index,
            sampled=sampled,
            stored_rows=len(view),
        )

    def _put_index(self, view, plan: dict) -> ColumnRef | None:
        import pandas as pd

        idx = view.index
        if "__index__" in plan:
            return ColumnRef("__index__", str(idx.dtype), "ref", ref=plan["__index__"])
        if isinstance(idx, pd.RangeIndex):
            return ColumnRef(
                "__index__",
                str(idx.dtype),
                "range",
                ref={"start": int(idx.start), "stop": int(idx.stop), "step": int(idx.step)},
            )
        try:
            arr = _to_arrow(idx.to_series())
        except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError):
            return None
        return ColumnRef("__index__", str(idx.dtype), "data", self._put_array("__index__", arr))

    # -- lineage ---------------------------------------------------------

    @staticmethod
    def _narrow(a: np.ndarray) -> np.ndarray:
        """Row positions fit in int32 for any frame under 2 billion rows.
        Halves both the bytes written and the time spent writing them."""
        if a.dtype == np.int64 and (len(a) == 0 or (a.min() >= -(2**31) and a.max() < 2**31 - 1)):
            return a.astype(np.int32, copy=False)
        return a

    def put_lineage(self, run_id: str, step: int, arrays: dict[str, np.ndarray]) -> None:
        if not arrays:
            return
        d = self.runs_dir / run_id / "lineage" / f"{step:06d}"
        d.mkdir(parents=True, exist_ok=True)
        for name, values in arrays.items():
            # one file per array: a group lineage holds ragged arrays of
            # different lengths, which cannot share a single table
            table = pa.table({name: pa.array(self._narrow(values))})
            with ipc.new_file(d / f"{name}.arrow", table.schema, options=self.write_options) as w:
                w.write_table(table)

    def get_lineage(self, run_id: str, step: int) -> dict[str, np.ndarray]:
        d = self.runs_dir / run_id / "lineage" / f"{step:06d}"
        if not d.is_dir():
            return {}
        out: dict[str, np.ndarray] = {}
        for path in sorted(d.glob("*.arrow")):
            with ipc.open_file(path) as reader:
                table = reader.read_all()
            name = table.schema.names[0]
            out[name] = (
                table.column(name).to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
            )
        return out

    # -- runs ------------------------------------------------------------

    def put_manifest(self, run_id: str, manifest: dict) -> Path:
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / "manifest.json"
        payload = {"format": FORMAT, **manifest}
        # Atomic for the same reason as a column, one step removed: a reader
        # listing runs while this one is being written must not catch the file
        # half-formed.
        _atomic_write(path, json.dumps(payload, indent=2, default=str).encode("utf-8"))
        return path

    def get_manifest(self, run_id: str) -> dict:
        path = self.runs_dir / run_id / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        # Absent means version 1: every trace written before the key existed.
        found = manifest.get("format", 1)
        if not isinstance(found, int) or isinstance(found, bool) or found > FORMAT:
            from . import __version__

            raise RuntimeError(
                f"the trace in {path.parent} was written in store format {found!r}, "
                f"and pandas-blame {__version__} reads format {FORMAT}. Upgrade "
                f"pandas-blame to open it, or record the pipeline again with this "
                f"version."
            )
        return manifest

    def list_runs(self) -> list[str]:
        runs = [p.name for p in self.runs_dir.iterdir() if (p / "manifest.json").exists()]
        return sorted(runs)

    def disk_usage(self) -> dict[str, int]:
        def total(p: Path) -> int:
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

        return {"columns": total(self.columns_dir), "runs": total(self.runs_dir)}
