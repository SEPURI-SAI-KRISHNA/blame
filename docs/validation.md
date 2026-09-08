# Validation against third-party code

How `blame` behaves on code nobody involved in it wrote, and what that
turned up. Reproduce with the harness described at the bottom.

## The corpus

The eight data-handling tutorials from the pandas documentation — real
idiomatic pandas written by the pandas maintainers, not by me — run under
`blame` on pandas 2.0.3, 2.2.3 and 3.0.5:

| | result |
|---|---|
| scripts that raised under tracing | **0 of 8** |
| scripts whose results changed | **0 of 8** |
| pandas calls made | 110 |
| calls `blame` has a handler for | 104 (95%) |
| traced steps | 83 |
| steps that were approximate | **0** |

The six unhandled calls are `idxmax`, `isin`, `notna`, `unique` and `info` —
none of which return a frame, so none of them have row lineage to record. All
three pandas versions produce an identical 83-step graph.

This is what running it on code I hadn't written was for. Nothing else found
these:

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


## How to reproduce

The corpus is the eight data-handling tutorials from the pandas
documentation (`doc/source/getting_started/intro_tutorials/*.rst`), with the
`.. ipython:: python` blocks extracted into scripts and run against the data
files in `doc/data/`. Each script runs twice, traced and untraced, and every
DataFrame it leaves behind is compared with `pandas.testing.assert_frame_equal`.
