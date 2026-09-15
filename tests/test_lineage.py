"""Correctness tests.

The important ones use ground truth rather than assertions about our own
output: a hidden column carries each source row's id through the pipeline,
pandas propagates it, and we check that why() returns exactly that set.
"""

import subprocess
import sys
import time

import numpy as np
import pandas as pd
import pytest

import blame


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _orders(n=40, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "id": np.arange(n),
            "cust": rng.integers(0, 7, n),
            "qty": rng.integers(-2, 6, n),
            "price": rng.integers(1, 50, n).astype(float),
        }
    )


# -- ground truth ---------------------------------------------------------


def test_filter_join_groupby_matches_ground_truth():
    orders = _orders()
    customers = pd.DataFrame({"cust": [0, 1, 2, 3, 4, 5, 6, 1], "region": list("nsewnsen")})
    truth_orders = orders.assign(_src=np.arange(len(orders)))

    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        report = joined.groupby("region", as_index=False)["total"].sum()

    # independent computation of which order rows feed each region
    t_clean = truth_orders[truth_orders.qty > 0]
    t_join = t_clean.merge(customers.assign(_csrc=np.arange(len(customers))), on="cust")
    expected = t_join.groupby("region")["_src"].apply(lambda s: sorted(set(s)))
    expected_c = t_join.groupby("region")["_csrc"].apply(lambda s: sorted(set(s)))

    run = h.run
    for i, region in enumerate(report["region"]):
        exp = run.why(row=i, col="total")
        got_orders = sorted(exp.sources[run.sources[0]].tolist())
        assert got_orders == expected[region], f"{region}: orders"
        cust_fid = [f for f in exp.sources if f != run.sources[0]][0]
        assert sorted(exp.sources[cust_fid].tolist()) == expected_c[region], f"{region}: customers"


@pytest.mark.parametrize("how", ["inner", "left", "right", "outer"])
def test_merge_lineage_exact_for_all_join_types(how):
    left = pd.DataFrame({"k": [1, 2, 2, 3], "lv": list("abcd")})
    right = pd.DataFrame({"k": [2, 3, 4], "rv": list("xyz")})
    tl = left.assign(_l=np.arange(len(left)))
    tr = right.assign(_r=np.arange(len(right)))

    with blame.trace() as h:
        out = left.merge(right, how=how, on="k")

    truth = tl.merge(tr, how=how, on="k")
    run = h.run
    lfid, rfid = run.steps[-1].inputs
    for i in range(len(out)):
        exp = run.why(row=i)
        for fid, col in ((lfid, "_l"), (rfid, "_r")):
            want = truth[col].iloc[i]
            want = [] if pd.isna(want) else [int(want)]
            assert sorted(exp.sources.get(fid, np.array([])).tolist()) == want


def test_sort_and_head_are_exact():
    df = _orders(20, seed=3)
    with blame.trace() as h:
        out = df.sort_values("price", ascending=False).head(5)
    run = h.run
    truth = df.sort_values("price", ascending=False).head(5).index.tolist()
    for i in range(5):
        assert h.run.why(row=i).sources[run.sources[0]].tolist() == [truth[i]]


def test_concat_lineage():
    a = pd.DataFrame({"v": [1, 2, 3]})
    b = pd.DataFrame({"v": [4, 5]})
    with blame.trace() as h:
        out = pd.concat([a, b], ignore_index=True)
    run = h.run
    afid, bfid = run.steps[-1].inputs
    assert run.why(row=1).sources == {afid: np.array([1])} or run.why(row=1).sources[
        afid
    ].tolist() == [1]
    assert run.why(row=4).sources[bfid].tolist() == [1]


def test_multikey_groupby():
    df = pd.DataFrame({"a": list("xxyy"), "b": list("pqpq"), "v": [1, 2, 3, 4]})
    with blame.trace() as h:
        out = df.groupby(["a", "b"])["v"].sum()
    run = h.run
    src = run.sources[0]
    for i, key in enumerate(out.index):
        want = sorted(df.index[(df.a == key[0]) & (df.b == key[1])].tolist())
        assert sorted(run.why(row=i).sources[src].tolist()) == want


def test_drop_duplicates_and_dropna():
    df = pd.DataFrame({"v": [1, 1, 2, None, 3]})
    with blame.trace() as h:
        out = df.dropna().drop_duplicates()
    run = h.run
    src = run.sources[0]
    assert run.why(row=0).sources[src].tolist() == [0]
    assert run.why(row=1).sources[src].tolist() == [2]
    assert run.why(row=2).sources[src].tolist() == [4]


# -- forward --------------------------------------------------------------


def test_forward_is_the_inverse_of_why():
    orders = _orders(30, seed=1)
    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        report = clean.groupby("cust", as_index=False)["price"].sum()
    run = h.run
    src = run.sources[0]
    result = run.result_fid
    for row in range(len(orders)):
        reached = run.forward(row=row, target=src).get(result, np.array([]))
        for out_row in reached:
            back = run.why(row=int(out_row)).sources[src]
            assert row in back


# -- honesty --------------------------------------------------------------


def test_apply_is_flagged_approximate():
    df = _orders(10)
    with blame.trace() as h:
        out = df.assign(z=df.qty).apply(lambda r: r, axis=1)
    run = h.run
    assert any(s.approximate for s in run.steps)
    assert run.why(row=0).approximate is True


def test_exact_path_is_not_flagged():
    df = _orders(10)
    with blame.trace() as h:
        out = df[df.qty > 0].sort_values("price")
    assert h.run.why(row=0).approximate is False


