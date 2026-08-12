# Market Data Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make full AKShare history collection resume after interruption without publishing partial data.

**Architecture:** `MarketDataPipeline` stores validated, end-dated CSV checkpoints per symbol under an ignored, source-versioned namespace. Reads validate the cached schema and coverage, writes are atomic, and later runs extend only the uncovered tail.

**Tech Stack:** Python 3.9+, pandas, pytest, filesystem atomic replace.

## Global Constraints

- Checkpoints live only below `work/market-data-checkpoints/v2-akshare-unadjusted/<baseline>/<symbol>/`.
- Partial histories must never be copied into `data/` or `public/data/`.
- Existing canonical manifest keys are `history_json` and `history_csv`; old keys remain read-compatible only.
- Successful and failed runs preserve valid completed-symbol checkpoints for reuse.

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

### Task 2: Reuse checkpoints across later publication dates

**Files:**
- Modify: `scripts/market_data/pipeline.py`
- Test: `tests/data/test_pipeline.py`

**Interfaces:**
- Consumes: the source-versioned per-symbol checkpoints created in Task 1.
- Produces: successful `run(...)` that retains checkpoints and fetches only an uncovered tail on a later `as_of` date.

- [ ] **Step 1: Write a failing incremental-reuse test**

Run a full fixture update, advance `as_of`, and assert the next stock request begins one day after the prior checkpoint's covered end.

- [ ] **Step 2: Verify the test fails because the old range cache cannot extend**

Run: `python3 -m pytest tests/data/test_pipeline.py::test_successful_publish_keeps_checkpoint_for_later_as_of -v`

Expected: FAIL because the old implementation refetches the full range.

- [ ] **Step 3: Implement source-versioned coverage checkpoints**

Select the newest valid checkpoint, fetch only the uncovered tail, merge and validate it, then atomically write the new end-dated checkpoint. Retain it after publication.

- [ ] **Step 4: Verify incremental reuse and all data tests**

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
