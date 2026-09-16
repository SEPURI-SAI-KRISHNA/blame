"""A local page for a recorded run.

The DAG on the left, a grid on the right, and one click on a cell to see the
rows that made it light up in every frame of the pipeline.

Everything the page asks for is answered by the same `Run` methods the CLI
uses -- `why`, `forward`, `highlight` -- so the page cannot disagree with the
terminal about where a number came from.
"""

from __future__ import annotations

import json
import math
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

_ASSET = Path(__file__).parent / "_ui" / "index.html"

PAGE_ROWS = 200  # grid rows per request
MAX_MARKS = 20_000  # per frame; beyond this the page shows counts only


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------


def _cell(value):
    """One grid cell, JSON-safe. Numbers stay numbers so the page can align
    them; everything else becomes the string the user would see printed."""
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    try:
        import pandas as pd

        if value is pd.NaT or (not isinstance(value, (list, dict, tuple, set)) and pd.isna(value)):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    return str(value)


def _marks(arrays: dict[str, np.ndarray]) -> dict[str, dict]:
    out = {}
    for fid, rows in arrays.items():
        listed = [int(r) for r in rows[:MAX_MARKS]]
        out[fid] = {"n": len(rows), "rows": listed, "truncated": len(rows) > MAX_MARKS}
    return out


def run_payload(run) -> dict:
    """Structure only: no cell data, so the page loads instantly on a big run."""
    nodes = {}
    for fid, node in run.nodes.items():
        nodes[fid] = {
            "fid": fid,
            "label": node.label,
            "step": node.step,
            "nrows": node.data.nrows,
            "ncols": node.data.ncols,
            "columns": [c.name for c in node.data.columns],
            "sampled": [node.data.stored_rows, node.data.nrows] if node.data.sampled else None,
        }
    steps = [
        {
            "idx": s.idx,
            "op": s.op,
            "detail": s.detail,
            "loc": s.loc,
            "inputs": s.inputs,
            "output": s.output,
            "approximate": s.approximate,
            "minor": s.minor,
            "duration_ms": s.duration_ms,
        }
        for s in run.steps
    ]
    return {
        "run_id": run.run_id,
        "label": run.label,
        "summary": run.summary(),
        "warnings": list(run.warnings),
        "nodes": nodes,
        "steps": steps,
        "sources": run.sources,
        "result": run.result_fid,
        "page_rows": PAGE_ROWS,
    }


def frame_payload(run, fid: str, offset: int = 0, limit: int = PAGE_ROWS) -> dict:
    """One page of one frame. Positions, not index labels, are row identity --
    the grid says so by showing `#` first and the index only when it differs."""
    node = run.nodes[fid]
    frame = run.frame(fid)
    total = len(frame)
    offset = max(0, min(offset, max(0, total - 1)))
    view = frame.iloc[offset : offset + limit]

    index = [_cell(v) for v in view.index]
    labelled = index != list(range(offset, offset + len(view)))
    columns = [str(c) for c in view.columns]
    rows = [[_cell(v) for v in rec] for rec in view.itertuples(index=False, name=None)]
    numeric = [
        bool(np.issubdtype(view[c].dtype, np.number))
        if hasattr(view[c].dtype, "kind") and view[c].dtype.kind in "iufcb"
        else False
        for c in view.columns
    ]

    missing = [c.name for c in node.data.columns if str(c.name) not in columns]
    return {
        "fid": fid,
        "label": node.label,
        "nrows": total,
        "declared_rows": node.data.nrows,
        "columns": columns,
        "numeric": numeric,
        "offset": offset,
        "rows": rows,
        "index": index if labelled else None,
        "sampled": [node.data.stored_rows, node.data.nrows] if node.data.sampled else None,
        "unavailable": missing,
    }


def explain_payload(run, fid: str, rows: list[int], col: str | None = None) -> dict:
    explanation = run.why(row=rows, col=col, target=fid)
    marks = run.highlight(row=rows, target=fid)
    return {
        "fid": fid,
        "rows": [int(r) for r in rows],
        "col": col,
        "approximate": bool(explanation.approximate),
        "hops": [
            {
                "step": h.step,
                "op": h.op,
                "detail": h.detail,
                "loc": h.loc,
                "approximate": h.approximate,
                "out_rows": h.out_rows,
                "contributions": {k: int(v) for k, v in h.contributions.items()},
            }
            for h in explanation.hops
        ],
        "sources": {
            f: {"n": len(r), "rows": [int(x) for x in r[:MAX_MARKS]]}
            for f, r in explanation.sources.items()
        },
        "marks": _marks(marks),
    }


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


def _handler(run, lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "blame"

        def log_message(self, format: str, *args: object) -> None:
            pass  # a local UI should be quiet

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(json.dumps(obj, allow_nan=False).encode(), "application/json", status)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)

            def arg(name, default=None):
                return query.get(name, [default])[0]

            try:
                if url.path in ("/", "/index.html"):
                    self._send(_ASSET.read_bytes(), "text/html; charset=utf-8")
                    return
                with lock:
                    if url.path == "/api/run":
                        self._json(run_payload(run))
                    elif url.path == "/api/frame":
                        fid = arg("fid")
                        if fid is None:
                            self._json({"error": "fid is required"}, 400)
                            return
                        self._json(
                            frame_payload(
                                run,
                                fid,
                                int(arg("offset", 0) or 0),
                                min(int(arg("limit", PAGE_ROWS) or PAGE_ROWS), 2000),
                            )
                        )
                    elif url.path == "/api/why":
                        fid = arg("fid")
                        if fid is None:
                            self._json({"error": "fid is required"}, 400)
                            return
                        rows = [int(r) for r in (arg("rows", "") or "").split(",") if r != ""]
                        self._json(explain_payload(run, fid, rows, arg("col") or None))
                    else:
                        self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as exc:  # a failed query must not kill the page
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


class _Server(ThreadingHTTPServer):
    # SO_REUSEADDR means two different things. On POSIX it lets a port in
    # TIME_WAIT be reused, which is why http.server turns it on; binding a port
    # that is actually listening still fails, so the fallback below works.
    #
    # On Windows it also lets a second process bind a port that is already
    # listening. Both servers then have the same address and arriving
    # connections go to one of them unpredictably -- so `blame ui` in a second
    # terminal appeared to start, printed the same URL, and served roughly half
    # the requests for somebody else's run. The fallback never ran, because
    # nothing failed.
    allow_reuse_address = os.name != "nt"


def make_server(run, port: int = 7654, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A bound, not-yet-serving HTTP server for `run`. Port 0 picks a free one;
    any other port is tried, then the 19 after it."""
    if not _ASSET.exists():
        raise FileNotFoundError(f"UI asset missing: {_ASSET}")

    lock = threading.Lock()
    handler = _handler(run, lock)
    candidates = [0] if port == 0 else range(port, port + 20)
    for candidate in candidates:
        try:
            return _Server((host, candidate), handler)
        except OSError:
            continue
    raise OSError(f"no free port in {port}..{port + 19}")


def serve(run, port: int = 7654, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    """Serve `run` until interrupted."""
    server = make_server(run, port, host)
    url = f"http://{host}:{server.server_port}/"
    # flushed: the URL is the whole point of the command, and stdout is a
    # pipe whenever someone redirects or wraps it
    print(f"blame ui  {run.summary()}", flush=True)
    print(f"          {url}   (ctrl-c to stop)", flush=True)
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
