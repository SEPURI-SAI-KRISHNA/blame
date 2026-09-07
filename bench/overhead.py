"""Measure what tracing costs: runtime multiple and bytes on disk."""

import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import blame

ROOT = Path("/tmp/blame-bench")


def make(n, seed=0):
    rng = np.random.default_rng(seed)
    orders = pd.DataFrame({
        "order_id": np.arange(n),
        "cust": rng.integers(0, max(2, n // 20), n),
        "qty": rng.integers(-2, 8, n),
        "price": rng.random(n) * 100,
        "channel": rng.choice(["web", "app", "store"], n),
        "note": rng.choice(["a", "b", "c", "d"], n),
    })
    customers = pd.DataFrame({
        "cust": np.arange(max(2, n // 20)),
        "region": rng.choice(["north", "south", "east", "west"], max(2, n // 20)),
        "tier": rng.choice(["gold", "silver"], max(2, n // 20)),
    })
    return orders, customers


def pipeline(orders, customers):
    clean = orders[orders.qty > 0]
    clean = clean.assign(total=clean.qty * clean.price)
    clean = clean.dropna(subset=["price"])
    joined = clean.merge(customers, on="cust")
    joined = joined.sort_values("total", ascending=False)
    top = joined.drop_duplicates(subset=["cust"])
    return top.groupby(["region", "tier"], as_index=False)["total"].sum()


def timed(fn, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def main():
    sizes = [int(s) for s in (sys.argv[1:] or ["10000", "100000", "1000000"])]
    print(f"{'rows':>10} {'plain':>9} {'traced':>9} {'ratio':>7} {'capture':>9} {'disk MB':>9} {'steps':>6}")
    for n in sizes:
        orders, customers = make(n)
        plain = timed(lambda: pipeline(orders, customers))

        if ROOT.exists():
            shutil.rmtree(ROOT)
        holder = {}

        def traced():
            with blame.trace(f"bench-{n}", root=ROOT, sample_rows=None) as h:
                pipeline(orders, customers)
            holder["run"] = h.run

        wall = timed(traced, repeat=1)
        run = holder["run"]
        usage = run.store.disk_usage()
        mb = (usage["columns"] + usage["runs"]) / 1e6
        print(f"{n:>10,} {plain:>8.3f}s {wall:>8.3f}s {wall / plain:>6.2f}x "
              f"{run.capture_seconds:>8.3f}s {mb:>8.1f} {len(run.steps):>6}")

        t0 = time.perf_counter()
        run.why(row=0)
        print(f"{'':>10} why() on the result: {(time.perf_counter() - t0) * 1000:.1f} ms")
    if ROOT.exists():
        shutil.rmtree(ROOT)


if __name__ == "__main__":
    main()
