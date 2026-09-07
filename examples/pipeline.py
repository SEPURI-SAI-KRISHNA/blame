"""A small pipeline with a planted bug: one order has a duplicated customer row,
so its total is double-counted in the regional report."""

import pandas as pd
import blame

orders = pd.DataFrame({
    "order_id":    [1, 2, 3, 4, 5, 6],
    "customer_id": [10, 11, 12, 10, 13, 11],
    "qty":         [2, 1, 0, 5, 3, -1],
    "price":       [10.0, 25.0, 8.0, 4.0, 12.0, 30.0],
})
customers = pd.DataFrame({
    "customer_id": [10, 11, 12, 13, 11],       # 11 appears twice -> the bug
    "region":      ["north", "south", "north", "west", "south"],
})

with blame.trace("regional revenue") as handle:
    clean = orders[orders.qty > 0]
    clean = clean.assign(total=clean.qty * clean.price)
    joined = clean.merge(customers, on="customer_id")
    report = joined.groupby("region", as_index=False)["total"].sum()

run = handle.run
print(run.summary())
print()
print(run.table())
print()
print(report)
print()
print(run.why(row=1, col="total"))
print()
print("--- contributing source rows ---")
for label, frame in run.why(row=1, col="total").frames().items():
    print(f"{label}:")
    print(frame)
