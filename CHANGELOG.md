# Changelog

Notable changes to `pandas-blame`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- CodeQL runs on every push, pull request and weekly, with the
  `security-and-quality` query suite. Nothing previously looked for the classes
  of defect it finds -- `ruff` checks style and `mypy` checks annotations, and
  neither reads `ui.py`'s HTTP handling, `_store.py`'s path construction from a
  user-supplied root, or `cli.py`'s argument handling with that question in
  mind. ([#16](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/16))
- Dependabot watches the pinned action versions weekly. Every workflow pins to
  an exact release tag, which is what makes the supply chain reviewable; without
  something watching them, "pinned" quietly becomes "stuck on a version with a
  known problem".

### Changed
- Run manifests record the store format version. Without it, the first
  incompatible change to the on-disk layout would have reached a user as a
  `KeyError` from inside a query, on a trace recorded weeks earlier; a reader
  that cannot understand a trace now says so, naming both versions. A manifest
  with no `format` key is read as version 1, so every trace written by 0.1.0
  through 0.1.2 keeps working.
  ([#21](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/21))
- CI runs the suite on Windows and macOS. Every job ran on Linux, while the
  package declared no platform restriction and PyPI served it to everyone. The
  tracer decides whose code a stack frame belongs to by comparing filesystem
  paths as strings, which is the most platform-sensitive thing in it -- and the
  fix for the path-attribution bug in 0.1.2 was written and checked on Linux
  only. ([#15](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/15))
- Python 3.14 is tested in CI and declared in the trove classifiers. It has
  worked since well before this release; nothing said so, so PyPI's metadata
  suggested the opposite to anyone checking before installing.
  ([#20](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/20))
- CI no longer runs the weekly schedule on forks. A cron is inherited by every
  fork, so each one was spending its owner's Actions minutes on a schedule they
  never set up; a fork's own pushes and pull requests still build.
  ([#19](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/19))
- Every CI and release job declares `timeout-minutes`. GitHub's default is six
  hours, so one hung job could burn the whole budget and block everything
  queued behind it.
- Superseded builds are cancelled only for pull requests. Cancelling a push to
  `main` threw away the build record of the commit that had just become `main`.

### Fixed
- Storing a column could fail on Windows when another process was reading the
  same column. `os.replace` overwrites silently on POSIX, so the loser of a
  race between two writers wrote identical bytes over identical bytes; on
  Windows it raises `PermissionError: [WinError 5]` while any process holds the
  destination open, and a reader holds it open for as long as it is loading
  that column. That turned a race writers were meant to survive back into
  "lineage capture failed" and a run with steps missing. The filename is a
  content address, so a destination that already exists holds exactly those
  bytes: losing the race is now treated as the write having succeeded.
  Introduced by the fix for #33 and found by the Windows job added in #15.
- Two processes tracing into the same `.blame` directory corrupted each
  other's runs. The store is content-addressed, so two writers holding the same
  column compute the same digest -- and the temporary file was named for that
  digest, so it was the same path for both. One renamed it into place while the
  other was still about to, and the loser raised `FileNotFoundError`, which
  surfaced as "lineage capture failed" and a run with steps missing from it.
  Temporary files are now unique to the writer and moved with `os.replace`, so
  the loser of a race overwrites identical bytes instead of failing. The default
  root is `.blame` in the working directory, so this affected anything running
  more than one process in a project -- `pytest -n auto`, two notebooks, a
  parallel batch job. The run manifest is written the same way now, so a reader
  listing runs cannot catch it half-formed.
  ([#33](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/33))
- Two threads could open a trace at the same time. `start()` checked whether a
  trace was running, then called `install()` -- 158 attribute assignments --
  and only then claimed the slot, so a second thread could pass a check the
  first had not yet invalidated. Both traces then ran at once, each missing the
  other's steps, and whichever finished first restored pandas underneath the
  one still running. Both failures were silent: a run came back, it was just
  incomplete. `start()`, `stop()`, `install()` and `uninstall()` now hold one
  reentrant lock across the whole transition. Reading the active tracer stays
  lock-free, because that happens on every patched pandas call.
  ([#18](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/18))
- `uninstall()` could raise `IndexError: pop from empty list` when two threads
  ended a trace at once, and silently swallowed any attribute it failed to
  restore -- leaving pandas patched for the rest of the process with no
  indication. Failed restores are now reported.

### Fixed
- The `py.typed` marker shipped in 0.1.2 promised working type hints and
  delivered none. `trace()` had no return annotation, so `blame.trace()`
  yielded an untyped value and every type check a caller wrote against it
  passed vacuously -- `h.run.nosuchmethod()` and `why(row="not-an-int")` were
  both accepted. `Handle` is now a module-level class, `trace()` is annotated,
  and `Explanation.frames()` returns `dict[str, pd.DataFrame]` rather than
  `dict[str, object]`, so its result can be used without a cast.

### Added
- CI builds the sdist, unpacks it and runs its own test suite. Downstream
  packagers build from the sdist and run the suite during the build, and every
  other job runs from a full checkout, so that failure could not surface
  anywhere else. The unpacked directory is named `pandas_blame-<version>`,
  which also makes this a standing guard against the "path contains pandas"
  regression.
- A CI job that installs the declared dependency minimums -- pandas 2.0.0,
  numpy 1.24.0, pyarrow 14.0.0 on Python 3.10 -- and runs the suite against
  them, plus a test asserting those pins match the bounds in `pyproject.toml`
  so the two cannot drift.
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
