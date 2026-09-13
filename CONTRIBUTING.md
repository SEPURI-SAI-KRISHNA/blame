# Contributing

## Setup

```bash
git clone https://github.com/SEPURI-SAI-KRISHNA/blame
cd blame
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## The one rule that matters

**Lineage tests check against ground truth, not against blame's own output.**

Asserting that `why()` returns what `why()` returned yesterday proves nothing.
Instead, carry a source-row id through the pipeline in a hidden column, let
pandas propagate it, and assert that `why()` returns exactly that set:

```python
truth = orders.assign(_src=np.arange(len(orders)))
# ... run the same pipeline over both ...
assert set(run.why(row=0).sources[src]) == set(result["_src"])
```

`test_filter_join_groupby_matches_ground_truth` is the canonical example. Every
bug found by running on third-party code became a test in this shape, and every
one of them was a case where blame's own output looked perfectly reasonable.

## The three rules the tracer follows

1. **Never change what the user's code returns.** We observe and hand back the
   original object untouched.
2. **Never raise.** If lineage cannot be derived, degrade to an approximate
   record and keep going. A debugging tool must not break the pipeline it is
   watching. This is why `_tracer.py` is full of broad `except` clauses, and
   why ruff's `BLE001` is disabled for it.
3. **Record only user-visible operations.** pandas implements its own methods
   with the ones we patch — `explode` goes through `reindex`, `stack` through
   `take`. Recording those invents a pipeline the user never wrote.

## Approximate is a feature, wrong is a bug

An answer that is approximate and says so is working correctly: it has been
*widened* to every candidate row. An answer that is wrong while claiming to be
exact is the worst thing this tool can do. If you cannot derive exact lineage
for an operation, mark it approximate rather than guessing.

## Before opening a PR

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
```

If you changed what is exact and what is not, update `docs/accuracy.md`.

## Performance

`bench/overhead.py`, `bench/scaling.py` and `bench/diffing.py` produce the
numbers quoted in `docs/benchmarks.md`. If a change moves them, update the doc
in the same PR — stale benchmark claims are worse than none.
