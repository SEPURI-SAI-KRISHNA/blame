# What is exact, and what is not

The whole value of a provenance tool is that you can trust the answer. Where
`blame` cannot be exact it says so, in the step table, in the explanation and
on the `Explanation` object.

## The two approximate cases

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

