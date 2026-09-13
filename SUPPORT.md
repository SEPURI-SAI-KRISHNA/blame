# Getting help

## Something is wrong with the lineage

Open an [issue](https://github.com/SEPURI-SAI-KRISHNA/blame/issues/new/choose).
Wrong or missing lineage is the report that most improves this project, and a
small runnable pipeline is worth more than a long description.

## A question, or you are not sure it is a bug

[Discussions](https://github.com/SEPURI-SAI-KRISHNA/blame/discussions) — usage
questions are welcome there, and they are often the fastest route to finding out
that something *is* a bug.

## Before either

- [docs/accuracy.md](docs/accuracy.md) — which operations are exact, which are
  approximate, and why. Approximate answers are widened on purpose; that is not
  the same as being wrong.
- [docs/benchmarks.md](docs/benchmarks.md) — what capture is expected to cost.
- [docs/validation.md](docs/validation.md) — what has actually been tested.
- [ROADMAP.md](ROADMAP.md) — what is deliberately out of scope.

## A security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md) — `blame` reads your
data and writes it to `.blame/`, so anything with a disclosure risk should be
reported privately.

## Response times

This is a single-maintainer project, alpha, maintained alongside other work. A
reproducible bug report will get attention well before a feature request.
