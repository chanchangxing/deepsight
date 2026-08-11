import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.market_data.pipeline import (
    MarketDataPipeline,
    MarketDataValidationError,
)


INDEX_DATES = [
    "2025-01-02",
    "2025-01-03",
    "2025-01-06",
    "2025-01-07",
    "2025-01-08",
    "2025-01-09",
    "2025-01-10",
    "2025-01-13",
]


class FixtureClient:
    def __init__(self, duplicate_symbols=False, close_delta=0.0):
        self.duplicate_symbols = duplicate_symbols
        self.close_delta = close_delta
        self.history_requests = []

    def get_index_history(self, start, end):
        return pd.DataFrame(
            {
                "date": INDEX_DATES,
                "close": [
                    value + self.close_delta
                    for value in [3200.0, 3210.0, 3205.0, 3220.0, 3215.0, 3230.0, 3225.0, 3240.0]
                ],
            }
        )

    def get_current_market(self):
        symbols = ["600000", "000001"]
        if self.duplicate_symbols:
            symbols.append("600000")
        return pd.DataFrame(
            {
                "symbol": symbols,
                "exchange": ["SH", "SZ"] + (["SH"] if self.duplicate_symbols else []),
                "current_close": [10.0, 20.0] + ([10.0] if self.duplicate_symbols else []),
                "previous_close": [9.0, 20.0] + ([9.0] if self.duplicate_symbols else []),
                "is_suspended": [False, False] + ([False] if self.duplicate_symbols else []),
            }
        )

    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        closes = {
            "600000": [9.0, 10.0, 11.0, 10.0, 10.0, 12.0, 11.0, 12.0, 13.0],
            "000001": [20.0, 20.0, 19.0, 19.0, 22.0, 21.0, 21.0, 20.0, 21.0],
        }[symbol]
        return pd.DataFrame(
            {
                "date": ["2024-12-31", *INDEX_DATES],
                "symbol": [symbol] * 9,
                "current_close": closes,
            }
        )


def make_pipeline(tmp_path: Path, client=None) -> MarketDataPipeline:
    return MarketDataPipeline(root=tmp_path, client=client or FixtureClient())


def test_rerun_replaces_date_and_rebuilds_cumulative(tmp_path):
    pipeline = make_pipeline(tmp_path)

    pipeline.run(date(2025, 1, 6))
    pipeline.run(date(2025, 1, 6))

    frame = pd.read_csv(tmp_path / "data/market_history.csv")
    assert frame["date"].is_unique
    assert frame["cumulative_diff"].tolist() == [1, 1, 0]
    assert json.loads((tmp_path / "public/data/market_history.json").read_text()) == frame.to_dict(
        "records"
    )


def test_fills_missing_index_day_outside_recent_five_day_window(tmp_path):
    pipeline = make_pipeline(tmp_path)
    pipeline.run(date(2025, 1, 13), full=True)
    history_path = tmp_path / "data/market_history.csv"
    history = pd.read_csv(history_path)
    history = history.loc[history["date"] != "2025-01-03"]
    history["cumulative_diff"] = history["daily_diff"].cumsum()
    history.to_csv(history_path, index=False)

    result = pipeline.run(date(2025, 1, 13))

    frame = pd.read_csv(history_path)
    assert "2025-01-03" in result.updated_dates
    assert frame["date"].tolist() == INDEX_DATES
    assert frame["cumulative_diff"].tolist() == [1, 1, 0, 1, 1, 0, 0, 2]


def test_validation_failure_preserves_published_csv_and_json(tmp_path):
    published_pipeline = make_pipeline(tmp_path)
    published_pipeline.run(date(2025, 1, 6))
    csv_path = tmp_path / "data/market_history.csv"
    json_path = tmp_path / "public/data/market_history.json"
    before_csv = csv_path.read_bytes()
    before_json = json_path.read_bytes()

    broken_pipeline = make_pipeline(tmp_path, FixtureClient(duplicate_symbols=True))

    with pytest.raises(MarketDataValidationError, match="duplicate symbols"):
        broken_pipeline.run(date(2025, 1, 7))

    assert csv_path.read_bytes() == before_csv
    assert json_path.read_bytes() == before_json


def test_second_atomic_replace_failure_restores_both_published_files(tmp_path, monkeypatch):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    csv_path = tmp_path / "data/market_history.csv"
    json_path = tmp_path / "public/data/market_history.json"
    before_csv = csv_path.read_bytes()
    before_json = json_path.read_bytes()
    original_replace = os.replace

    def fail_json_replace(source, destination):
        if Path(destination) == json_path:
            raise OSError("simulated JSON replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr("scripts.market_data.pipeline.os.replace", fail_json_replace)

    with pytest.raises(OSError, match="simulated JSON replacement failure"):
        make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(date(2025, 1, 6))

    assert csv_path.read_bytes() == before_csv
    assert json_path.read_bytes() == before_json


def test_rejects_existing_dates_that_are_not_index_trading_days(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    csv_path = tmp_path / "data/market_history.csv"
    history = pd.read_csv(csv_path)
    weekend = history.iloc[[1]].copy()
    weekend["date"] = "2025-01-04"
    weekend["daily_diff"] = 0
    history = pd.concat([history, weekend], ignore_index=True).sort_values("date")
    history["cumulative_diff"] = history["daily_diff"].cumsum()
    history.to_csv(csv_path, index=False)
    before = csv_path.read_bytes()

    with pytest.raises(MarketDataValidationError, match="non-trading dates"):
        make_pipeline(tmp_path).run(date(2025, 1, 6))

    assert csv_path.read_bytes() == before


def test_update_cli_runs_as_a_script_without_import_path_setup():
    script = Path(__file__).parents[2] / "scripts" / "update_market_data.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "--full" in result.stdout