# -- plumbing -------------------------------------------------------------


def test_pandas_is_restored_and_results_unchanged():
    df = _orders(10)
    before = df[df.qty > 0].copy()
    getitem_before = pd.DataFrame.__getitem__
    merge_before = pd.merge
    with blame.trace():
        after = df[df.qty > 0]
    pd.testing.assert_frame_equal(before, after)
    assert pd.DataFrame.__getitem__ is getitem_before, "pandas not restored after trace"
    assert pd.merge is merge_before


def test_run_survives_a_round_trip_to_disk():
    orders = _orders(25, seed=5)
    with blame.trace("persisted") as h:
        out = orders[orders.qty > 0].groupby("cust", as_index=False)["price"].sum()
    live = h.run.why(row=0)
    reloaded = blame.load(h.run.run_id)
    assert reloaded.label == "persisted"
    assert reloaded.why(row=0).sources.keys() == live.sources.keys()
    for fid, rows in live.sources.items():
        assert reloaded.why(row=0).sources[fid].tolist() == rows.tolist()


def test_at_returns_intermediate_frames():
    orders = _orders(12)
    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        out = clean.assign(total=clean.qty * clean.price)
    run = h.run
    major = [s.idx for s in run.steps if not s.minor]
    frame = run.at(major[0])
    assert len(frame) == len(orders[orders.qty > 0])


def test_column_dedup_stores_untouched_columns_once():
    df = _orders(50)
    with blame.trace(sample_rows=10_000) as h:
        out = df.assign(extra=1).assign(extra2=2)
    usage = h.run.store.disk_usage()["columns"]
    # 4 original columns stored once each, reused by both assigns
    hashes = {c.hash for n in h.run.nodes.values() for c in n.data.columns}
    assert len(hashes) <= 4 + 2 + 1
    assert usage > 0


# -- materialization ------------------------------------------------------


def test_referenced_columns_rebuild_the_real_intermediates():
    """Most columns are stored as references to a parent. Rebuilding them must
    reproduce the frame the pipeline actually held at that step."""
    orders = _orders(60, seed=7)
    customers = pd.DataFrame({"cust": range(7), "region": list("nsewnse")})

    truth = {}
    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        truth["filter"] = clean.copy()
        clean = clean.assign(total=clean.qty * clean.price)
        truth["assign"] = clean.copy()
        joined = clean.merge(customers, on="cust")
        truth["merge"] = joined.copy()
        ordered = joined.sort_values("total")
        truth["sort_values"] = ordered.copy()

    run = h.run
    for step in run.steps:
        if step.op not in truth:
            continue
        got = run.at(step.idx)
        want = truth[step.op].reset_index(drop=True)
        pd.testing.assert_frame_equal(
            got.reset_index(drop=True), want[got.columns], check_dtype=False
        )


def test_outer_join_unmatched_rows_materialize_as_na():
    left = pd.DataFrame({"k": [1, 2], "lv": ["a", "b"]})
    right = pd.DataFrame({"k": [2, 3], "rv": ["x", "y"]})
    with blame.trace() as h:
        out = left.merge(right, how="outer", on="k")
    got = h.run.at(h.run.steps[-1].idx).reset_index(drop=True)
    assert got["lv"].isna().sum() == out["lv"].isna().sum()
    assert got["rv"].isna().sum() == out["rv"].isna().sum()


def test_storage_shrinks_when_columns_are_references():
    orders = _orders(20_000, seed=2)
    customers = pd.DataFrame({"cust": range(1000), "region": ["n"] * 1000})
    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        joined.sort_values("total").head(100)
    stored = sum(1 for n in h.run.nodes.values() for c in n.data.columns if c.kind == "data")
    referenced = sum(1 for n in h.run.nodes.values() for c in n.data.columns if c.kind == "ref")
    assert referenced > stored, f"{referenced} referenced vs {stored} stored"


def test_sampling_keeps_lineage_exact_and_says_so():
    df = _orders(500, seed=11)
    with blame.trace(sample_rows=100) as h:
        out = df[df.qty > 0].sort_values("price")
    run = h.run
    assert any("sample_rows" in w for w in run.warnings)
    # lineage is unaffected by how much data we chose to keep
    truth = df[df.qty > 0].sort_values("price").index.tolist()
    assert run.why(row=0).sources[run.sources[0]].tolist() == [truth[0]]


# -- untraced operations must not masquerade as sources -------------------


def test_untraced_op_becomes_an_explicit_approximate_step():
    """An operation we do not cover must break the chain loudly: an explicit
    step, an approximate flag, and the real source still named. Silently
    reporting an intermediate as a pipeline input is the worst thing this tool
    could do."""
    df = pd.DataFrame({"a": list("xxyy"), "b": list("pqpq"), "v": [1, 2, 3, 4]})
    with blame.trace() as h:
        kept = df[df.v > 0]
        flipped = kept.T  # transpose has no row-level answer
        out = flipped.reset_index()
    run = h.run
    exp = run.why(row=0)

    assert exp.approximate is True
    assert any(s.op.startswith("<untraced") and s.approximate for s in run.steps)
    # the true source is named, not an anonymous intermediate
    labels = {run.nodes[f].label for f in exp.sources}
    assert labels == {"df"}, labels
    assert any("not traced" in w for w in run.warnings)


