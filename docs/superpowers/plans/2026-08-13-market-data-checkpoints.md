# Market Data Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make full AKShare history collection resume after interruption without publishing partial data.

**Architecture:** `MarketDataPipeline` stores one validated CSV checkpoint per symbol under an ignored request-range directory. Reads validate the cached schema, writes are atomic, and only a successful publish removes that exact checkpoint directory.

**Tech Stack:** Python 3.9+, pandas, pytest, filesystem atomic replace.

## Global Constraints

- Checkpoints live only below `work/market-data-checkpoints/<start>_<end>/`.
- Partial histories must never be copied into `data/` or `public/data/`.
- Existing canonical manifest keys are `history_json` and `history_csv`; old keys remain read-compatible only.
- A successful publish clears the request-range checkpoint directory; failures preserve completed symbols.

---

### Task 1: Resume per-symbol history collection

**Files:**
- Modify: `scripts/market_data/pipeline.py`
- Test: `tests/data/test_pipeline.py`

**Interfaces:**
- Consumes: `client.get_stock_history(symbol, start, end) -> DataFrame`.
- Produces: `_stock_history(symbol, start, end) -> DataFrame`, backed by `<checkpoint_dir>/<symbol>.csv`.

- [ ] **Step 1: Write a failing interruption/resume test**

Add a client that succeeds for `600000`, fails once for `000001`, then succeeds. The first pipeline run must fail. A second pipeline instance with the same root and client must complete, and `600000` must have exactly one upstream history request across both runs.

- [ ] **Step 2: Verify the test fails for repeated requests**

Run: `python3 -m pytest tests/data/test_pipeline.py::test_full_run_resumes_completed_stock_history_after_failure -v`

Expected: FAIL because `600000` is requested twice.

- [ ] **Step 3: Implement the minimal atomic per-symbol cache**

Add a request-range checkpoint path, a strict cached CSV reader requiring `date`, `symbol`, `current_close`, and `is_suspended`, and an atomic CSV writer using the existing `_write_temp` plus `os.replace`. Route stock history collection through this helper.

- [ ] **Step 4: Verify resume passes and run pipeline regression tests**

Run: `python3 -m pytest tests/data/test_pipeline.py::test_full_run_resumes_completed_stock_history_after_failure -v && python3 -m pytest tests/data/test_pipeline.py -q`

Expected: PASS.

### Task 2: Clear checkpoints after successful publication

**Files:**
- Modify: `scripts/market_data/pipeline.py`
- Test: `tests/data/test_pipeline.py`

**Interfaces:**
- Consumes: the request-range checkpoint directory created in Task 1.
- Produces: successful `run(...)` with no remaining exact-range checkpoint directory.

- [ ] **Step 1: Write a failing cleanup test**

Run a full fixture update and assert `work/market-data-checkpoints/2024-12-31_2025-01-06` does not exist after publication.

- [ ] **Step 2: Verify the test fails because checkpoints remain**

Run: `python3 -m pytest tests/data/test_pipeline.py::test_successful_publish_removes_request_checkpoint -v`

Expected: FAIL because the checkpoint directory still exists.

- [ ] **Step 3: Implement exact-range cleanup after `_atomic_publish`**

Use `shutil.rmtree(checkpoint_dir)` only after the active manifest has switched successfully. Do not clean during exceptions.

- [ ] **Step 4: Verify cleanup and all data tests**

Run: `python3 -m pytest tests/data -v`

Expected: PASS.

### Task 3: Run and validate the real full history

**Files:**
- Modify: `data/market_history.csv`
- Modify: `public/data/current.json`
- Create: `public/data/versions/<generated-version>/market_history.csv`
- Create: `public/data/versions/<generated-version>/market_history.json`

**Interfaces:**
- Consumes: AKShare index, current-market, delisting, and unadjusted per-symbol history endpoints from 2025-01-01 through 2026-08-13.
- Produces: validated, immutable history plus canonical manifest.

- [ ] **Step 1: Run the full update**

Run: `python3 scripts/update_market_data.py --full`

Expected: all trading dates publish; interruptions can resume from saved symbols.

- [ ] **Step 2: Check real-data invariants independently**

Load the canonical manifest CSV and assert dates equal the index trading dates from 2025-01-01, are unique/increasing, all breadth and cumulative identities hold, and recent `valid_count` values stay above the configured 60% threshold relative to their prior-five-day median.

- [ ] **Step 3: Run final verification with bundled Node 24**

Run Python data tests, `npm test -- --run`, `npm run lint`, `npm run build`, and `git diff --check` with the bundled Node 24 bin prepended to `PATH`.

- [ ] **Step 4: Write the Task 6 report and commit**

Document source calls, exact date range/count, verification outputs, commit(s), and concerns in `.superpowers/sdd/2026-08-10-a-share-market-breadth/task-6-report.md`; commit all necessary code, tests, report, canonical manifest, and real data without `og.png`, deployment, push, or publish.
