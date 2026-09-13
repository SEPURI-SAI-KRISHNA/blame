"""Command line access to recorded runs.

blame runs / steps / why / forward / diff / at / ui
"""

from __future__ import annotations

import argparse
import sys

from ._store import Store
from .query import Run


def _run(args) -> Run:
    return Run.load(args.run, args.root)


def _cmd_runs(args) -> int:
    store = Store(args.root)
    runs = store.list_runs()
    if not runs:
        print(f"no runs recorded under {args.root}")
        return 1
    rows = []
    for rid in runs:
        m = store.get_manifest(rid)
        rows.append(
            (rid, m.get("label", ""), str(len(m["steps"])), f"{m.get('wall_seconds', 0):.2f}s")
        )
    rows.sort(key=lambda r: r[0])
    width = max(len(r[1]) for r in rows) if rows else 0
    for rid, label, steps, wall in rows:
        print(f"{rid}  {label.ljust(width)}  {steps.rjust(4)} steps  {wall.rjust(8)}")
    return 0


def _cmd_steps(args) -> int:
    run = _run(args)
    print(run.summary())
    print()
    print(run.table(all_steps=args.all))
    if run.warnings:
        print()
        for w in run.warnings:
            print(f"  ! {w}")
    return 0


def _cmd_why(args) -> int:
    run = _run(args)
    explanation = run.why(row=args.row, col=args.col, target=args.target)
    print(explanation)
    if args.show:
        print()
        for label, frame in explanation.frames().items():
            print(f"--- {label}")
            print(frame.head(args.show).to_string())
    return 0


def _cmd_forward(args) -> int:
    run = _run(args)
    reached = run.forward(row=args.row, target=args.target)
    for fid, rows in reached.items():
        node = run.nodes[fid]
        preview = ", ".join(str(int(r)) for r in rows[:20])
        more = f" (+{len(rows) - 20} more)" if len(rows) > 20 else ""
        print(f"{fid:>5} {node.label:<24} {len(rows):>6} row(s)  [{preview}]{more}")
    return 0


def _cmd_at(args) -> int:
    run = _run(args)
    frame = run.at(args.step)
    step = run.steps[args.step]
    print(f"step {step.idx}: {step.op}({step.detail})  {step.loc}")
    print(frame.head(args.rows).to_string())
    if len(frame) > args.rows:
        print(f"... {len(frame) - args.rows} more rows")
    return 0


def _cmd_diff(args) -> int:
    store = Store(args.root)
    runs = store.list_runs()
    if len(runs) < 2 and not (args.a and args.b):
        print("blame: need two recorded runs to diff", file=sys.stderr)
        return 1
    if not (args.a and args.b):
        newest = sorted(runs, key=lambda r: store.get_manifest(r).get("created", 0))
        args.a, args.b = newest[-2], newest[-1]
    left = Run(store.get_manifest(args.a), store)
    right = Run(store.get_manifest(args.b), store)
    print(left.diff(right, target=args.target, on=args.on, rtol=args.rtol))
    return 0


def _cmd_ui(args) -> int:
    from .ui import serve

    serve(_run(args), port=args.port, open_browser=not args.no_open)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blame", description=__doc__)
    parser.add_argument("--root", default=".blame", help="trace directory (default: .blame)")
    parser.add_argument("--run", default=None, help="run id (default: most recent)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("runs", help="list recorded runs").set_defaults(fn=_cmd_runs)

    p = sub.add_parser("steps", help="show the operations in a run")
    p.add_argument("--all", action="store_true", help="include internal column access")
    p.set_defaults(fn=_cmd_steps)

    p = sub.add_parser("why", help="which source rows produced this row")
    p.add_argument("row", type=int)
    p.add_argument("--col", default=None)
    p.add_argument("--target", default=None, help="frame id, label or step index")
    p.add_argument("--show", type=int, default=0, metavar="N", help="print N source rows")
    p.set_defaults(fn=_cmd_why)

    p = sub.add_parser("forward", help="where a source row ended up")
    p.add_argument("row", type=int)
    p.add_argument("--target", default=None)
    p.set_defaults(fn=_cmd_forward)

    p = sub.add_parser("diff", help="compare two runs and explain the differences")
    p.add_argument("a", nargs="?", default=None, help="older run id (default: second newest)")
    p.add_argument("b", nargs="?", default=None, help="newer run id (default: newest)")
    p.add_argument("--target", default=None, help="frame id, label or step index")
    p.add_argument(
        "--on",
        action="append",
        default=None,
        metavar="COL",
        help="column to pair rows by; repeatable, applied to each "
        "frame that has it (e.g. --on region --on order_id)",
    )
    p.add_argument("--rtol", type=float, default=0.0, help="relative float tolerance")
    p.set_defaults(fn=_cmd_diff)

    p = sub.add_parser("ui", help="open the run in a browser")
    p.add_argument("--port", type=int, default=7654)
    p.add_argument("--no-open", action="store_true", help="print the URL, do not open a browser")
    p.set_defaults(fn=_cmd_ui)

    p = sub.add_parser("at", help="the frame produced by a step")
    p.add_argument("step", type=int)
    p.add_argument("--rows", type=int, default=20)
    p.set_defaults(fn=_cmd_at)

    args = parser.parse_args(argv)
    if isinstance(getattr(args, "target", None), str) and args.target.isdigit():
        args.target = int(args.target)
    try:
        return args.fn(args)
    except FileNotFoundError as exc:
        print(f"blame: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
