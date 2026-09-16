"""The HTTP surface behind `blame ui`.

Driven against a real server on an ephemeral port, in-process. What matters is
not only that each endpoint answers, but what happens to bad input: a local UI
is long-lived, and a handler that raises takes the page down with it. Every
failure here has to come back as JSON with the server still serving.

This is also the code path in the demo GIF at the top of the README.
"""

import contextlib
import json
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

import blame
from blame.ui import make_server


def _get(base: str, path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=30) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:  # 4xx and 5xx carry a body we want
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _json(base: str, path: str) -> tuple[int, dict]:
    status, body, content_type = _get(base, path)
    assert content_type == "application/json", f"{path} answered {content_type}"
    return status, json.loads(body)


@contextlib.contextmanager
def _serving(run):
    """`run`, served on an ephemeral port, for the duration of the block."""
    server = make_server(run, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """A served run, and the base URL it is served at."""
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"a": [1, 2, 3, 4], "b": [10.0, 20, 30, 40]})
    with blame.trace("ui") as handle:
        kept = df[df.a > 1]
        out = kept.groupby("a", as_index=False)["b"].sum()

    with _serving(handle.run) as base:
        yield base, handle.run


def test_the_page_itself_is_served(ui):
    base, _ = ui
    status, body, content_type = _get(base, "/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert b"<" in body and len(body) > 1000, "that is not the UI page"


def test_the_run_endpoint_describes_the_whole_pipeline(ui):
    base, run = ui
    status, payload = _json(base, "/api/run")
    assert status == 200
    assert payload["run_id"] == run.run_id
    assert payload["label"] == "ui"
    assert set(payload["nodes"]) == set(run.nodes)
    assert [s["idx"] for s in payload["steps"]] == [s.idx for s in run.steps]
    assert payload["result"] == run.result_fid


def test_a_frame_comes_back_as_rows_and_columns(ui):
    base, run = ui
    fid = run.sources[0]
    status, payload = _json(base, f"/api/frame?fid={fid}")
    assert status == 200
    assert payload["columns"] == ["a", "b"]
    assert payload["rows"] == [[1, 10.0], [2, 20.0], [3, 30.0], [4, 40.0]]
    assert payload["nrows"] == 4


def test_a_frame_can_be_paged(ui):
    """The grid asks for one window at a time; `offset` and `limit` have to
    mean what the page thinks they mean or it shows the wrong rows."""
    base, run = ui
    fid = run.sources[0]
    _, payload = _json(base, f"/api/frame?fid={fid}&offset=2&limit=1")
    assert payload["offset"] == 2
    assert payload["rows"] == [[3, 30.0]]


def test_why_answers_with_the_rows_that_made_a_row(ui):
    base, run = ui
    status, payload = _json(base, f"/api/why?fid={run.result_fid}&rows=0")
    assert status == 200
    assert payload["rows"] == [0]
    assert payload["hops"], "no hops: the answer explains nothing"
    # The same question the CLI and the library answer, and it must agree.
    expected = run.why(row=0).sources[run.sources[0]].tolist()
    assert payload["sources"][run.sources[0]]["rows"] == expected


def test_an_unknown_path_is_a_404_not_a_stack_trace(ui):
    base, _ = ui
    status, payload = _json(base, "/api/nope")
    assert status == 404
    assert payload == {"error": "not found"}


@pytest.mark.parametrize("endpoint", ["/api/frame", "/api/why"])
def test_a_missing_fid_is_the_callers_mistake(ui, endpoint):
    """400, not 500. The parameter is required and absent, which the caller can
    fix; a 500 would say the server is broken."""
    base, _ = ui
    status, payload = _json(base, endpoint)
    assert status == 400
    assert payload == {"error": "fid is required"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/frame?fid=does-not-exist",
        "/api/why?fid=does-not-exist&rows=0",
        "/api/why?fid={result}&rows=abc",
        "/api/why?fid={result}&rows=999",
        "/api/frame?fid={source}&offset=nonsense",
    ],
)
def test_bad_input_is_json_and_the_server_keeps_serving(ui, path):
    """The resilience this asserts is a design decision, not an accident: the
    handler catches everything so that one malformed request cannot end the
    session. Nothing protected it until now.
    """
    base, run = ui
    status, payload = _json(base, path.format(result=run.result_fid, source=run.sources[0]))
    assert status == 500
    assert "error" in payload, payload
    assert ":" in payload["error"], "the error should name the exception type"

    # The whole point: the next request still works.
    assert _json(base, "/api/run")[0] == 200


def test_a_row_that_does_not_exist_says_so(ui):
    """Rather than reporting a row the frame does not have. The message is the
    one the library raises, which names the frame and its size."""
    base, run = ui
    _, payload = _json(base, f"/api/why?fid={run.result_fid}&rows=999")
    assert "no row 999" in payload["error"]
    assert "IndexError" in payload["error"]


def test_the_server_refuses_to_start_without_its_page(ui, monkeypatch):
    """`make_server` checks the asset up front: a UI that binds a port and then
    404s the page is worse than one that does not start."""
    _, run = ui
    monkeypatch.setattr(blame.ui, "_ASSET", blame.ui._ASSET.parent / "missing.html")
    with pytest.raises(FileNotFoundError, match="UI asset missing"):
        make_server(run, port=0)


def test_every_kind_of_cell_survives_the_trip_to_json(tmp_path, monkeypatch):
    """The payload is written with `allow_nan=False`, so a NaN reaching
    `json.dumps` raises inside the handler and the grid shows an error instead
    of the frame. NaN, infinity and NaT are ordinary contents of a real
    DataFrame, and all three have to arrive as null.
    """
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame(
        {
            "i": [1, 2],
            "f": [float("nan"), float("inf")],
            "b": [True, False],
            "s": ["x", None],
            "t": pd.to_datetime(["2020-01-01", None]),
        }
    )
    with blame.trace() as handle:
        out = df[df.i > 0]

    with _serving(handle.run) as base:
        status, payload = _json(base, f"/api/frame?fid={handle.run.result_fid}")

    assert status == 200
    first, second = payload["rows"]
    assert first == [1, None, True, "x", "2020-01-01 00:00:00"]
    assert second == [2, None, False, None, None]


def test_a_busy_port_falls_through_to_the_next(ui):
    """`blame ui` twice in two terminals should open two pages, not fail."""
    base, run = ui
    taken = int(base.rsplit(":", 1)[1])
    server = make_server(run, port=taken)
    try:
        assert taken < server.server_port <= taken + 19
    finally:
        server.server_close()
