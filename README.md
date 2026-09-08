# blame

**Cell-level "why" for pandas pipelines. No code changes.**

Your report says `revenue = 41,900` and it should say `38,400`. Today you find out
by inserting print statements down a thirty-step pipeline until something looks
wrong. `blame` records the pipeline as it runs and lets you ask the question
directly.

```python
import blame, pandas as pd

with blame.trace():
    orders    = pd.read_csv("orders.csv")
    customers = pd.read_csv("customers.csv")
    clean     = orders[orders.qty > 0]
    clean     = clean.assign(total=clean.qty * clean.price)
    joined    = clean.merge(customers, on="customer_id")
    report    = joined.groupby("region", as_index=False)["total"].sum()

run = blame.last_run()
print(run.why(row=1, col="total"))
```

The `south` row of the report says `50.0` and should say `25.0`:

```
why(groupby.sum [f5] 3x2, row [1], column 'total')

  derivation:
    step   3 groupby.sum('region') [demo.py:9] <- f4:2 rows
    step   2 merge(on='customer_id') [demo.py:8] <- f3:1 row, f1:2 rows
    step   1 assign(total=<frame>) [demo.py:7] <- f2:1 row
    step   0 filter(<frame>) [demo.py:6] <- f0:1 row

  source rows:
    read_csv('customers.csv') [f1]: 2 row(s) -> [1, 4]
    read_csv('orders.csv') [f0]: 1 row(s) -> [1]
```

One order row, but **two** customer rows — `customers.csv` has `customer_id 11`
twice, so the merge duplicated that order and the group-by added it up twice.
The answer is two row numbers in two files, not "somewhere in these steps".

## Or point at it

```bash
blame ui
```

![the blame UI](docs/ui.png)

The pipeline on the left, the data on the right. Click the cell that is wrong
and every frame that touched it lights up with the number of rows it
contributed; frames that had nothing to do with it grey out. Click any frame in
the graph to see those rows in it — here, `customers` rows 1 and 4, the same
`customer_id` twice. The address bar carries the selection, so a link to one
cell's explanation can go straight into a code review.

## Install

Not on PyPI yet — the `blame` name needs to be checked and claimed before v0.1
ships.

```bash
git clone <this repo> && cd blame
uv venv && uv pip install -e ".[dev]"
```

## What you get

| Question | Call |
|---|---|
| Which source rows produced this cell? | `run.why(row=2, col="total")` |
| Where did this input row end up? | `run.forward(row=17)` |
| What did the pipeline actually do? | `run.table()` |
| What did the data look like at step 12? | `run.at(12)` |
| ...about this exact frame, not one I named | `run.why(row=2, target=report)` |
| Why did yesterday's number change? | `before.diff(after, on="order_id")` |
| All of the above, by clicking | `run.ui()` or `blame ui` |

Nothing in your pipeline changes. `blame` wraps pandas while the trace is
active and restores it afterwards; your code returns exactly the objects it
returned before.

## When yesterday's numbers don't match today's

```bash
blame diff --on region --on order_id
```

```
diff 42552a4ab2e2 -> 1f92a9b30fc6
  groupby.sum: 3x2 -> 3x2   (rows paired by column 'region')
  2 changed

  ~ row 'north'  total: 72.0 -> 40.0
      caused by orders row 3: qty: 4.0 -> 0.0
  ~ row 'south'  total: 25.0 -> 70.0
      caused by orders row 2: price: 25.0 -> 40.0
      caused by orders row 6: added

  2 of 2 differing rows traced to a changed input row
    orders: 5 -> 6 rows, 1 added, 2 changed  (by column 'order_id')
    customers: 4 rows, unchanged
```

A plain table compare tells you `north` went from 72 to 40. `blame` tells you
order 3 was cancelled. The difference is the lineage: for every output row that
moved, it walks back to the input rows that produced it and keeps the ones that
moved too.

Rows are paired by **value, not position** — insert one row at the top of a
file and a positional diff calls every row below it changed. Pass `--on` with
the columns that identify a row; it is applied to each frame that has them, and
each frame reports which identity it actually used.

When nothing in the inputs accounts for a difference, it says so rather than
inventing a cause — that is the signature of the pipeline having changed, not
the data:

```python
assert not before.diff(after, on="order_id").unexplained_rows()
```

That line in a test suite is the point of the feature: inputs that did not move
should not produce outputs that did.

## Does it work on someone else's code?

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

## What it costs

Measured on this machine (`python bench/overhead.py`), an 11-step pipeline —
filter, assign, dropna, merge, sort, drop_duplicates, two-key group-by:

