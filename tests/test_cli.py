"""The command line is most users' first contact with this tool.

Every test drives `main(argv)` in-process and reads what it printed, so the
whole file is covered by the ordinary suite without spawning a subprocess. The
error paths matter as much as the successful ones: a traceback at a mistyped
row number tells the user nothing they can act on.
"""

from __future__ import annotations

import pandas as pd
import pytest

import blame
from blame.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Two runs over the same pipeline, so `diff` has something to compare."""
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame(
        {
            "cust": ["a", "b", "a", "b"],
            "qty": [1, 5, 3, 9],
            "amt": [10.0, 20, 30, 40],
        }
    )
    ids = []
    for label in ("first", "second"):
        with blame.trace(label=label, root="./bl") as handle:
            kept = df[df.qty > 2]
            kept.groupby("cust", as_index=False)["amt"].sum()
        ids.append(handle.run.run_id)
    return ids


def run(capsys, *argv):
    """Call the CLI and hand back (exit code, stdout, stderr)."""
    code = main(["--root", "./bl", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- the successful paths -------------------------------------------------


def test_runs_lists_every_recorded_run(store, capsys):
    code, out, _ = run(capsys, "runs")
    assert code == 0
    for run_id in store:
        assert run_id in out
    assert "first" in out and "second" in out


def test_steps_shows_the_operations_with_line_numbers(store, capsys):
    code, out, _ = run(capsys, "steps")
    assert code == 0
    assert "filter" in out and "groupby.sum" in out
    assert "test_cli.py:" in out, "a step should say which line of the user's code it came from"


def test_why_names_the_source_rows(store, capsys):
    code, out, _ = run(capsys, "why", "0")
    assert code == 0
    assert "why(" in out
    assert "source rows:" in out


def test_why_show_prints_the_source_frames(store, capsys):
    code, out, _ = run(capsys, "why", "0", "--show", "3")
    assert code == 0
    assert "cust" in out and "qty" in out, "--show should print the source frame itself"


def test_forward_traces_a_source_row_through(store, capsys):
    code, out, _ = run(capsys, "forward", "3")
    assert code == 0
    assert "f0" in out and "row(s)" in out


def test_at_prints_the_frame_a_step_produced(store, capsys):
    code, out, _ = run(capsys, "at", "0")
    assert code == 0
    assert "step 0:" in out and "filter" in out


def test_diff_compares_the_two_newest_runs(store, capsys):
    code, out, _ = run(capsys, "diff")
    assert code == 0
    assert store[0][:8] in out and store[1][:8] in out


def test_diff_accepts_two_run_ids(store, capsys):
    code, out, _ = run(capsys, "diff", store[0], store[1])
    assert code == 0
    assert "no differences" in out or "differences" in out


def test_run_selects_an_older_run(store, capsys):
    code, out, _ = run(capsys, "--run", store[0], "steps")
    assert code == 0
    assert "first" in out


def test_version_prints_the_installed_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert blame.__version__ in capsys.readouterr().out


def test_ui_is_dispatched_without_opening_a_browser(store, monkeypatch, capsys):
    """`serve` blocks forever, so the check is that the CLI reaches it with the
    arguments the flags imply."""
    seen = {}

    def fake_serve(run_obj, port, open_browser):
        seen.update(port=port, open_browser=open_browser)

    monkeypatch.setattr("blame.ui.serve", fake_serve)
    code, _, _ = run(capsys, "ui", "--port", "7999", "--no-open")
    assert code == 0
    assert seen == {"port": 7999, "open_browser": False}


# -- the paths where the user got something wrong -------------------------


def test_no_runs_says_so_rather_than_crashing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main(["--root", "./empty", "runs"])
    assert code == 1
    assert "no runs recorded" in capsys.readouterr().out


def test_an_unknown_target_is_reported_not_raised(store, capsys):
    code, _, err = run(capsys, "why", "0", "--target", "nosuchframe")
    assert code == 1
    assert "nosuchframe" in err
    assert "known frames" in err, "the message should say what the caller could have typed"


def test_a_step_that_does_not_exist_is_reported_not_raised(store, capsys):
    code, _, err = run(capsys, "at", "999")
    assert code == 1
    assert "no step 999" in err
    assert "this run has 2 steps" in err


def test_a_row_that_does_not_exist_is_reported_not_raised(store, capsys):
    code, _, err = run(capsys, "why", "999")
    assert code == 1
    assert "Traceback" not in err
    assert "no row 999" in err and "row(s)" in err, (
        f"the message should name the frame and its size, got {err!r}"
    )


def test_forward_refuses_a_row_the_source_does_not_have(store, capsys):
    """`blame forward 99` on a four-row input used to print `[99]` as though
    that row existed."""
    code, out, err = run(capsys, "forward", "99")
    assert code == 1, f"it answered instead of refusing:\n{out}"
    assert "no row 99" in err


def test_forward_accepts_a_negative_row(store, capsys):
    """-1 is the last row, as everywhere else in Python."""
    _, last, _ = run(capsys, "forward", "3")
    code, out, err = run(capsys, "forward", "-1")
    assert code == 0, err
    assert out == last


def test_a_missing_store_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main(["--root", "./nowhere", "steps"])
    assert code == 1
    assert "blame:" in capsys.readouterr().err


def test_diff_needs_two_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"a": [1, 2, 3]})
    with blame.trace(root="./bl"):
        _ = df[df.a > 1]
    code = main(["--root", "./bl", "diff"])
    assert code == 1
    assert "need two recorded runs" in capsys.readouterr().err


def test_steps_surfaces_the_run_warnings(tmp_path, monkeypatch, capsys):
    """A warning recorded during tracing is the difference between an exact
    answer and an approximate one, so it belongs in front of the reader rather
    than only in the run file."""
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    with blame.trace(root="./bl"):
        df.apply(lambda row: row.a + row.b, axis=1)
    code = main(["--root", "./bl", "steps"])
    out = capsys.readouterr().out
    assert code == 0
    assert " ! " in out, f"the approximate-lineage warning was not shown:\n{out}"


def test_at_says_how_many_rows_it_did_not_print(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    df = pd.DataFrame({"a": range(50)})
    with blame.trace(root="./bl"):
        _ = df[df.a > 1]
    code = main(["--root", "./bl", "at", "0", "--rows", "5"])
    out = capsys.readouterr().out
    assert code == 0
    assert "more rows" in out


def test_a_numeric_target_is_read_as_a_step_index(store, capsys):
    """`--target 1` means step 1, not a frame called "1"."""
    code, out, err = run(capsys, "why", "0", "--target", "1")
    assert code == 0, err
    assert "why(" in out
