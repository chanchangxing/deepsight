# A-share market breadth

This site charts the daily breadth of eligible Shanghai and Shenzhen A shares
against the Shanghai Composite Index.

## Statistical definition

The universe covers ordinary A-share symbols on the Shanghai and Shenzhen
exchanges. It excludes other exchange instruments and keeps delisted securities
in history through their delisting date. Suspended securities and prices without
a valid prior close are excluded from that day's comparison.

For each trading day, `daily_diff = up_count - down_count`. The displayed
cumulative breadth is the running sum:

```
cumulative_diff[t] = cumulative_diff[t - 1] + daily_diff[t]
```

The close and trading-day calendar come from the Shanghai Composite Index.

## Data publication and automatic updates

GitHub Actions runs on weekdays at 15:35 and 17:05 Beijing time (the UTC cron
values are `35 7 * * 1-5` and `5 9 * * 1-5`). The two windows accommodate
post-close source availability. Each run refreshes the recent trading days,
validates the complete data pipeline, and creates a commit only when published
data actually changed.

Offline data tests run before any live fetch, and a separate read-only PR workflow
checks the adapter and pipeline. The AKShare client retries transient network,
timeout, rate-limit, and server failures with delays of 30, 90, and 180 seconds.
Stock and index history switch from Sina to Eastmoney after a controlled network,
parse, or response-schema failure. Current listings can use the two Eastmoney
exchange lists if the exchange-backed list is unavailable; delisted lists remain
required. Every candidate response must satisfy the same data contract.
Source failures log structured stage, symbol, source, date range, and exception
records. A failed stock does not stop collecting other symbols: successful
checkpoints are saved and failures are recorded in the private run directory.
Any stock fetch failure blocks publication. A failed fetch or validation fails
the Action and
does not advance the published version pointer, so the previous release remains
available. Inspect the failed run in the repository's **Actions** tab for the
command and error output before retrying.

Published files are immutable under `public/data/versions/<uuid>/`. The
`public/data/current.json` manifest is the version pointer consumed by the site;
it changes only after a new version has been fully written. The canonical
`data/market_history.csv` is a derived review mirror: every run atomically
reconciles it byte-for-byte from the version selected by the manifest before
doing any market work. The manifest remains the site's sole live release
pointer and makes interrupted mirror updates deterministically recoverable.

Private per-symbol checkpoints live below `work/market-data-checkpoints/` and
are restored by GitHub Actions across runs. They are namespaced by source and
data semantics, validated before reuse, and grouped by workflow generation.
Failed runs can reuse completed symbols from another failed generation only
within 12 hours and with identical mode, date bounds, AKShare/adapter version,
index coverage and security-universe fingerprints. CSV hashes, row counts,
symbols, and coverage bounds must also validate. Expired or incompatible runs
refetch; old v3 checkpoints are ignored. A new full generation after success
refetches history, while routine generations refresh at least the latest five
trading days so upstream corrections replace cached rows. Each symbol cache
retains only one valid pre-2025 baseline row plus the 2025-onward window.

To recover historical data, open **Actions → Update market breadth data → Run
workflow**, tick **full**, and run it. This rebuilds history from 2025-01-01.

## Local commands

```bash
python -m pip install -r requirements.txt
python scripts/update_market_data.py          # routine refresh
python scripts/update_market_data.py --full   # full rebuild from 2025-01-01
python -m pytest tests/data -v                # offline data and workflow tests
npm install
npm test -- --run
npm run build
```