| input rows | untraced | traced | overhead | trace on disk | `why()` |
|---|---|---|---|---|---|
| 10,000 | 0.006s | 0.019s | 3.4x | 0.8 MB | 2 ms |
| 100,000 | 0.024s | 0.052s | 2.2x | 7.7 MB | 3 ms |
| 1,000,000 | 0.206s | 0.411s | **2.0x** | 77 MB | 11 ms |

Most of that is a fixed price paid once for writing your input frames, not a
per-step tax. Chaining more operations over the same million rows costs about
**5 ms per step** and the ratio stays flat (`python bench/scaling.py`):

| steps | untraced | traced | overhead |
|---|---|---|---|
| 4 | 0.101s | 0.237s | 2.3x |
| 8 | 0.137s | 0.325s | 2.4x |
| 16 | 0.211s | 0.488s | 2.3x |
| 32 | 0.413s | 0.861s | 2.1x |

`blame` records only what your code invoked. pandas implements many of its own
methods with the ones we patch, and those internal calls used to be recorded
too — the seven-operation pipeline above was showing ten steps, and paying for
all ten.

A frame whose index has duplicates cannot identify its own rows, so `blame`
replays that one step with hidden position columns to recover them exactly
rather than reporting every input row as a candidate. That step costs about 2x
instead of 1.7x (at 1M rows: 225 ms against 115 ms). It applies per step, only
on duplicate-index frames, and only for operations where an extra column cannot
change what gets selected.

`blame diff` scales with the size of the difference, not the size of the data
(`python bench/diffing.py`): 42 ms over 10,000 input rows, 316 ms over 100,000,
3.5 s over 1,000,000 — with 1% of the input rows changed in each case.

Traces are written uncompressed because a local debugging cache is bound by
write time, not disk: zstd shrinks an int64 column 2.3x but takes 14x longer to
write it. Pass `compression="zstd"` or `"lz4"` if you would rather have the
space, and `sample_rows=N` to cap how much of very large frames gets stored
(lineage stays exact either way).

## How it works

Every traced operation stores a **lineage vector** — a compact array mapping
output rows back to input rows. Filters store the selection; joins store the
matched row pairs on both sides; group-bys store the group membership in CSR
layout. Answering `why` is then a walk backwards through those arrays, which is
array indexing rather than recomputation.

Intermediate frames are written to `.blame/` one column at a time, each
addressed by the hash of its contents. Most columns are never written at all: a
filtered, sorted or joined column is its parent's column seen through a row
mapping, so `blame` stores a *reference* to the parent plus the mapping it
already recorded, and rebuilds the column by indexing when you ask to see it.
Only two kinds of bytes are ever stored — your input frames, and columns whose
values are genuinely new (`assign`, aggregations).

## Honesty about accuracy

Lineage is **exact** for filters, slices (positional and label), sorts, joins,
group-bys, `resample`, concats, the reshapes (`pivot`, `pivot_table`, `melt`,
`unstack`), column projections and the row-preserving transforms (`assign`,
`rename`, `astype`, `fillna`, ...) — including on frames whose index has
duplicates.

It is **approximate** in two situations, and says so in both:

*An opaque user function* — `apply`, `map`, `transform` with a lambda. `blame`
assumes row identity and marks the step `~`.

*An operation `blame` does not cover* — `transpose`, `stack`, `explode`,
`wide_to_long`. pandas tells us it derived one frame from another, so rather than
letting the intermediate pose as a pipeline input, `blame` inserts an explicit
step and widens the answer to every candidate row:

```
    step   2 <untraced explode>() [pipeline.py:8] <- f1:4 rows  ~approximate
```

Either way `run.table()` shows a `~`, `why()` prints a warning banner, and
`Explanation.approximate` is `True`. The chain still runs all the way back to
your real source files — it is widened, never truncated, and never presented as
exact.

## Try it

```bash
git clone <this repo> && cd blame
uv venv && uv pip install -e ".[dev]"
python examples/pipeline.py      # a pipeline with a planted double-counting bug
blame steps                      # what it did
blame why 1 --col total --show 5 # why the "south" total is wrong
blame ui                         # the same answer, by clicking
blame diff                       # what changed between the last two runs
pytest -q                        # 56 tests, ground-truth checked
```

Tested against pandas 2.0.3, 2.2.3 and 3.0.5, on Python 3.11 and 3.12.

## Status

v0.1 is the tracer, the store and the query API for pandas, with a CLI; v0.3
is the page above; v0.6 is `blame diff`. All are validated against third-party
code as shown. `blame diff run1 run2`, column-level `why`, and polars are
next — see `ROADMAP.md`, which also lists what is known not to work.
