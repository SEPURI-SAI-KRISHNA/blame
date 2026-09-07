# Roadmap

`blame` is P1 in the Glass-Box Stack: the on-ramp product whose users tell
Project C (the provenance-native query engine) what to build. See
`../P1-blame-for-dataframes.md` for the full plan and kill criteria.

## v0.1 — the tracer (done)

- [x] Patch pandas: filters, slices, projections, sorts, dedup, dropna, joins,
      concat, group-by aggregations, row-preserving transforms
- [x] Lineage vectors: identity, select, join, group (CSR), concat, unknown
- [x] Content-addressed column store with reference columns (a filtered column
      is stored as a pointer plus a row mapping, not as bytes)
- [x] `why` / `forward` / `steps` / `at`, in Python and on the CLI
- [x] Approximate-lineage flagging for `apply` / `map` / `transform`
- [x] Ground-truth test suite, overhead and scaling benchmarks
- [x] Untraced operations detected via `__finalize__` and reported as explicit
      approximate steps instead of silently becoming fake sources
- [x] The depth guard held across lineage derivation, so pandas calls `blame`
      makes on its own behalf cannot enter the graph as steps or sources

## v0.2 — the queries people actually ask

- [ ] `why` restricted by column, using column-level provenance rather than
      reporting every source row that fed the output row
- [ ] `how(cell)` — the derivation expression, not just the input rows
- [ ] A terminal UI to scrub steps, with shapes, schemas and null counts
- [ ] `pivot_table`, `melt`, `stack`/`unstack`, window functions
- [ ] Series-level operations as first-class steps

## v0.3 — the web UI (done)

- [x] `blame ui` / `run.ui()`: a local page, DAG of frames on the left, grid on
      the right, click a cell to light up its source rows in every frame
- [x] Answers come from the same `why` / `forward` / `highlight` the CLI uses,
      so the page cannot disagree with the terminal
- [x] `Run.highlight(row)` — the whole connected set, upstream and downstream,
      not just the endpoints `why` reports
- [x] Paging, so a million-row frame opens on the rows that matter
- [x] The selection lives in the URL, so one cell's explanation is a link
- [ ] Record the GIF and put it at the top of the README (still a static shot)

### Not in the page yet

- Column-level highlighting: clicking a cell narrows the *rows*, not the
  columns, so it reports every source row that fed the output row.
- No editing, no re-running, no diff between two runs.
- Frames are materialized in full on the server to serve one page of rows.
  Fine at a few million; a lazy per-page read is the fix when it isn't.

## v0.4 — run diff

- [ ] `blame diff run1 run2`: row-level differences with lineage-explained
      causes ("row 812 changed because input row 91 changed `price`")
- [ ] pytest integration: assert no unexplained diffs between runs

## v1 — mixed pipelines

- [ ] SQL steps via sqlglot for column lineage, DuckDB with row-id tagging
- [ ] dbt model lineage, so a pandas + SQL pipeline traces end to end
- [ ] polars, using the lazy plan instead of monkeypatching

## Later

- [ ] Spark via listener plus sampled lineage
- [ ] Replace the tracer with the Project C engine for full-fidelity mode

## Known limits in v0.1

- `apply`/`map`/`transform` assume row identity and are flagged approximate.
- Reshaping operations (`pivot_table`, `unstack`, `melt`, transpose) are not
  traced row by row. They are detected and reported as approximate steps, so
  answers through them widen to every candidate row rather than being wrong.
- A parent frame whose index has duplicates and which was not produced by a
  traced operation falls back to approximate lineage.
- Column provenance is by name; a renamed or computed column is marked derived
  rather than traced to the expression that made it.
- In-place mutation (`df["x"] = ...`) is not recorded as a step, but row
  lineage survives it (covered by a test). `df.loc[mask, col] = ...` that
  changes values in place is not detected at all.
- The DAG is laid out by longest-path depth with no edge routing, so a long
  edge that skips layers passes behind the boxes between them.
- Tested against pandas 3.0.5 only. pandas 2.x is untested and monkeypatching
  is version-sensitive; this is the biggest portability risk in the project.
