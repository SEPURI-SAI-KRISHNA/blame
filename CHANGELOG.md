# Changelog

Notable changes to `pandas-blame`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Continuous integration: lint, a pandas 2.0 / 2.2 / 3.0 test matrix across
  Python 3.10–3.13, and a packaging job that installs the built wheel and
  checks it can answer a `why` on its own.
- A weekly scheduled CI run against unpinned pandas, so a release that breaks
  the tracer shows up in CI rather than in a bug report.
- Issue templates, contribution guide, code of conduct, security policy.

### Changed
- Codebase formatted with `ruff format`; `.git-blame-ignore-revs` keeps that
  commit out of `git blame`.

### Fixed
- Removed the `Typing :: Typed` classifier, which claimed typed support the
  package does not ship a `py.typed` marker for.

## [0.1.1] - 2026-09-13

### Fixed
- README image and links are absolute, so the demo renders on the PyPI project
  page instead of 404ing against `pypi.org`.
- `blame --help` lists `diff` and `ui`, which it had never mentioned.

## [0.1.0] - 2026-09-13

First public release.

### Added
- Row-level lineage capture for pandas by monkeypatching, with no code changes
  to the traced pipeline.
- `why` — which source rows produced a cell; `forward` — where a source row
  ended up; `table`, `at`, `highlight`.
- `blame ui` — a local page showing the pipeline as a graph, where clicking a
  cell highlights its source rows in every frame.
- `blame diff` — row-level differences between two runs, attributed to the
  input rows that changed, with `unexplained_rows()` for test suites.
- Exact lineage for filters, slices, sorts, joins, group-bys, `resample`,
  concats, `pivot`, `pivot_table`, `melt`, `unstack` and row-preserving
  transforms. Operations that cannot be traced exactly are marked approximate
  and widened to every candidate row, never silently narrowed.
- Content-addressed column store with structural sharing, keeping capture at
  about 2x wall time and 5 ms per step on a million rows.

[Unreleased]: https://github.com/SEPURI-SAI-KRISHNA/blame/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/SEPURI-SAI-KRISHNA/blame/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/SEPURI-SAI-KRISHNA/blame/releases/tag/v0.1.0