def test_genuine_sources_are_still_exact():
    """The fix must not turn ordinary inputs into approximate ones."""
    orders = _orders(20, seed=4)
    customers = pd.DataFrame({"cust": range(7), "region": list("nsewnse")})
    with blame.trace() as h:
        out = orders[orders.qty > 0].merge(customers, on="cust")
    run = h.run
    exp = run.why(row=0)
    assert exp.approximate is False
    assert not any(s.op.startswith("<untraced") for s in run.steps)
    assert {run.nodes[f].label for f in exp.sources} == {"orders", "customers"}


def test_frame_created_during_the_trace_is_a_source_not_an_orphan():
    orders = _orders(15, seed=6)
    with blame.trace() as h:
        lookup = pd.DataFrame({"cust": range(7), "tier": list("aabbccd")})
        out = orders.merge(lookup, on="cust")
    run = h.run
    assert not any(s.op.startswith("<untraced") for s in run.steps)
    assert run.why(row=0).approximate is False


def test_in_place_column_assignment_keeps_row_lineage():
    """`df["x"] = ...` is not recorded as a step, but the rows it produced are
    still traceable, because later operations map back through the index."""
    df = pd.DataFrame({"v": [1, 2, 3, 4]})
    with blame.trace() as h:
        kept = df[df.v > 1]
        kept["doubled"] = kept.v * 2
        out = kept[kept.doubled > 4]
    run = h.run
    truth = df[df.v > 1].assign(doubled=lambda d: d.v * 2)
    truth = truth[truth.doubled > 4].index.tolist()
    assert run.why(row=0).sources[run.sources[0]].tolist() == [truth[0]]


# -- the graph the UI draws ------------------------------------------------


def test_internal_column_access_does_not_invent_a_source():
    """Deriving lineage uses pandas itself. None of that may leak into the
    graph -- reading group keys back off an aggregation once registered the
    result a second time, as an <external> source with nothing above it."""
    orders = _orders()
    customers = pd.DataFrame({"cust": list(range(7)), "region": list("nsewnse")})

    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        joined.groupby("region", as_index=False)["total"].sum()

    labels = {h.run.nodes[f].label for f in h.run.sources}
    assert labels == {"orders", "customers"}
    assert not [n for n in h.run.nodes.values() if n.label == "<external>"]


def test_highlight_covers_the_whole_path_not_just_the_ends():
    orders = _orders()
    customers = pd.DataFrame({"cust": list(range(7)), "region": list("nsewnse")})

    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        joined = clean.merge(customers, on="cust")
        report = joined.groupby("region", as_index=False)["qty"].sum()

    run = h.run
    marks = run.highlight(row=0, target=run.result_fid)
    why = run.why(row=0, target=run.result_fid)

    # the sources agree with why, and every intermediate on the path is there too
    for fid, rows in why.sources.items():
        assert np.array_equal(marks[fid], rows)
    on_path = {s.output for s in run.steps if s.output in marks}
    assert len(on_path) >= 3, "intermediates should be marked, not only the ends"

    # and the marked rows really are the ones that carry the answer
    region = report.loc[0, "region"]
    expected = np.flatnonzero((joined["region"] == region).to_numpy())
    joined_fid = [s.output for s in run.steps if s.op == "merge"][0]
    assert np.array_equal(marks[joined_fid], expected)


def test_highlight_from_a_source_row_reaches_downstream():
    orders = _orders()
    with blame.trace() as h:
        kept = orders[orders.qty > 0]
        kept.sort_values("price")

    run = h.run
    keep = np.flatnonzero((orders.qty > 0).to_numpy())
    marks = run.highlight(row=int(keep[0]), target=run.sources[0])
    reached = run.forward(row=int(keep[0]), target=run.sources[0])
    for fid, rows in reached.items():
        assert set(rows.tolist()) <= set(marks[fid].tolist())
    assert len(marks) > 1, "a kept source row must appear downstream"


def test_ui_payloads_are_json_safe_and_agree_with_the_api():
    import json

    from blame import ui

    orders = _orders()
    orders.loc[3, "price"] = np.nan
    with blame.trace("ui") as h:
        kept = orders[orders.qty > 0]
        kept.groupby("cust", as_index=False)["qty"].sum()

    run = h.run
    structure = json.loads(json.dumps(ui.run_payload(run), allow_nan=False))
    assert structure["result"] == run.result_fid
    assert set(structure["nodes"]) == set(run.nodes)

    page = json.loads(json.dumps(ui.frame_payload(run, run.sources[0]), allow_nan=False))
    assert page["nrows"] == len(orders)
    assert page["rows"][3][page["columns"].index("price")] is None  # NaN -> null

    answer = json.loads(json.dumps(ui.explain_payload(run, run.result_fid, [0]), allow_nan=False))
    truth = run.highlight(row=0, target=run.result_fid)
    assert {f: m["rows"] for f, m in answer["marks"].items()} == {
        f: r.tolist() for f, r in truth.items()
    }


