"""blame -- cell-level "why" for pandas pipelines, with no code changes.

import blame

with blame.trace():
    orders = pd.read_csv("orders.csv")
    ...
    report = joined.groupby("region")["total"].sum()

run = blame.last_run()
print(run.table())          # every step
print(run.why(row=2))       # which source rows produced this cell
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from . import _tracer
from ._store import Store
from .diff import Diff
from .query import Explanation, Run

__version__ = "0.1.2"
__all__ = [
    "Diff",
    "Explanation",
    "Run",
    "diff",
    "forward",
    "last_run",
    "load",
    "start",
    "stop",
    "trace",
    "why",
]

_LAST: Run | None = None


def start(
    label: str = "",
    root: str | Path = ".blame",
    sample_rows: int | None = None,
    compression: str | None = "none",
) -> None:
    """Begin recording. Every pandas operation after this call is traced."""
    _tracer.start(Store(root, sample_rows=sample_rows, compression=compression), label)


def stop() -> Run | None:
    """Stop recording and return the finished run."""
    global _LAST
    tracer = _tracer.stop()
    if tracer is None:
        return None
    _LAST = Run(tracer.manifest(), tracer.store, tracer=tracer)
    return _LAST


@contextlib.contextmanager
def trace(
    label: str = "",
    root: str | Path = ".blame",
    sample_rows: int | None = None,
    compression: str | None = "none",
):
    """Context manager form. Yields a handle whose .run is set on exit."""

    class Handle:
        run: Run | None = None

    handle = Handle()
    start(label, root, sample_rows, compression)
    try:
        yield handle
    finally:
        handle.run = stop()


def last_run() -> Run:
    """The run recorded in this process, or the most recent one on disk."""
    if _LAST is not None:
        return _LAST
    return Run.load()


def load(run_id: str | None = None, root: str | Path = ".blame") -> Run:
    return Run.load(run_id, root)


def why(row: int | list[int], col: str | None = None, target=None) -> Explanation:
    return last_run().why(row=row, col=col, target=target)


def forward(row: int | list[int], target=None) -> dict:
    return last_run().forward(row=row, target=target)


def diff(left, right, target=None, on=None, rtol: float = 0.0) -> Diff:
    """Compare two runs. Either argument may be a run id."""
    if isinstance(left, str):
        left = Run.load(left)
    if isinstance(right, str):
        right = Run.load(right)
    return left.diff(right, target=target, on=on, rtol=rtol)
