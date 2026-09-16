# Benchmarks

Every number here comes from a script in `bench/`, run on one machine. Run
them yourself before believing them.

## Capture overhead

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
