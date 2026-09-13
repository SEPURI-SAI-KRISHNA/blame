## What this changes

<!-- One or two sentences. What is different afterwards, and why. -->

## Related issue

<!-- "Closes #123" if this fixes an issue, so it closes on merge. Delete if not applicable. -->

Closes #

## How it is verified

<!--
Say what you actually ran, not what should pass. If you found a bug, the most
convincing thing you can show is that the new test FAILS on the old code:
paste that failure.
-->

Lineage changes need a test that checks against **ground truth** rather than
against blame's own output — carry a source-row id through the pipeline in a
hidden column and assert that `why()` returns exactly that set. See
`test_filter_join_groupby_matches_ground_truth` for the pattern.

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] New lineage behaviour has a ground-truth test
- [ ] A bug fix has a test that fails without the fix
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`
- [ ] If accuracy changed, `docs/accuracy.md` says so
- [ ] If the cost model changed, the figures in `README.md` still hold (`bench/overhead.py`)

## Anything a reviewer should know

<!--
Deliberate omissions, limitations you chose to leave in place, alternatives you
rejected. A known limitation stated plainly is better than one discovered later.
-->
