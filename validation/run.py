"""Run the pandas documentation tutorials under blame and check nothing changed.

The corpus is code nobody involved in this project wrote: the getting-started
tutorials from the pandas documentation, pinned to a pandas release. Our own
tests share our own assumptions about what a pipeline looks like; these do not.
That difference is the point -- four real defects were found this way, and they
are listed in docs/validation.md.

Each tutorial runs twice, once plain and once inside `blame.trace()`. Every
DataFrame and Series the script leaves behind is compared between the two runs
with pandas' own `assert_frame_equal`. A tracer that changes what the user's
code returns is broken no matter how good its lineage is.

    python validation/run.py            # human-readable table
    python validation/run.py --json     # machine-readable, for the docs table
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import traceback
import urllib.request
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Pinned: the corpus has to be the same code every time, or a change in the
# result means "pandas edited a tutorial" as often as it means "we broke
# something".
PANDAS_REF = "v3.0.5"
RAW = f"https://raw.githubusercontent.com/pandas-dev/pandas/{PANDAS_REF}"

TUTORIALS = [
    "01_table_oriented",
    "02_read_write",
    "03_subset_data",
    "04_plotting",
    "05_add_columns",
    "06_calculate_statistics",
    "07_reshape_table_layout",
    "08_combine_dataframes",
    "09_timeseries",
    "10_text_data",
]
DATA_FILES = [
    "air_quality_long.csv",
    "air_quality_no2.csv",
    "air_quality_no2_long.csv",
    "air_quality_parameters.csv",
    "air_quality_pm25_long.csv",
    "air_quality_stations.csv",
    "titanic.csv",
]

_BLOCK = re.compile(r"^\.\. ipython:: python\s*$")
_OPTION = re.compile(r"^\s+:[a-z]+:\s*$")


def extract(rst: str) -> str:
    """The `.. ipython:: python` blocks of a tutorial, as one script.

    Sphinx renders these blocks by executing them in order in a shared
    namespace, so concatenating them reproduces exactly what the published page
    shows a reader.
    """
    lines = rst.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _BLOCK.match(lines[i]):
            i += 1
            continue
        i += 1
        while i < len(lines) and (_OPTION.match(lines[i]) or not lines[i].strip()):
            i += 1
        body: list[str] = []
        while i < len(lines):
            line = lines[i]
            if line.strip() and not line.startswith("    "):
                break
            body.append(line[4:] if line.startswith("    ") else "")
            i += 1
        while body and not body[-1].strip():
            body.pop()
        # @savefig is a sphinx directive rather than python; the plotting call
        # it decorates is on the following line and is kept.
        out.extend(line for line in body if not line.lstrip().startswith("@"))
        out.append("")
    return "\n".join(out)


def fetch(cache: Path) -> Path:
    """Download the corpus once and keep it, so reruns need no network."""
    work = cache / PANDAS_REF
    (work / "data").mkdir(parents=True, exist_ok=True)
    for name in TUTORIALS:
        script = work / f"{name}.py"
        if script.exists():
            continue
        url = f"{RAW}/doc/source/getting_started/intro_tutorials/{name}.rst"
        with urllib.request.urlopen(url) as response:
            script.write_text(extract(response.read().decode()), encoding="utf-8")
    for name in DATA_FILES:
        target = work / "data" / name
        if target.exists():
            continue
        with urllib.request.urlopen(f"{RAW}/doc/data/{name}") as response:
            target.write_bytes(response.read())
    return work


@dataclass
class Result:
    name: str
    raised: str = ""
    changed: list[str] = field(default_factory=list)
    compared: int = 0
    steps: int = 0
    approximate: int = 0
    approximate_ops: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.raised and not self.changed


def _frames(namespace: dict) -> dict:
    import pandas as pd

    return {
        k: v
        for k, v in namespace.items()
        if isinstance(v, (pd.DataFrame, pd.Series)) and not k.startswith("_")
    }


def check(script: Path, work: Path) -> Result:
    import pandas as pd

    import blame

    result = Result(name=script.stem)
    code = compile(script.read_text(encoding="utf-8"), str(script), "exec")

    plain: dict = {"__name__": "__main__", "__file__": str(script)}
    traced: dict = {"__name__": "__main__", "__file__": str(script)}

    root = Path(tempfile.mkdtemp(prefix="blame-validation-"))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                exec(code, plain)  # running the corpus is the job
            except Exception:  # noqa: BLE001 - whatever the tutorial raises is the result
                result.raised = "untraced: " + traceback.format_exc().strip().splitlines()[-1]
                return result
            try:
                with blame.trace(root=root) as handle:
                    exec(code, traced)
            except Exception:  # noqa: BLE001 - whatever the tutorial raises is the result
                result.raised = "traced: " + traceback.format_exc().strip().splitlines()[-1]
                return result

        run = handle.run
        result.steps = len(run.steps)
        approximate = [s for s in run.steps if s.approximate]
        result.approximate = len(approximate)
        result.approximate_ops = sorted({s.op for s in approximate})

        before, after = _frames(plain), _frames(traced)
        for name in sorted(before.keys() & after.keys()):
            result.compared += 1
            try:
                if isinstance(before[name], pd.DataFrame):
                    pd.testing.assert_frame_equal(before[name], after[name])
                else:
                    pd.testing.assert_series_equal(before[name], after[name])
            except AssertionError as exc:
                first = str(exc).strip().splitlines()[0]
                result.changed.append(f"{name}: {first}")
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default=".validation-cache", help="where to keep the corpus")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    parser.add_argument("--only", default=None, help="run one tutorial by name")
    args = parser.parse_args(argv)

    work = fetch(Path(args.cache).resolve())
    names = [n for n in TUTORIALS if args.only in (None, n)]
    if not names:
        parser.error(f"no tutorial called {args.only!r}")

    here = Path.cwd()
    import os

    os.chdir(work)  # the tutorials read "data/titanic.csv" relative to cwd
    try:
        results = [check(work / f"{n}.py", work) for n in names]
    finally:
        os.chdir(here)

    import pandas as pd

    summary = {
        "pandas_ref": PANDAS_REF,
        "pandas_version": pd.__version__,
        "scripts": len(results),
        "raised": sum(1 for r in results if r.raised),
        "changed": sum(1 for r in results if r.changed),
        "frames_compared": sum(r.compared for r in results),
        "steps": sum(r.steps for r in results),
        "approximate": sum(r.approximate for r in results),
        "results": [asdict(r) for r in results],
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"corpus: pandas {PANDAS_REF} tutorials | running under pandas {pd.__version__}\n")
        print(f"{'tutorial':<28}{'frames':>8}{'steps':>7}{'approx':>8}  result")
        print("-" * 68)
        for r in results:
            verdict = "ok" if r.ok else (r.raised or "; ".join(r.changed))
            print(f"{r.name:<28}{r.compared:>8}{r.steps:>7}{r.approximate:>8}  {verdict}")
        print("-" * 68)
        print(
            f"{'total':<28}{summary['frames_compared']:>8}{summary['steps']:>7}"
            f"{summary['approximate']:>8}  "
            f"{summary['raised']} raised, {summary['changed']} changed"
        )
        ops = sorted({op for r in results for op in r.approximate_ops})
        if ops:
            print(f"\napproximate steps came from: {', '.join(ops)}")

    return 1 if summary["raised"] or summary["changed"] else 0


if __name__ == "__main__":
    sys.exit(main())
