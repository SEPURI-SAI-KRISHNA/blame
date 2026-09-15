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
| 03_subset_data | 7 | 28 | 0 |
| 04_plotting | 1 | 2 | 0 |
| 05_add_columns | 2 | 10 | 0 |
| 06_calculate_statistics | 1 | 11 | 0 |
| 07_reshape_table_layout | 6 | 22 | 0 |
| 08_combine_dataframes | 6 | 17 | 0 |
| 09_timeseries | 3 | 19 | 0 |
| 10_text_data | 1 | 14 | 0 |
| **total** | **30** | **127** | **0** |

| | result |
|---|---|
| scripts that raised under tracing | **0 of 10** |
| scripts whose results changed | **0 of 10** |
| frames compared | 30 |
| traced steps | 127 |
| steps that were approximate | **0** |

Tracing changes nothing about what the tutorials compute. That is the claim
that matters most: a lineage tool that alters your results is worse than no
lineage tool.

### No approximate steps

There were two when this harness first ran, both pandas' internal `take`
showing through from `df.loc[mask, cols]` and `groupby().head(n)` -- neither of
which recorded a step of its own. `.loc` and `.iloc` are now traced directly
and `groupby().head`/`tail` are answered from the group's positional indices,
so both are exact. The step count rose from 125 to 127 because two `.iloc`
calls in `03_subset_data` that previously recorded nothing now appear as the
operations they are.

That is the useful shape of a step-count change: it should move only when a
line of *your* code starts or stops being recorded. A jump without one means
pandas' internal calls have started masquerading as user steps, which is what
`explode` and `stack` once did.

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

It needs `matplotlib` and `openpyxl` installed -- dependencies of the corpus
rather than of `blame`: 04_plotting draws, and 02_read_write round-trips an
`.xlsx`. A missing one is reported as `untraced: ModuleNotFoundError` against
that tutorial, which distinguishes it from anything `blame` did.

The harness downloads the tutorials and their data from the pinned pandas tag
on first run and caches them, so later runs need no network. It exits non-zero
if any script raises under tracing or if any frame differs between the traced
and untraced runs.

The pin matters: without it, a change in the result means "pandas edited a
tutorial" as often as it means "we broke something".
