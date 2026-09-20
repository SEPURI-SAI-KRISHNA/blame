"""The public surface, exercised the way the documentation writes it.

The suite reaches `why()` and `diff()` through the objects `trace()` yields,
because that is convenient to write. The README reaches them through the
module -- `blame.last_run().why(...)` -- and that is what every reader runs
first. Those are different code paths, and only one of them had tests.
"""

import re
from pathlib import Path

import pandas as pd
import pytest

import blame

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _csvs() -> None:
    """The two files the README's example reads, written so that customer 11
    appears twice -- which is the duplicate the example is about."""
    pd.DataFrame(
        {
            "order_id": [1, 2, 3, 4],
            "customer_id": [10, 11, 12, 10],
            "qty": [2, 1, 0, 5],
            "total": [20.0, 25.0, 8.0, 40.0],
        }
    ).to_csv("orders.csv", index=False)
    pd.DataFrame(
        {"customer_id": [10, 11, 12, 11], "region": ["north", "south", "north", "south"]}
    ).to_csv("customers.csv", index=False)


def _readme_example() -> str:
    """The first Python block in the README, verbatim.

    Extracted rather than copied so the test fails when the README drifts. A
    snippet nobody runs is a snippet that stops being true, and this one is the
    first thing a reader executes.
    """
    blocks = re.findall(r"```python\n(.*?)```", ROOT.joinpath("README.md").read_text(), re.S)
    assert blocks, "the README no longer has a Python example"
    return blocks[0]


def test_the_readme_example_runs_and_says_what_the_readme_says():
    _csvs()
    exec(compile(_readme_example(), "README.md", "exec"), {})

    # The claim under the snippet: one order row, two customer rows, because
    # customers.csv holds customer_id 11 twice.
    explanation = blame.last_run().why(row=1, col="total")
    sources = {
        blame.last_run().nodes[fid].label: rows.tolist()
        for fid, rows in explanation.sources.items()
    }
    assert sources == {"read_csv('orders.csv')": [1], "read_csv('customers.csv')": [1, 3]}
    assert not explanation.approximate


def test_module_level_why_is_the_run_objects_why():
    _csvs()
    with blame.trace() as handle:
        orders = pd.read_csv("orders.csv")
        kept = orders[orders.qty > 0]
        out = kept.groupby("customer_id", as_index=False)["total"].sum()

    assert len(out) == 2
    direct = handle.run.why(row=0)
    through_module = blame.why(row=0)
    assert through_module.sources.keys() == direct.sources.keys()
    for fid, rows in direct.sources.items():
        assert through_module.sources[fid].tolist() == rows.tolist()


def test_module_level_forward_is_the_run_objects_forward():
    _csvs()
    with blame.trace() as handle:
        orders = pd.read_csv("orders.csv")
        kept = orders[orders.qty > 0]

    assert len(kept) == 3
    assert blame.forward(row=0) == pytest.approx(handle.run.forward(row=0))


def test_last_run_is_the_run_just_recorded():
    _csvs()
    with blame.trace("named") as handle:
        pd.read_csv("orders.csv")

    assert blame.last_run().run_id == handle.run.run_id
    assert blame.last_run().label == "named"


def test_load_reads_a_run_back_by_its_id():
    _csvs()
    with blame.trace("persisted") as handle:
        orders = pd.read_csv("orders.csv")
        orders[orders.qty > 0]

    reloaded = blame.load(handle.run.run_id)
    assert reloaded.run_id == handle.run.run_id
    assert reloaded.label == "persisted"
    assert [s.op for s in reloaded.steps] == [s.op for s in handle.run.steps]


def test_diff_accepts_run_ids_as_strings():
    """`blame.diff` documents that either argument may be a run id. Nothing
    exercised that branch, so the two `Run.load` calls in it were dead code as
    far as the suite was concerned."""
    _csvs()

    def run(qty):
        with blame.trace() as handle:
            orders = pd.read_csv("orders.csv")
            orders.loc[0, "qty"] = qty
            kept = orders[orders.qty > 0]
            kept.groupby("customer_id", as_index=False)["total"].sum()
        return handle.run

    before, after = run(2), run(7)

    by_object = blame.diff(before, after, on=["customer_id"])
    by_id = blame.diff(before.run_id, after.run_id, on=["customer_id"])
    assert [c.key for c in by_id.changes] == [c.key for c in by_object.changes]


def test_the_run_is_not_available_until_the_block_has_exited():
    """Reaching for `handle.run` inside the `with` is the obvious mistake, and
    the answer would be a half-recorded run. It says so instead."""
    _csvs()
    with blame.trace() as handle:
        pd.read_csv("orders.csv")
        with pytest.raises(RuntimeError, match="has exited"):
            _ = handle.run


def test_last_run_falls_back_to_the_most_recent_run_on_disk():
    """A fresh process has no run in memory. `blame.why(...)` still has to work
    there -- opening a shell after the pipeline ran is the common case."""
    _csvs()
    with blame.trace("on-disk") as handle:
        orders = pd.read_csv("orders.csv")
        orders[orders.qty > 0]
    recorded = handle.run.run_id

    monkeyed = blame._LAST
    try:
        blame._LAST = None  # as if this were a new interpreter
        assert blame.last_run().run_id == recorded
    finally:
        blame._LAST = monkeyed


def test_stopping_a_trace_that_is_not_running_returns_nothing():
    assert blame.stop() is None
