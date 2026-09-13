## What this changes

<!-- One or two sentences. -->

## How it is verified

Lineage changes need a test that checks against **ground truth** rather than
against blame's own output — carry a source-row id through the pipeline in a
hidden column and assert that `why()` returns exactly that set. See
`test_filter_join_groupby_matches_ground_truth` for the pattern.

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] New lineage behaviour has a ground-truth test
- [ ] If accuracy changed, `docs/accuracy.md` says so
