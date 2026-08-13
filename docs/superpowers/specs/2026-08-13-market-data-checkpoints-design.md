# Market Data Checkpoint Design

## Goal

Make `python scripts/update_market_data.py --full` resumable across process or network failures without publishing or committing partial market data.

## Design

The pipeline uses a private, versioned cache namespace at
`work/market-data-checkpoints/v3-akshare-unadjusted/<baseline>/runs/<generation>/`.
The immutable `run.json` records mode, `as_of`, history start, and refresh
boundary. After a stock response passes schema normalization, the pipeline
atomically writes that generation's per-symbol CSV.

On a retry with the same generation, a completed symbol is loaded without a
second upstream request. A new full generation ignores older success data and
refetches the requested history. A new routine generation starts from the most
recent successful generation, removes the refresh interval, and refetches at
least the latest five index trading dates through `as_of`, so corrections and
deletions replace stale cached rows. Invalid metadata or per-symbol data is
never silently reused.

Each per-symbol CSV is trimmed before persistence and aggregation to the latest
positive, non-suspended row before 2025 plus rows in the statistics window.
Thus retained rows scale with symbols times window trading days rather than
total listing age. GitHub Actions restores the stable cache path and passes
`github.run_id` as the generation, so run attempts resume while separate runs
receive fresh generations. Changing source or adjustment semantics requires a
new namespace version. After the success pointer advances, superseded completed
generations are removed so the persisted cache remains bounded; a failed
generation remains available until a later successful generation replaces it.

## Alternatives Considered

- A single aggregate pickle is simpler, but one interrupted write or corrupt file invalidates all completed work and the format is harder to inspect safely.
- Fetching and checkpointing by trading date does not match AKShare's per-symbol history endpoint and would multiply requests substantially.

## Safety and Tests

- Checkpoints remain under the repository's ignored `work/` directory and cannot become public artifacts through the data publisher.
- A regression test forces a failure after one stock succeeds, then reruns and asserts the completed symbol is not requested again.
- Regression tests prove new full and routine generations absorb upstream revisions.
- A decades-long response and 120-symbol synthetic fixture bound retained rows to one baseline plus window rows per symbol.
- Existing atomic-publication tests continue to prove that failures preserve the active manifest and data.
- A failed generation is tested to resume already completed symbols.
