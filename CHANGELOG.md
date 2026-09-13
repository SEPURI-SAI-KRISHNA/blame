# Changelog

Notable changes to `pandas-blame`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-09-13

### Added
- Continuous integration: lint, a pandas 2.0 / 2.2 / 3.0 test matrix across
  Python 3.10-3.13, and a packaging job that installs the built wheel and
  checks it can answer a `why` on its own.
- A weekly scheduled CI run against unpinned pandas, so a release that breaks
  the tracer shows up in CI rather than in a bug report.
- Issue templates, contribution guide, code of conduct, security policy.
- `py.typed` marker. The package was already annotated throughout, but without
  this file PEP 561 requires type checkers to ignore every one of those
  annotations in projects that depend on it.
- `blame --version`.
- Packaging tests pinning the invariants that drifted: classifiers must cover
  every Python the CI matrix tests, the `py.typed` marker must ship, and the
  PyPI publishing action must be pinned to an exact release tag.

### Changed
- Classifiers advertise Python 3.13, which CI had been testing all along, and
  declare `Typing :: Typed` and `Operating System :: OS Independent`.
- Workflows declare `permissions: contents: read` explicitly, and the PyPI
  publish step is pinned to the exact release tag `v1.14.2` rather than the
  moving `release/v1`. Not a commit digest: that action runs a container image
  tagged with the literal ref, and only release tags are published, so a SHA
  fails to pull.
- `CHANGELOG.md` ships in the sdist, and PyPI links to it from the sidebar.
- Codebase formatted with `ruff format`; `.git-blame-ignore-revs` keeps that
  commit out of `git blame`.

### Fixed
- The `blame diff` example in the README could not run as written: it named
  `before` and `after` without showing where they come from, and the obvious
  reading raised `AttributeError`, because `trace()` yields a handle whose
  `.run` is the run. Replaced with the runnable form.
- Tests no longer fail to collect on Python 3.10, which has no stdlib
  `tomllib`.
- Tests no longer construct `pandas.Timedelta`, which trips a numpy 2.5
  deprecation from inside pandas 2.2. Found by running the matrix combination
  (pandas 2.2 with numpy 2) that local testing had never covered.

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

[Unreleased]: https://github.com/SEPURI-SAI-KRISHNA/blame/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/SEPURI-SAI-KRISHNA/blame/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/SEPURI-SAI-KRISHNA/blame/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/SEPURI-SAI-KRISHNA/blame/releases/tag/v0.1.0
