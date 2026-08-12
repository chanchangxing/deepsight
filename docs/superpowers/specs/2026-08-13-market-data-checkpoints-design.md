# Market Data Checkpoint Design

## Goal

Make `python scripts/update_market_data.py --full` resumable across process or network failures without publishing or committing partial market data.

## Design

The pipeline uses a private, versioned cache namespace at
`work/market-data-checkpoints/v2-akshare-unadjusted/<baseline>/<symbol>/`.
Each file name records the covered end date. After a stock response has passed
the existing adapter schema normalization, the pipeline writes that stock's
normalized history to its own CSV file using a temporary file, `fsync`, and
`os.replace`.

On a retry or a later `as_of` date, the newest valid per-stock checkpoint is
loaded. If it already covers the requested end, no stock-history request is
made. Otherwise only the uncovered tail is fetched and atomically merged into
a new checkpoint. A missing, unreadable, schema-invalid, symbol-mismatched, or
range-invalid checkpoint is ignored and fetched again; it is never used
silently. The security universe and index history are always refreshed, so the
resumed run still discovers newly relevant symbols and validates the requested
trading dates.

Checkpoints persist after publication so the next `as_of` date can extend them.
GitHub Actions restores the stable cache path before the updater and saves it
with `if: always()`, so completed symbols also survive failed runs. Changing the
source or adjustment semantics requires a new namespace version.

## Alternatives Considered

- A single aggregate pickle is simpler, but one interrupted write or corrupt file invalidates all completed work and the format is harder to inspect safely.
- Fetching and checkpointing by trading date does not match AKShare's per-symbol history endpoint and would multiply requests substantially.

## Safety and Tests

- Checkpoints remain under the repository's ignored `work/` directory and cannot become public artifacts through the data publisher.
- A regression test forces a failure after one stock succeeds, then reruns and asserts the completed symbol is not requested again.
- A regression test advances `as_of` and asserts only the uncovered date tail is requested.
- Existing atomic-publication tests continue to prove that failures preserve the active manifest and data.
- A successful run is tested to retain reusable per-symbol checkpoints.
