"""How long does comparing two runs take, against the size of the inputs?

The answer should scale with the size of the difference, not the size of the
data: you reach for this when a report of a million rows moved by three.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import blame

ROOT = ".blame-bench-diff"


def run(n: int, bump: float):
    rng = np.random.default_rng(0)
    orders = pd.DataFrame(
        {
            "order_id": np.arange(n),
            "cust": rng.integers(0, 500, n),
            "qty": rng.integers(1, 6, n),
            "price": rng.random(n) * 50,
        }
    )
    orders.loc[: n // 100, "price"] += bump  # move 1% of the rows
    customers = pd.DataFrame({"cust": np.arange(500), "region": rng.choice(list("nsew"), 500)})
    with blame.trace(root=ROOT) as handle:
        clean = orders.assign(total=orders.qty * orders.price)
        joined = clean.merge(customers, on="cust")
        joined.groupby("region", as_index=False)["total"].sum()
    return handle.run


def main() -> None:
    print(
        f"{'source rows':>12} {'diff':>9} {'changed out':>12} {'explained':>10} {'changed in':>11}"
    )
    for n in (10_000, 100_000, 1_000_000):
        before, after = run(n, 0.0), run(n, 5.0)
        start = time.perf_counter()
        d = before.diff(after, on=["region", "order_id"])
        elapsed = time.perf_counter() - start
        print(
            f"{n:>12,} {elapsed * 1000:>8.0f}ms {len(d.changes):>12} "
            f"{d.explained:>10} {n // 100 + 1:>11,}"
        )


if __name__ == "__main__":
    main()
