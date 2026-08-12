from pathlib import Path

import yaml


def test_workflow_has_two_schedules_and_manual_full_mode():
    workflow = yaml.safe_load(Path(".github/workflows/update-data.yml").read_text())

    schedules = [item["cron"] for item in workflow["on"]["schedule"]]
    assert schedules == ["35 7 * * 1-5", "5 9 * * 1-5"]
    full = workflow["on"]["workflow_dispatch"]["inputs"]["full"]
    assert full["type"] == "boolean"
    assert full["default"] is False
