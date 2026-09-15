# Validation against third-party code

How `blame` behaves on code nobody involved in it wrote, and what that turned
up. Everything here comes from `validation/run.py`, which runs in CI on every
pull request — run it yourself with `python validation/run.py`.

## The corpus

The ten getting-started tutorials from the pandas documentation — real
idiomatic pandas written by the pandas maintainers, not by me — pinned to
pandas `v3.0.5`. Each runs twice, once plain and once inside `blame.trace()`,
and every DataFrame and Series the script leaves behind is compared between the
two with `pandas.testing.assert_frame_equal`.

| tutorial | frames compared | steps | approximate |
|---|---|---|---|
| 01_table_oriented | 2 | 2 | 0 |
| 02_read_write | 1 | 2 | 0 |
| 03_subset_data | 7 | 26 | 1 |
| 04_plotting | 1 | 2 | 0 |
| 05_add_columns | 2 | 10 | 0 |
| 06_calculate_statistics | 1 | 11 | 0 |
| 07_reshape_table_layout | 6 | 22 | 1 |
| 08_combine_dataframes | 6 | 17 | 0 |
| 09_timeseries | 3 | 19 | 0 |
| 10_text_data | 1 | 14 | 0 |
| **total** | **30** | **125** | **2** |

| | result |
|---|---|
| scripts that raised under tracing | **0 of 10** |
| scripts whose results changed | **0 of 10** |
| frames compared | 30 |
| traced steps | 125 |
| steps that were approximate | 2 |

Tracing changes nothing about what the tutorials compute. That is the claim
that matters most: a lineage tool that alters your results is worse than no
lineage tool.

### The two approximate steps

Both are `take`, which `blame` does not trace, reached from ordinary code:

- `03_subset_data`: `titanic.loc[titanic["Age"] > 35, "Name"]`
- `07_reshape_table_layout`: `no2.sort_index().groupby(["location"]).head(2)`

pandas implements `.loc` with a boolean mask and `groupby().head()` through
`take`, so lineage through them widens to every input row rather than being
exact. This is a **coverage gap, not a wrong answer** — asked directly, `blame`
says so rather than guessing:

```
KeyError: that frame is not in this run -- whatever produced it is not an
operation blame traces, so it has no recorded lineage.
```

It is not a regression either: the same two operations record nothing on pandas
2.0.3, 2.2.3 and 3.0.5 alike. Tracked separately.

An earlier version of this page reported eight tutorials, 83 steps and zero
approximate steps, and counted pandas calls made against calls handled. Those
figures came from a harness that was never committed and covered a smaller
selection with extra instrumentation. The numbers above are what the committed
harness measures, so they can be checked.

## What this approach found

Nothing else found these:

- `resample` was untraced and its result never entered the graph, so `why()`
  answered confidently about an unrelated frame.
- A duplicated index silently degraded `head`, `tail` and `sort_values` — and a
  duplicated index is what you get from `concat`, from `melt`, or from reading
  any file keyed on a non-unique column.
- Reshape keys were read as a `Series`, which pandas aligns on *its* index, so
  every `pivot` on a frame that was not `RangeIndex`ed was quietly wrong. The
  tests missed it because they all used default indexes.
- pandas implements `explode` with `reindex`, `stack` with `take`, and plotting
  with half a dozen more. Those internal calls were recorded as user steps: one
  `explode` produced seven, two of them carrying approximate warnings about
  operations that appear nowhere in the user's code.

Our own tests encode the assumptions we already had about what a pipeline looks
like. The tutorials do not, and that difference is the whole point.

## How to reproduce

```bash
python validation/run.py           # the table above
python validation/run.py --json    # machine-readable
python validation/run.py --only 03_subset_data
```

The harness downloads the tutorials and their data from the pinned pandas tag
on first run and caches them, so later runs need no network. It exits non-zero
if any script raises under tracing or if any frame differs between the traced
and untraced runs.

The pin matters: without it, a change in the result means "pandas edited a
tutorial" as often as it means "we broke something".
