"""The run graph: frames as nodes, operations as edges."""

from __future__ import annotations

from dataclasses import dataclass, field

from ._store import FrameData


@dataclass
class FrameNode:
    fid: str
    label: str
    data: FrameData
    step: int | None = None       # step that produced it; None for sources
    columns: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "fid": self.fid,
            "label": self.label,
            "step": self.step,
            "columns": self.columns,
            "data": self.data.to_json(),
        }

    @staticmethod
    def from_json(d: dict) -> "FrameNode":
        return FrameNode(
            fid=d["fid"],
            label=d["label"],
            data=FrameData.from_json(d["data"]),
            step=d["step"],
            columns=d["columns"],
        )


@dataclass
class Step:
    idx: int
    op: str
    inputs: list[str]
    output: str
    detail: str = ""
    lineage_meta: dict = field(default_factory=dict)
    approximate: bool = False
    minor: bool = False
    col_map: dict[str, list[list]] = field(default_factory=dict)
    duration_ms: float = 0.0
    loc: str = ""

    def to_json(self) -> dict:
        return {
            "idx": self.idx,
            "op": self.op,
            "inputs": self.inputs,
            "output": self.output,
            "detail": self.detail,
            "lineage_meta": self.lineage_meta,
            "approximate": self.approximate,
            "minor": self.minor,
            "col_map": self.col_map,
            "duration_ms": round(self.duration_ms, 3),
            "loc": self.loc,
        }

    @staticmethod
    def from_json(d: dict) -> "Step":
        return Step(**d)
