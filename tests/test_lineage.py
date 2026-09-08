"""Correctness tests.

The important ones use ground truth rather than assertions about our own
output: a hidden column carries each source row's id through the pipeline,
pandas propagates it, and we check that why() returns exactly that set.
"""

import numpy as np
import pandas as pd
import pytest

import blame


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _orders(n=40, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "id": np.arange(n),
        "cust": rng.integers(0, 7, n),
        "qty": rng.integers(-2, 6, n),
        "price": rng.integers(1, 50, n).astype(float),
    })


# -- ground truth ---------------------------------------------------------

def test_filter_join_groupby_matches_ground_truth():
    orders = _orders()
    customers = pd.DataFrame({"cust": [0, 1, 2, 3, 4, 5, 6, 1],
                              "region": list("nsewnsen")})
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
    assert run.why(row=1).sources == {afid: np.array([1])} or \
        run.why(row=1).sources[afid].tolist() == [1]
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
        pd.testing.assert_frame_equal(got.reset_index(drop=True), want[got.columns],
                                      check_dtype=False)


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
        wide = kept.pivot_table(index="a", columns="b", values="v", aggfunc="sum")
        out = wide.reset_index()
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
    assert page["rows"][3][page["columns"].index("price")] is None   # NaN -> null

    answer = json.loads(json.dumps(ui.explain_payload(run, run.result_fid, [0]), allow_nan=False))
    truth = run.highlight(row=0, target=run.result_fid)
    assert {f: m["rows"] for f, m in answer["marks"].items()} == \
           {f: r.tolist() for f, r in truth.items()}


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
        truth = np.flatnonzero((df.index >= day) & (df.index < day + pd.Timedelta("1D")))
        got = run.why(row=out_row).sources[run.sources[0]]
        assert np.array_equal(got, truth), f"row {out_row}"


def test_resample_without_column_selection_is_traced():
    idx = pd.date_range("2024-03-01", periods=12, freq="8h")
    df = pd.DataFrame({"v": np.arange(12.0)}, index=idx)
    with blame.trace() as h:
        daily = df.resample("D").sum()
    truth = np.flatnonzero(df.index < df.index[0].normalize() + pd.Timedelta("1D"))
    assert np.array_equal(h.run.why(row=0).sources[h.run.sources[0]], truth)
    assert len(daily) == 4


def test_duplicate_index_does_not_force_approximate_lineage():
    """A duplicated index is everywhere in real data -- after concat, melt, or
    just reading a file keyed on a non-unique column."""
    df = pd.DataFrame({"city": list("aabbcc"), "v": [5, 3, 9, 1, 7, 2]},
                      index=[0, 0, 1, 1, 2, 2])
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
    assert f"{len(report)}x" in repr(by_object)      # the shape is stated


def test_asking_about_an_untraced_frame_says_so_instead_of_guessing():
    orders = _orders()
    with blame.trace() as h:
        orders[orders.qty > 0]
    outsider = pd.DataFrame({"a": [1, 2, 3]})
    with pytest.raises(KeyError, match="not in this run"):
        h.run.why(row=0, target=outsider)
