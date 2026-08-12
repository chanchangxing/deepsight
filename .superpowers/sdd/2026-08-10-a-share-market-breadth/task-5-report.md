# Task 5 report: automatic data update workflow

## RED

- Added `tests/data/test_workflow_config.py` before creating the workflow.
- `python3 -m pytest tests/data/test_workflow_config.py -v` failed as expected
  with `FileNotFoundError` for `.github/workflows/update-data.yml`.

## GREEN

- Added `.github/workflows/update-data.yml` with the two required UTC schedules,
  manual `full` dispatch input, minimal `contents: write` permission, and a
  non-cancelling concurrency group.
- The workflow uses Python 3.11, runs the daily or full pipeline, validates the
  offline data test suite, and commits only changed `public/data` and `data`
  files. The release output is the `current.json` pointer plus new immutable
  version directories.
- Added PyYAML to `requirements.txt` for the offline workflow configuration test.
- Replaced the starter README with market breadth methodology, schedule,
  retry/no-publish behavior, manual full recovery, version-pointer details, and
  local commands.
- `python3 -m pytest tests/data/test_workflow_config.py -v` passed.

## Verification

- `python3 -m pytest tests/data -v`: 35 passed (34 pre-existing pandas
  deprecation warnings).
- `npm test -- --run`: unable to start because local Node is v16.13.1 while the
  project requires Node >=22.13.0; Vitest imports `node:fs/promises.constants`,
  which Node 16 does not provide.
- `npm run build`: unable to start for the same Node incompatibility; Vinext
  imports `node:fs/promises.glob`, which Node 16 does not provide.
- `git diff --check`: passed.

## Self-review

- Confirmed schedules are `35 7 * * 1-5` and `5 9 * * 1-5` (15:35 and 17:05
  Beijing time), and manual full dispatch invokes `--full` only when selected.
- Confirmed the workflow fails visibly before the commit step when data refresh
  or validation fails, and it leaves `current.json` untouched when the pipeline
  fails.
- Confirmed commit detection scopes to actual data publication paths, including
  `public/data/current.json` and new `public/data/versions/<uuid>/` files.

## Commit

Created with `git commit -m "ci: automate post-close market updates"`.
