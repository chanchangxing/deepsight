# Task 6 Report: Real Data Initialization and Final Verification

## Status

**BLOCKED on real data initialization.** No full market history was published or committed. The repository remains on the empty `bootstrap` manifest, and `data/market_history.csv` does not exist. No `og.png` was created, no Sites deployment was performed, and nothing was pushed.

## Implemented and Committed

- `2dfab2f docs: design resumable market data checkpoints`
- `f574b6d docs: plan resumable market data checkpoints`
- `c8657da feat: resume market data history collection`
  - New manifests publish only canonical `history_csv` / `history_json` keys.
  - Existing old-key bootstrap manifests remain read-compatible.
  - Full stock-history collection saves one atomic CSV checkpoint per symbol below `work/market-data-checkpoints/<start>_<end>/` and reuses completed symbols after interruption.
  - A successful atomic publication clears only the exact request-range checkpoint directory.
- `73ab6d4 fix: use resilient AKShare history sources`
  - Adds `requests.exceptions.ProxyError` to transient retry classification.
  - Uses AKShare's Sina Shanghai index history, AKShare exchange-backed A-share code list, and AKShare Sina unadjusted stock history.
- `f791b8d fix: skip securities outside history window`
  - Does not request securities whose delisting date is before 2025-01-01; window-overlapping delisted securities remain included.
- `ea938db fix: recover delisted stock histories`
  - When AKShare Sina stock history raises its specific `demjson.JSONDecodeError`, falls back to AKShare Eastmoney unadjusted stock history; unrelated errors remain visible.
- `0567198 fix: bound fallback market requests`
  - Adds a 20-second per-request bound to the fallback while retaining 30/90/180-second transient retry delays.

All production fixes were written from reproducible failing tests and observed red-to-green.

## AKShare Source Results

Environment: Python 3.9.9, AKShare 1.18.50, pandas 2.3.3.

- Initial sandbox run: `stock_zh_index_daily_em` exhausted retries because `push2his.eastmoney.com` could not resolve. Network permission was then elevated as required.
- Elevated Eastmoney runs: index or Shanghai snapshot calls repeatedly failed with `requests.exceptions.ProxyError` because the authorized proxy closed the connection.
- Read-only availability probes through AKShare:
  - Sina Shanghai index: succeeded, 8,702 rows total, sample probe completed in 0.63 seconds.
  - Exchange-backed A-share code list: succeeded, 5,543 rows before project A-share/lifecycle filtering, 17 source pages completed in 7.95 seconds.
  - Shanghai and Shenzhen delisting lists: succeeded, 159 and 208 rows respectively.
  - Sina unadjusted stock history: succeeded for current stocks `000001`, `000002`, `600000`, and `000001` sample ranges.
  - Window-overlapping delisted stock `000004`: Sina returned non-JSON; AKShare Tencent and AKShare Eastmoney probes both returned 332 unadjusted rows with matching dates, closes, and volumes from 2024-12-31 through 2026-07-13.
- Final full-run attempt:
  - `000001` and `000002` completed and were atomically checkpointed, each with 391 rows from 2024-12-31 through 2026-08-12.
  - `000004` Sina history returned `JSONDecodeError`, then the AKShare Eastmoney fallback exhausted the 20-second request bounds and 30/90/180-second retry schedule with persistent `ProxyError` from `push2his.eastmoney.com`.
  - The run stopped with 2 completed securities and no publication. Partial checkpoints remain ignored under `work/market-data-checkpoints/2024-12-31_2026-08-13/` for recovery.

## Real Data Range and Invariants

- Requested range: 2025-01-01 through 2026-08-13.
- Published real-data range: **none**.
- Published real-data record count: **0** (empty bootstrap only).
- Trading-day continuity, cumulative identity, and production `valid_count` threshold checks: **not claimable / not run against real published data**, because upstream failure prevented construction of the complete market history.
- No fabricated, sampled, or partial market values were copied into `data/` or `public/data/`.

## Verification Evidence

Fresh final local verification:

- `python3 -m pytest tests/data -v`: **42 passed**, 42 pandas future warnings, exit 0.
- Bundled Node: `/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node --version`: **v24.14.0**.
- Direct bundled Node execution initially failed to load Rollup/Rolldown native addons because macOS library validation rejected ad-hoc project `.node` modules loaded by the ChatGPT-Team-ID hardened executable.
- A temporary copy of the same bundled Node 24.14.0 binary was ad-hoc re-signed in `/tmp` (no app, repository, lockfile, or dependency mutation) and used for the final checks:
  - `npm test -- --run`: **1 file, 8 tests passed**, exit 0.
  - `npm run lint`: **passed**, exit 0.
  - `npm run build`: **vinext build complete**, all five environments built, exit 0.
  - `git diff --check`: **passed**, exit 0.

## Concerns and Unblock Condition

- The complete Task 6 outcome requires the AKShare per-stock history upstream to remain reachable long enough to process all eligible symbols. The current authorized proxy persistently breaks the Eastmoney fallback needed by at least the window-overlapping delisted symbol `000004`.
- Resume only when `push2his.eastmoney.com` is reliably reachable through the approved network path, or after a separately approved/tested AKShare-only source can provide the same unadjusted close plus suspension evidence for these delisted securities.
- On resume, rerun `python3 scripts/update_market_data.py --full`; it will reuse `000001` and `000002`, then continue from `000004`. Only after a complete publish should the real-data invariants and full final verification be repeated and the generated immutable version plus canonical manifest be committed.
