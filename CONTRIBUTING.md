# Contributing

## Setup

```bash
git clone https://github.com/SEPURI-SAI-KRISHNA/blame
cd blame
uv venv && uv pip install -e ".[dev]"
uv run pytest
uvx pre-commit install
```

That last line is worth the ten seconds. The hooks are the same list CI runs --
formatting, a workflow audit, a few file checks -- so they catch at commit time
the things that otherwise cost you a round trip through a red build.

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
uvx pre-commit run --all-files
```

`pre-commit` runs ruff, a zizmor audit of the workflows, and the file checks.
It is exactly what the `lint` job runs, from the same pinned revisions, so a
pass here is a pass there.

Not covered by the hooks, and worth running if you touched the tracer or the
workflows:

```bash
uv run mypy
```

`actionlint` is not a hook because it runs shellcheck over every `run:` block
when it can find shellcheck and silently skips that half of its work when it
cannot -- on a machine without it you would be told a workflow is fine when it
is not. The `lint` job downloads a pinned, checksummed binary; the command is
in `.github/workflows/ci.yml` if you want to run it yourself.

If you changed what is exact and what is not, update `docs/accuracy.md`.

## Performance

`bench/overhead.py`, `bench/scaling.py` and `bench/diffing.py` produce the
numbers quoted in `docs/benchmarks.md`. If a change moves them, update the doc
in the same PR — stale benchmark claims are worse than none.
