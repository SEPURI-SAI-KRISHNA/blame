# blame

**Cell-level "why" for pandas pipelines. No code changes.**

Your report says `41,900` and it should say `38,400`. Today you find that out by
adding print statements down a thirty-step pipeline until something looks wrong.
`blame` records the pipeline as it runs, so you can point at the number instead.

![clicking the wrong number lights up the rows that made it](https://raw.githubusercontent.com/SEPURI-SAI-KRISHNA/blame/main/docs/demo.gif)

## Install

```bash
pip install pandas-blame
```

## Use

```python
import blame, pandas as pd

with blame.trace():
    orders    = pd.read_csv("orders.csv")
    customers = pd.read_csv("customers.csv")
    clean     = orders[orders.qty > 0]
    joined    = clean.merge(customers, on="customer_id")
    report    = joined.groupby("region", as_index=False)["total"].sum()

print(blame.last_run().why(row=1, col="total"))
```

```
why(groupby.sum [f5] 3x2, row [1], column 'total')

  derivation:
    step   3 groupby.sum('region') [demo.py:9] <- f4:2 rows
    step   2 merge(on='customer_id') [demo.py:8] <- f3:1 row, f1:2 rows
    step   0 filter(<frame>) [demo.py:6] <- f0:1 row

  source rows:
    read_csv('customers.csv') [f1]: 2 row(s) -> [1, 4]
    read_csv('orders.csv') [f0]: 1 row(s) -> [1]
```

One order row, but **two** customer rows: `customers.csv` has `customer_id 11`
twice, so the merge duplicated that order and the group-by counted it twice.
Two row numbers in two files, not "somewhere in these steps".

```bash
blame steps    # every operation the pipeline ran, with line numbers
blame ui       # the picture above: click a cell, watch its sources light up
blame diff     # what changed since the last run, and which input row did it
```

`blame diff` is the one that earns its keep in a test suite:

```python
assert not before.diff(after, on="order_id").unexplained_rows()
```

Inputs that did not move should not produce outputs that did. When they do,
`blame` names the input row responsible — or says plainly that none explains it,
which means the pipeline changed rather than the data.

## Honest limits

Lineage is **exact** for filters, slices, sorts, joins, group-bys, `resample`,
concats, `pivot`, `pivot_table`, `melt`, `unstack` and the row-preserving
transforms. It is **approximate**, and says so with a `~`, for `apply`/`map` with
a user function, and for operations it does not cover (`transpose`, `stack`,
`explode`). Approximate answers are widened to every candidate row, never
narrowed and never presented as exact — see [docs/accuracy.md](https://github.com/SEPURI-SAI-KRISHNA/blame/blob/main/docs/accuracy.md).

Capture costs about **2x** wall time and **5 ms per step** on a million rows
([benchmarks](https://github.com/SEPURI-SAI-KRISHNA/blame/blob/main/docs/benchmarks.md)). Tested on pandas 2.0.3, 2.2.3 and 3.0.5;
the eight pandas documentation tutorials run under it with no exceptions, no
changed results and no approximate steps ([validation](https://github.com/SEPURI-SAI-KRISHNA/blame/blob/main/docs/validation.md)).

## Status

Alpha, and honest about it. It works, it is tested against ground truth, and it
has never been run on your pipeline. If it gets something wrong about a real one,
that is the bug report worth filing. [Roadmap](https://github.com/SEPURI-SAI-KRISHNA/blame/blob/main/ROADMAP.md).

Apache-2.0.
