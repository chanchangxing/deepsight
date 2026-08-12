# Market Data Checkpoint Design

## Goal

Make `python scripts/update_market_data.py --full` resumable across process or network failures without publishing or committing partial market data.

## Design

For each stock-history request range, the pipeline uses a private cache directory at `work/market-data-checkpoints/<start>_<end>/`. After a stock response has passed the existing adapter schema normalization, the pipeline writes that stock's normalized history to its own CSV file using a temporary file, `fsync`, and `os.replace`.

On a retry with the same range, a valid per-stock checkpoint is loaded instead of calling AKShare again. A missing, unreadable, or schema-invalid checkpoint is treated as incomplete and fetched again; it is never used silently. The security universe and index history are always refreshed, so the resumed run still discovers newly relevant symbols and validates the requested trading dates.

After the complete combined history passes all existing invariants and the immutable version plus canonical `current.json` manifest have been published, the pipeline removes only that exact range's checkpoint directory. Any failure before publication leaves completed stock checkpoints available for the next invocation.

## Alternatives Considered

- A single aggregate pickle is simpler, but one interrupted write or corrupt file invalidates all completed work and the format is harder to inspect safely.
- Fetching and checkpointing by trading date does not match AKShare's per-symbol history endpoint and would multiply requests substantially.

## Safety and Tests

- Checkpoints remain under the repository's ignored `work/` directory and cannot become public artifacts through the data publisher.
- A regression test forces a failure after one stock succeeds, then reruns and asserts the completed symbol is not requested again.
- Existing atomic-publication tests continue to prove that failures preserve the active manifest and data.
- A successful run is tested to remove its exact checkpoint directory.