def test_ui_server_answers_the_three_endpoints():
    import json
    import threading
    import urllib.error
    import urllib.request

    from blame import ui

    orders = _orders()
    with blame.trace("served") as h:
        orders[orders.qty > 0].sort_values("price")

    server = ui.make_server(h.run, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:

        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.read()

        assert b"<title>blame</title>" in get("/")
        structure = json.loads(get("/api/run"))
        fid = structure["result"]
        page = json.loads(get(f"/api/frame?fid={fid}&offset=0&limit=5"))
        assert len(page["rows"]) == 5
        answer = json.loads(get(f"/api/why?fid={fid}&rows=0&col=price"))
        assert answer["marks"][h.run.sources[0]]["n"] == 1
        with pytest.raises(urllib.error.HTTPError) as missing:
            get("/api/nope")
        assert json.loads(missing.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- found by running on third-party code ----------------------------------


def test_resample_matches_ground_truth():
    """resample is a group-by over time bins. It used to be untraced, and the
    result frame never entered the graph at all."""
    idx = pd.date_range("2024-01-01", periods=40, freq="6h")
    df = pd.DataFrame({"v": np.arange(40.0), "site": list("ab") * 20}, index=idx)

    with blame.trace() as h:
        daily = df.resample("D")["v"].sum()

    run = h.run
    assert len(run.frame(run.result_fid)) == len(daily), "the result must be the traced frame"
    for out_row in range(len(daily)):
        day = daily.index[out_row]
        truth = np.flatnonzero(df.index.normalize() == day)
        got = run.why(row=out_row).sources[run.sources[0]]
        assert np.array_equal(got, truth), f"row {out_row}"


def test_resample_without_column_selection_is_traced():
    idx = pd.date_range("2024-03-01", periods=12, freq="8h")
    df = pd.DataFrame({"v": np.arange(12.0)}, index=idx)
    with blame.trace() as h:
        daily = df.resample("D").sum()
    truth = np.flatnonzero(df.index.normalize() == df.index[0].normalize())
    assert np.array_equal(h.run.why(row=0).sources[h.run.sources[0]], truth)
    assert len(daily) == 4


def test_duplicate_index_does_not_force_approximate_lineage():
    """A duplicated index is everywhere in real data -- after concat, melt, or
    just reading a file keyed on a non-unique column."""
    df = pd.DataFrame({"city": list("aabbcc"), "v": [5, 3, 9, 1, 7, 2]}, index=[0, 0, 1, 1, 2, 2])
    with blame.trace() as h:
        top = df.head(3)
        srt = df.sort_values("v")

    steps = {s.op: s for s in h.run.steps if not s.minor}
    assert not steps["head"].approximate
    assert not steps["sort_values"].approximate

    head_fid = steps["head"].output
    assert np.array_equal(h.run.why(row=2, target=head_fid).sources[h.run.sources[0]], [2])
    sort_fid = steps["sort_values"].output
    # sorted by v, so output row 0 is the smallest value: v=1, at position 3
    assert np.array_equal(h.run.why(row=0, target=sort_fid).sources[h.run.sources[0]], [3])
    assert srt.iloc[0]["v"] == 1 and len(top) == 3


def test_head_and_tail_are_positional():
    df = pd.DataFrame({"v": np.arange(10)}, index=list("aabbccddee"))
    with blame.trace() as h:
        first = df.head(3)
        last = df.tail(2)
    steps = {s.op: s.output for s in h.run.steps if not s.minor}
    src = h.run.sources[0]
    assert np.array_equal(h.run.why(row=[0, 1, 2], target=steps["head"]).sources[src], [0, 1, 2])
    assert np.array_equal(h.run.why(row=[0, 1], target=steps["tail"]).sources[src], [8, 9])
    assert len(first) == 3 and len(last) == 2


def test_label_slice_is_traced():
    """df["2019-05-20":"2019-05-21"] is not a positional slice."""
    idx = pd.date_range("2019-05-19", periods=6, freq="D")
    df = pd.DataFrame({"v": np.arange(6.0)}, index=idx)
    with blame.trace() as h:
        window = df["2019-05-20":"2019-05-21"]
    assert len(window) == 2
    assert not h.run.warnings, h.run.warnings
    step = [s for s in h.run.steps if not s.minor][-1]
    assert not step.approximate
    assert np.array_equal(h.run.why(row=0, target=step.output).sources[h.run.sources[0]], [1])


def test_plotting_does_not_enter_the_graph():
    """matplotlib builds frames of its own on the way to a figure. They are
    not steps in the user's pipeline."""
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")

    df = pd.DataFrame({"v": np.arange(10.0), "w": np.arange(10.0) * 2})
    with blame.trace() as h:
        kept = df[df.v > 2]
        kept.plot()
        kept.info()

    ops = [s.op for s in h.run.steps if not s.minor]
    assert ops == ["filter"], ops
    assert not any("untraced" in o for o in ops)


def test_why_can_be_asked_about_the_frame_itself():
    """Naming the frame beats guessing which leaf of the graph you meant."""
    orders = _orders()
    with blame.trace() as h:
        kept = orders[orders.qty > 0]
        report = kept.groupby("cust", as_index=False)["qty"].sum()

    run = h.run
    by_object = run.why(row=0, target=report)
    by_default = run.why(row=0)
    assert by_object.target_fid == by_default.target_fid
    assert run.why(row=0, target=kept).target_fid != by_object.target_fid
    assert f"{len(report)}x" in repr(by_object)  # the shape is stated


def test_asking_about_an_untraced_frame_says_so_instead_of_guessing():
    orders = _orders()
    with blame.trace() as h:
        orders[orders.qty > 0]
    outsider = pd.DataFrame({"a": [1, 2, 3]})
    with pytest.raises(KeyError, match="not in this run"):
        h.run.why(row=0, target=outsider)


# -- reshaping -------------------------------------------------------------


def _wide():
    return pd.DataFrame(
        {
            "city": ["ny", "ny", "la", "la", "sf", "sf"],
            "month": ["jan", "feb", "jan", "feb", "jan", "feb"],
            "temp": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "hum": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        }
    )


def test_melt_matches_ground_truth():
    """Carry a source id through as an id_var and check why() against it."""
    df = _wide().assign(_src=lambda d: np.arange(len(d)))
    with blame.trace() as h:
        long = df.melt(id_vars=["city", "month", "_src"], value_vars=["temp", "hum"])

    run, src = h.run, None
    src = run.sources[0]
    step = [s for s in run.steps if s.op == "melt"][0]
    assert not step.approximate
    assert len(long) == 2 * len(df)
    for row in range(len(long)):
        truth = [int(long.iloc[row]["_src"])]
        got = run.why(row=row, target=step.output).sources[src]
        assert np.array_equal(got, truth), f"row {row}"


def test_pivot_matches_ground_truth():
    df = _wide()
    with blame.trace() as h:
        wide = df.pivot(index="city", columns="month", values="temp")

    run, src = h.run, h.run.sources[0]
    step = [s for s in run.steps if s.op == "pivot"][0]
    assert not step.approximate
    for row, key in enumerate(wide.index):
        truth = np.flatnonzero((df["city"] == key).to_numpy())
        got = run.why(row=row, target=step.output).sources[src]
        assert np.array_equal(got, truth), f"row {row} ({key})"


def test_pivot_table_matches_ground_truth():
    df = _wide()
    with blame.trace() as h:
        table = df.pivot_table(index="city", columns="month", values="temp", aggfunc="sum")

    run, src = h.run, h.run.sources[0]
    step = [s for s in run.steps if s.op == "pivot_table"][0]
    assert not step.approximate
    for row, key in enumerate(table.index):
        truth = np.flatnonzero((df["city"] == key).to_numpy())
        assert np.array_equal(run.why(row=row, target=step.output).sources[src], truth)


def test_unstack_matches_ground_truth():
    df = _wide()
    with blame.trace() as h:
        stacked = df.set_index(["city", "month"])["temp"]
        wide = stacked.unstack("month")

    run = h.run
    step = [s for s in run.steps if s.op == "unstack"][0]
    assert not step.approximate
    src = run.sources[0]
    for row, key in enumerate(wide.index):
        truth = np.flatnonzero((df["city"] == key).to_numpy())
        assert np.array_equal(run.why(row=row, target=step.output).sources[src], truth), key


def test_reshape_survives_keys_it_cannot_read():
    """pivot_table with no index aggregates everything; there is no per-row
    key to read, so it must degrade loudly rather than invent one."""
    df = _wide()
    with blame.trace() as h:
        table = df.pivot_table(columns="month", values="temp", aggfunc="sum")
    step = [s for s in h.run.steps if s.op == "pivot_table"][0]
    assert step.approximate
    assert len(table) == 1


def test_pivot_without_an_index_argument_uses_the_frames_own_index():
    df = pd.DataFrame(
        {
            "city": ["ny", "ny", "la", "la"],
            "month": ["jan", "feb", "jan", "feb"],
            "temp": [1.0, 2.0, 3.0, 4.0],
        }
    ).set_index("city")
    with blame.trace() as h:
        wide = df.pivot(columns="month", values="temp")
    run, src = h.run, h.run.sources[0]
    step = [s for s in run.steps if s.op == "pivot"][0]
    assert not step.approximate
    for row, key in enumerate(wide.index):
        truth = np.flatnonzero(np.asarray(df.index == key))
        assert np.array_equal(run.why(row=row, target=step.output).sources[src], truth), key


def test_reshape_keys_are_positional_not_index_aligned():
    """A Series grouper is aligned on its own index. Passing one silently
    mismatched every reshape on a frame that was not RangeIndexed -- which is
    any frame read with index_col, i.e. most real ones."""
    n = 60
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    df = pd.DataFrame(
        {
            "site": np.repeat(list("abcd"), n // 4),
            "metric": np.tile(["no2", "pm25"], n // 2),
            "value": np.arange(float(n)),
        },
        index=idx,
    )
    assert not isinstance(df.index, pd.RangeIndex)

    with blame.trace() as h:
        table = df.pivot_table(values="value", index="site", columns="metric", aggfunc="mean")

    run, src = h.run, h.run.sources[0]
    step = [s for s in run.steps if s.op == "pivot_table"][0]
    assert not step.approximate
    for row, key in enumerate(table.index):
        truth = np.flatnonzero((df["site"] == key).to_numpy())
        got = run.why(row=row, target=step.output).sources[src]
        assert np.array_equal(got, truth), f"{key}: {len(got)} vs {len(truth)}"


def test_pivot_table_margins_row_spans_every_input_row():
    df = _wide()
    with blame.trace() as h:
        table = df.pivot_table(
            values="temp", index="city", columns="month", aggfunc="sum", margins=True
        )
    run, src = h.run, h.run.sources[0]
    step = [s for s in run.steps if s.op == "pivot_table"][0]
    total_row = list(table.index).index("All")
    got = run.why(row=total_row, target=step.output).sources[src]
    assert np.array_equal(got, np.arange(len(df)))


def test_multi_key_pivot_table():
    df = _wide()
    with blame.trace() as h:
        table = df.pivot_table(values="temp", index=["city", "month"], aggfunc="sum")
    run, src = h.run, h.run.sources[0]
    step = [s for s in run.steps if s.op == "pivot_table"][0]
    assert not step.approximate
    for row, key in enumerate(table.index):
        city, month = key
        truth = np.flatnonzero(((df["city"] == city) & (df["month"] == month)).to_numpy())
        assert np.array_equal(run.why(row=row, target=step.output).sources[src], truth), key


def test_pandas_internals_do_not_become_user_steps():
    """explode is implemented with reindex and take, which we patch. Recording
    those invents a pipeline the user never wrote -- and the seven fabricated
    steps it produced included approximate warnings about operations that
    appear nowhere in their code."""
    df = pd.DataFrame({"a": list("xxyy"), "v": [1.0, 2, 3, 4]})
    with blame.trace() as h:
        kept = df[df.v > 0]
        burst = kept.assign(l=[[1, 2]] * len(kept)).explode("l")
        out = burst.sort_values("v")

    run = h.run
    ops = [s.op for s in run.steps if not s.minor]
    assert ops == ["filter", "assign", "<untraced explode>", "sort_values"], ops

    # and the chain stays honest: widened, still reaching the real source
    exp = run.why(row=0, target=out)
    assert exp.approximate is True
    assert {run.nodes[f].label for f in exp.sources} == {"df"}
    assert any("explode" in w for w in run.warnings)


def test_untraced_chain_survives_collected_intermediates():
    """pandas discards the frames it builds mid-operation immediately, so the
    origin has to be resolved as the chain is built, not walked afterwards."""
    import gc

    df = pd.DataFrame({"g": list("aabb"), "v": [1.0, 2, 3, 4]})
    with blame.trace() as h:
        kept = df[df.v > 0]
        flipped = kept.T
        gc.collect()  # drop whatever pandas left behind
        out = flipped.reset_index()

    exp = h.run.why(row=0, target=out)
    assert exp.approximate is True
    assert {h.run.nodes[f].label for f in exp.sources} == {"df"}


# -- diffing two runs ------------------------------------------------------


def _sales(orders, customers):
    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        joined.groupby("region", as_index=False)["total"].sum()
    return h.run


def _books(qty3=4, extra=None, regions=None):
    orders = pd.DataFrame(
        {
            "order_id": [1, 2, 3, 4],
            "cust": [10, 11, 12, 10],
            "qty": [2, 1, qty3, 5],
            "price": [10.0, 25.0, 8.0, 4.0],
        }
    )
    if extra is not None:
        orders = pd.concat([orders, extra], ignore_index=True)
    customers = pd.DataFrame(
        {"cust": [10, 11, 12], "region": regions or ["north", "south", "north"]}
    )
    return orders, customers


def test_diff_attributes_a_changed_number_to_the_input_row_that_changed():
    before = _sales(*_books(qty3=4))
    after = _sales(*_books(qty3=9))
    d = before.diff(after, on=["region", "order_id"])

    assert d, "the totals moved, so the diff must not be empty"
    north = [c for c in d.changes if c.key == "north"]
    assert len(north) == 1 and north[0].kind == "changed"
    assert [c.column for c in north[0].cells] == ["total"]

    causes = north[0].causes
    assert [(c.source, c.key) for c in causes] == [("orders", 3)]
    assert [str(c) for c in causes[0].cells] == ["qty: 4.0 -> 9.0"]
    assert d.explained == len(d.changes)


def test_diff_reports_added_and_removed_input_rows():
    extra = pd.DataFrame({"order_id": [9], "cust": [11], "qty": [3], "price": [10.0]})
    before = _sales(*_books())
    after = _sales(*_books(extra=extra))
    d = before.diff(after, on=["region", "order_id"])

    south = [c for c in d.changes if c.key == "south"][0]
    assert [(c.source, c.key, c.kind) for c in south.causes] == [("orders", 9, "added")]

    # and the other direction: the row is gone in the second run
    back = after.diff(before, on=["region", "order_id"])
    south_back = [c for c in back.changes if c.key == "south"][0]
    assert [(c.key, c.kind) for c in south_back.causes] == [(9, "removed")]


def test_diff_of_identical_runs_is_empty():
    d = _sales(*_books()).diff(_sales(*_books()), on=["region", "order_id"])
    assert not d
    assert d.changes == []
    assert "no differences" in repr(d)


def test_diff_says_so_when_no_input_change_explains_the_output():
    """The data is identical and the numbers still moved, so the pipeline
    itself changed. Inventing a cause would be worse than admitting none."""
    orders, customers = _books()
    before = _sales(orders, customers)
    with blame.trace() as h:  # same inputs, different filter
        clean = orders[orders.qty > 1]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        joined.groupby("region", as_index=False)["total"].sum()
    after = h.run

    d = before.diff(after, on=["region", "order_id"])
    assert d.changes
    assert d.explained == 0
    assert "no changed input row explains this" in repr(d)


def test_diff_falls_back_per_frame_when_a_key_is_not_unique():
    before = _sales(*_books(qty3=4))
    after = _sales(*_books(qty3=9))
    d = before.diff(after, on=["region", "order_id"])
    assert "region" in d.aligned_by
    # customers has a region column too, but it repeats there
    assert "not being unique" in d.source_summary["customers"]


def test_diff_detects_added_and_removed_columns():
    orders, customers = _books()
    before = _sales(orders, customers)
    with blame.trace() as h:
        clean = orders[orders.qty > 0]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        out = joined.groupby("region", as_index=False)[["total", "qty"]].sum()
    d = before.diff(h.run, on=["region", "order_id"])
    assert d.gained_columns == ["qty"]
    assert len(out.columns) == 3


def test_unexplained_rows_is_the_assertion_for_a_test_suite():
    orders, customers = _books()
    before = _sales(orders, customers)
    after = _sales(orders, customers)
    assert before.diff(after, on=["region", "order_id"]).unexplained_rows() == []

    with blame.trace() as h:  # same data, changed pipeline
        clean = orders[orders.qty > 1]
        clean = clean.assign(total=clean.qty * clean.price)
        joined = clean.merge(customers, on="cust")
        joined.groupby("region", as_index=False)["total"].sum()
    assert before.diff(h.run, on=["region", "order_id"]).unexplained_rows()


def test_diff_compares_by_value_not_by_row_position():
    """Inserting a row at the top must not report every row below it as
    changed -- position is not identity across runs."""
    orders, customers = _books()
    before = _sales(orders, customers)
    shifted = pd.concat(
        [pd.DataFrame({"order_id": [0], "cust": [13], "qty": [1], "price": [1.0]}), orders],
        ignore_index=True,
    )
    customers2 = pd.concat(
        [customers, pd.DataFrame({"cust": [13], "region": ["west"]})], ignore_index=True
    )
    after = _sales(shifted, customers2)

    d = before.diff(after, on=["region", "order_id"])
    # north and south are untouched; only the new west region appears
    assert [(c.key, c.kind) for c in d.changes] == [("west", "added")]


def test_tracing_works_from_a_directory_whose_name_contains_pandas(tmp_path):
    """Frames were attributed to pandas by searching the path for "pandas".

    Any user working in ~/pandas-tutorial -- or in an unpacked pandas_blame
    sdist -- had every operation classified as a pandas internal, so nothing
    was recorded and `why` answered about an empty run. Silently: no error,
    no warning, just no lineage.
    """
    script = (
        "import blame, pandas as pd\n"
        "df = pd.DataFrame({'k': ['a', 'b', 'a'], 'v': [1, 2, 3]})\n"
        "with blame.trace() as h:\n"
        "    f = df[df.v > 1]\n"
        "    g = f.groupby('k', as_index=False)['v'].sum()\n"
        "print(len(h.run.steps))\n"
    )
    out = {}
    for name in ("plain", "my-pandas-work"):
        d = tmp_path / name
        d.mkdir()
        (d / "s.py").write_text(script)
        out[name] = subprocess.run(
            [sys.executable, "s.py"], cwd=d, capture_output=True, text=True, check=True
        ).stdout.strip()
    assert out["plain"] == "2", f"baseline broken: {out}"
    assert out["my-pandas-work"] == out["plain"], (
        f"a directory named {'my-pandas-work'!r} silently disabled tracing: {out}"
    )


def test_other_threads_do_not_leak_into_the_trace():
    """The patches are global; a trace describes one pipeline.

    Pandas work on an unrelated thread used to be recorded into whatever trace
    happened to be open, putting steps into the graph that the user never ran
    and adding source frames the pipeline never read.
    """
    import threading

    def other():
        time.sleep(0.05)
        d = pd.DataFrame({"zzz_unrelated": [9] * 5})
        for _ in range(3):
            d = d[d.zzz_unrelated > 0]

    t = threading.Thread(target=other)
    t.start()
    try:
        with blame.trace() as h:
            df = pd.DataFrame({"k": ["a", "b", "a"], "v": [1, 2, 3]})
            f = df[df.v > 1]
            time.sleep(0.15)  # the other thread runs inside the trace window
            f.groupby("k", as_index=False)["v"].sum()
    finally:
        t.join()

    ops = [s.op for s in h.run.steps]
    assert ops == ["filter", "groupby.sum"], f"other thread leaked in: {ops}"
    assert len(h.run.sources) == 1, f"phantom source frames: {h.run.sources}"


def test_two_threads_cannot_open_a_trace_at_once():
    """Opening a trace has to be one step, not a check followed by an act.

    start() tested _ACTIVE, then called install(), then assigned. install()
    touches 150-odd attributes, which is a wide enough window for a second
    thread to pass a check the first had not yet invalidated -- so two traces
    ran at once, each missing the other's steps, and whichever finished first
    unpatched pandas underneath the other. Both failures were silent: the user
    got a run back, it was just incomplete.

    The window is forced open here rather than raced for. Timing-dependent
    tests pass by luck; a correct implementation holds one lock across the
    whole of start(), so a slow install() cannot change the outcome.
    """
    import threading

    from blame import _tracer

    live = 0
    peak = 0
    counter_lock = threading.Lock()
    real_install = _tracer.install

    def slow_install():
        time.sleep(0.05)
        real_install()

    def worker():
        nonlocal live, peak
        try:
            with blame.trace():
                with counter_lock:
                    live += 1
                    peak = max(peak, live)
                time.sleep(0.02)
                with counter_lock:
                    live -= 1
        except RuntimeError:
            pass  # the guard doing its job is the expected outcome for one thread

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_tracer, "install", slow_install)
    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        monkeypatch.undo()

    assert peak == 1, f"{peak} traces were open at once; only one may be"
    assert _tracer._ACTIVE is None, "a tracer was left active"
    assert _tracer._PATCHES == [], "pandas was left patched"


def test_a_failed_restore_is_reported_not_swallowed():
    """uninstall() used to `except Exception: pass`.

    A method that cannot be put back stays wrapped for the rest of the
    process, affecting every later pandas call. Swallowing the error made that
    indistinguishable from a clean exit, so the remaining patches still have to
    be restored -- but not quietly.
    """
    from blame import _tracer

    class Unrestorable:
        __name__ = "Unrestorable"

        def __setattr__(self, name, value):
            raise AttributeError("read-only for the test")

    restored = []

    class Restorable:
        __name__ = "Restorable"

        def __setattr__(self, name, value):
            restored.append(name)

    _tracer._PATCHES.append((Restorable(), "ok_one", object()))
    _tracer._PATCHES.append((Unrestorable(), "bad_one", object()))
    _tracer._PATCHES.append((Restorable(), "ok_two", object()))
    try:
        with pytest.warns(UserWarning, match="could not restore"):
            _tracer.uninstall()
    finally:
        _tracer._PATCHES.clear()

    assert sorted(restored) == ["ok_one", "ok_two"], (
        f"a failed restore stopped the others: {restored}"
    )


def test_processes_sharing_a_store_do_not_corrupt_each_other(tmp_path):
    """The store is content-addressed, so two processes collide by design.

    A temp file named for the destination is the same path for every writer
    holding the same column. One renamed it away while another was still about
    to, and the loser raised FileNotFoundError -- which the tracer caught as
    "lineage capture failed", dropped the step, and handed back a run that was
    missing pieces. Two runs over the same inputs is the `blame diff` workflow,
    so identical columns are the common case, not the exotic one.
    """
    script = tmp_path / "w.py"
    script.write_text(
        "import sys, warnings\n"
        "import pandas as pd\n"
        "import blame\n"
        "warnings.simplefilter('error', UserWarning)\n"
        "root = sys.argv[1]\n"
        "df = pd.DataFrame({'a': list(range(200)), 'b': [float(i) for i in range(200)]})\n"
        "for i in range(12):\n"
        "    with blame.trace(label=f'{sys.argv[2]}-{i}', root=root) as h:\n"
        "        kept = df[df.a > 5]\n"
        "        out = kept.groupby(kept.a % 7, as_index=False)['b'].sum()\n"
        "    e = h.run.why(row=0, target=out)\n"
        "    assert e.sources, 'a step went missing from the run'\n",
        encoding="utf-8",
    )
    root = tmp_path / "shared"

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(root), tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for tag in ("A", "B", "C", "D")
    ]
    outputs = [(p.wait(timeout=120), p.communicate()[0]) for p in procs]

    for code, out in outputs:
        assert code == 0, f"a writer failed:\n{out}"
        assert "lineage capture failed" not in out, f"a step was dropped:\n{out}"


def test_a_stored_column_gets_a_temp_file_of_its_own(tmp_path):
    """The temp file has to be named for the writer, not the destination.

    Content addressing makes collision the normal case: two processes holding
    the same column compute the same digest, so a temp path derived from it is
    the same path for both. One renames it away while the other is still about
    to, and the loser gets FileNotFoundError -- which the tracer catches as
    "lineage capture failed" and turns into a run with steps missing.

    Asserted directly rather than raced for. The window is microseconds wide,
    so a test that raced for it would pass most runs even on broken code -- the
    multi-process test above reproduces the same defect only about twice in
    five attempts, which is why it is a scenario check and this is the guard.
    """
    import tempfile as tempfile_module

    from blame import _store

    target = tmp_path / "967ef979b46dab63839b.arrow"
    handed_out: list[str] = []
    real_mkstemp = tempfile_module.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        handed_out.append(name)
        return fd, name

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tempfile_module, "mkstemp", spy_mkstemp)
    try:
        _store._atomic_write(target, b"first writer")
        _store._atomic_write(target, b"second writer")
    finally:
        monkeypatch.undo()

    assert len(handed_out) == 2, "the write did not go through a temp file"
    assert handed_out[0] != handed_out[1], (
        f"both writers used {handed_out[0]}; whichever renamed first would "
        "delete the other's file out from under it"
    )
    assert target.read_bytes() == b"second writer"
    assert list(tmp_path.glob("*.tmp")) == [], "a temp file was left behind"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path):
    """A half-written temp file is litter the next reader may trip over."""
    import os as os_module

    from blame import _store

    target = tmp_path / "col.arrow"

    def failing_replace(src, dst):
        raise OSError("disk full, for the test")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(os_module, "replace", failing_replace)
    try:
        with pytest.raises(OSError, match="disk full"):
            _store._atomic_write(target, b"x" * 100)
    finally:
        monkeypatch.undo()

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == [], "a failed write left a temp file"


def test_a_refused_replace_accepts_the_file_that_is_already_there(tmp_path):
    """Windows refuses to replace a file another process has open.

    `os.replace` overwrites silently on POSIX, so the loser of a race wrote
    identical bytes over identical bytes and nothing noticed. On Windows it
    raises `PermissionError: [WinError 5]` while any process holds the
    destination open -- and a reader of this store holds it open for as long as
    it is loading that column. That turned a race two writers were expected to
    survive into "lineage capture failed" and a run with steps missing.

    Losing the race is not a failure: the filename is a content address, so a
    destination that already exists holds exactly these bytes.
    """
    import os as os_module

    from blame import _store

    target = tmp_path / "col.arrow"
    target.write_bytes(b"written by somebody else")

    def refusing_replace(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(os_module, "replace", refusing_replace)
    try:
        _store._atomic_write(target, b"written by somebody else")  # must not raise
    finally:
        monkeypatch.undo()

    assert target.read_bytes() == b"written by somebody else"
    assert list(tmp_path.glob("*.tmp")) == [], "the losing writer left its temp file"
