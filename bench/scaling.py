"""Does tracing cost scale with pipeline length, or is it a fixed price paid
once for the input? Same data, increasing numbers of steps."""

import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

import blame

ROOT = Path("/tmp/blame-scaling")


def make(n, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "id": np.arange(n),
            "g": rng.integers(0, 500, n),
            "a": rng.random(n) * 100,
            "b": rng.random(n) * 100,
            "c": rng.choice(list("xyz"), n),
        }
    )


def pipeline(df, steps):
    """A chain of row-mapping operations, the shape real pipelines have."""
    out = df
    for i in range(steps):
        if i % 4 == 0:
            out = out[out.a > i * 0.01]
        elif i % 4 == 1:
            out = out.sort_values("b")
        elif i % 4 == 2:
            out = out.assign(**{f"d{i}": out.a - out.b})
        else:
            out = out.drop_duplicates(subset=["id"])
    return out


def run(n, steps):
    df = make(n)
    t0 = time.perf_counter()
    pipeline(df, steps)
    plain = time.perf_counter() - t0

    if ROOT.exists():
        shutil.rmtree(ROOT)
    holder = {}
    t0 = time.perf_counter()
    with blame.trace(root=ROOT, sample_rows=None) as h:
        pipeline(df, steps)
    wall = time.perf_counter() - t0
    holder["run"] = h.run
    r = holder["run"]
    return plain, wall, r.capture_seconds, len([s for s in r.steps if not s.minor])


def main():
    n = 1_000_000
    print(f"{n:,} rows, chained row-mapping operations\n")
    print(f"{'steps':>6} {'plain':>9} {'traced':>9} {'ratio':>7} {'capture':>9} {'per step':>10}")
    base = None
    for steps in (1, 4, 8, 16, 32):
        plain, wall, capture, recorded = run(n, steps)
        if base is None:
            base = capture
        per = (capture - base) / max(1, recorded - 1)
        print(
            f"{recorded:>6} {plain:>8.3f}s {wall:>8.3f}s {wall / plain:>6.2f}x "
            f"{capture:>8.3f}s {per * 1000:>8.1f}ms"
        )
    if ROOT.exists():
        shutil.rmtree(ROOT)


if __name__ == "__main__":
    main()
