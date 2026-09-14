# Changelog

Notable changes to `pandas-blame`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- The `py.typed` marker shipped in 0.1.2 promised working type hints and
  delivered none. `trace()` had no return annotation, so `blame.trace()`
  yielded an untyped value and every type check a caller wrote against it
  passed vacuously -- `h.run.nosuchmethod()` and `why(row="not-an-int")` were
  both accepted. `Handle` is now a module-level class, `trace()` is annotated,
  and `Explanation.frames()` returns `dict[str, pd.DataFrame]` rather than
  `dict[str, object]`, so its result can be used without a cast.

### Added
- `mypy` runs in CI over `src/blame`, with a step that checks a *downstream*
  project actually gets type errors -- the failure above passed `mypy` on the
  package itself, so checking the package alone would not have caught it.
- `Handle` is exported, so the value `trace()` yields can be named in a
  signature.

### Changed
- `Handle.run` is typed `Run` rather than `Run | None` and raises a
  `RuntimeError` when read before the `with` block exits, instead of returning
  `None`. Nothing is recorded until the block closes, so the old return value
  was never useful; the annotation now matches the contract and callers do not
  have to narrow away a `None` that cannot occur in practice.

### Fixed
- Pandas operations running on *other* threads were recorded into whatever
  trace happened to be open, putting steps into the graph that the pipeline
  never ran and adding source frames it never read. The patches are global, so
  every thread reached them; the tracer now ignores work arriving from any
  thread other than the one that opened the trace. Affects any program using
  pandas on more than one thread -- a request handler, a `ThreadPoolExecutor`,
  joblib or Dask with a threading backend.
  ([#5](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/5))

### Fixed
- Tracing silently recorded nothing when the traced code lived in a path
  containing the string `pandas` -- `~/pandas-tutorial/`, `~/pandas_work/`, an
  unpacked `pandas_blame` sdist. Stack frames were attributed to pandas by
  searching the file path for "pandas" rather than by comparing against the
  directory pandas is installed in, so every user frame looked internal, no
  steps were captured, and `why` answered about an empty run. No exception and
  no warning: the pipeline ran normally and the lineage was simply absent.
  Affects 0.1.0 through 0.1.2. ([#1](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/1))

### Changed
- The sdist ships `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `SECURITY.md`,
  which the README links to, and the two tests that read `.github/workflows/`
  now skip outside a source checkout instead of failing. The suite could not
  pass when run from an unpacked sdist, which is how downstream packagers
  (Debian, conda-forge, Homebrew, Nix) run it during a build.
  ([#2](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/2))

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
