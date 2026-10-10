from pathlib import Path

import yaml


def test_workflow_has_two_schedules_and_manual_full_mode():
    workflow = yaml.safe_load(Path(".github/workflows/update-data.yml").read_text())

    schedules = [item["cron"] for item in workflow["on"]["schedule"]]
    assert schedules == ["35 7 * * 1-5", "5 9 * * 1-5"]
    full = workflow["on"]["workflow_dispatch"]["inputs"]["full"]
    assert full["type"] == "boolean"
    assert full["default"] is False


def test_workflow_restores_and_always_saves_cross_runner_checkpoints():
    workflow = yaml.safe_load(Path(".github/workflows/update-data.yml").read_text())
    steps = workflow["jobs"]["update"]["steps"]

    restore_index, restore = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/cache/restore@v4"
    )
    update_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Update market data"
    )
    save_index, save = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/cache/save@v4"
    )

    checkpoint_path = "work/market-data-checkpoints"
    stable_prefix = "market-data-checkpoints-v4-akshare-unadjusted-${{ runner.os }}-"
    assert restore_index < update_index < save_index
    assert restore["id"] == "checkpoint_cache"
    assert restore["with"]["path"] == checkpoint_path
    assert restore["with"]["key"] == (
        stable_prefix + "${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert restore["with"]["restore-keys"].strip() == stable_prefix
    assert save["if"] == "always()"
    assert save["with"] == {
        "path": checkpoint_path,
        "key": "${{ steps.checkpoint_cache.outputs.cache-primary-key }}",
    }


def test_workflow_reuses_one_checkpoint_generation_across_run_attempts():
    workflow = yaml.safe_load(Path(".github/workflows/update-data.yml").read_text())
    update = next(
        step
        for step in workflow["jobs"]["update"]["steps"]
        if step.get("name") == "Update market data"
    )

    assert update["env"]["MARKET_DATA_CHECKPOINT_GENERATION"] == (
        "${{ github.run_id }}"
    )


def test_offline_tests_precede_network_fetch():
    workflow = yaml.safe_load(Path(".github/workflows/update-data.yml").read_text())
    names = [step.get("name") for step in workflow["jobs"]["update"]["steps"]]
    assert names.index("Test data pipeline before fetching") < names.index(
        "Update market data"
    )


def test_pr_ci_is_offline_and_has_no_publish_step():
    workflow = yaml.safe_load(Path(".github/workflows/test-data.yml").read_text())
    assert "pull_request" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    commands = [step.get("run", "") for step in workflow["jobs"]["test"]["steps"]]
    assert "python -m pytest tests/data -v" in commands
    assert not any(
        "update_market_data.py" in command or "git push" in command
        for command in commands
    )
