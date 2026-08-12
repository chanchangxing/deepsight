import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.market_data.akshare_client import AkshareClient
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
    def __init__(
        self,
        duplicate_symbols=False,
        close_delta=0.0,
        include_delisted=False,
        suspended_on=None,
        suspended_close=None,
        invalid_symbols=(),
        invalid_on=(),
        omit_symbols=(),
        close_overrides=None,
    ):
        self.duplicate_symbols = duplicate_symbols
        self.close_delta = close_delta
        self.include_delisted = include_delisted
        self.suspended_on = suspended_on
        self.suspended_close = suspended_close
        self.invalid_symbols = set(invalid_symbols)
        self.invalid_on = set(invalid_on)
        self.omit_symbols = set(omit_symbols)
        self.close_overrides = close_overrides or {}
        self.history_requests = []

    def get_index_history(self, start, end):
        dates = ["2024-12-31", *INDEX_DATES]
        closes = [3190.0, 3200.0, 3210.0, 3205.0, 3220.0, 3215.0, 3230.0, 3225.0, 3240.0]
        return pd.DataFrame(
            {"date": dates, "close": [value + self.close_delta for value in closes]}
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

    def get_security_universe(self):
        result = self.get_current_market().loc[:, ["symbol", "exchange"]].copy()
        result["listing_date"] = "2020-01-01"
        result["delisting_date"] = None
        if self.include_delisted:
            result = pd.concat(
                [
                    result,
                    pd.DataFrame(
                        {
                            "symbol": ["600999"],
                            "exchange": ["SH"],
                            "listing_date": ["2020-01-01"],
                            "delisting_date": ["2025-01-07"],
                        }
                    ),
                ],
                ignore_index=True,
            )
        return result

    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        closes = {
            "600000": [9.0, 10.0, 11.0, 10.0, 10.0, 12.0, 11.0, 12.0, 13.0],
            "000001": [20.0, 20.0, 19.0, 19.0, 22.0, 21.0, 21.0, 20.0, 21.0],
            "600999": [10.0, 11.0, 12.0, 13.0, 14.0, None, None, None, None],
        }[symbol]
        if self.suspended_on and symbol == "600000":
            closes = list(closes)
            closes[INDEX_DATES.index(self.suspended_on) + 1] = self.suspended_close
        if symbol in self.invalid_symbols and not self.invalid_on:
            closes = [None] * len(closes)
        elif symbol in self.invalid_symbols:
            closes = list(closes)
            for day in self.invalid_on:
                closes[INDEX_DATES.index(day) + 1] = None
        for (override_symbol, override_date), value in self.close_overrides.items():
            if symbol == override_symbol:
                closes = list(closes)
                closes[INDEX_DATES.index(override_date) + 1] = value
        frame = pd.DataFrame(
            {
                "date": ["2024-12-31", *INDEX_DATES],
                "symbol": [symbol] * 9,
                "current_close": closes,
                "is_suspended": [
                    symbol == "600000" and value == self.suspended_on
                    for value in ["2024-12-31", *INDEX_DATES]
                ],
            }
        )
        return frame.loc[~((frame["symbol"].isin(self.omit_symbols)) & (frame["date"].isin(self.invalid_on)))].reset_index(drop=True)


class HistoryShapeApi:
    def __init__(self, fixture):
        self.fixture = fixture

    def stock_zh_a_hist(self, **kwargs):
        symbol = kwargs["symbol"]
        frame = FixtureClient.get_stock_history(
            self.fixture, symbol, "2024-12-31", "2025-01-07"
        )
        result = frame.rename(
            columns={"date": "日期", "symbol": "股票代码", "current_close": "收盘"}
        ).loc[:, ["日期", "股票代码", "收盘"]]
        result["成交量"] = 100.0
        result["成交额"] = 1000.0
        if symbol == "600000":
            suspended = result["日期"] == "2025-01-06"
            result.loc[suspended, "成交量"] = 0.0
            result.loc[suspended, "成交额"] = 0.0
        return result


class ProductionShapeFixtureClient(FixtureClient):
    def __init__(self):
        super().__init__(
            suspended_on="2025-01-06",
            suspended_close=99.0,
            close_overrides={("600000", "2025-01-07"): 12.0},
        )
        self.history_client = AkshareClient(api=HistoryShapeApi(self))

    def get_stock_history(self, symbol, start, end):
        return self.history_client.get_stock_history(symbol, start, end)


def make_pipeline(tmp_path: Path, client=None) -> MarketDataPipeline:
    return MarketDataPipeline(root=tmp_path, client=client or FixtureClient())


def published_paths(tmp_path):
    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    public_data = tmp_path / "public/data"
    return public_data / manifest["csv"], public_data / manifest["json"]


def test_rerun_replaces_date_and_rebuilds_cumulative(tmp_path):
    pipeline = make_pipeline(tmp_path)

    pipeline.run(date(2025, 1, 6))
    pipeline.run(date(2025, 1, 6))

    _, json_path = published_paths(tmp_path)
    csv_path, _ = published_paths(tmp_path)
    frame = pd.read_csv(csv_path)
    assert frame["date"].is_unique
    assert frame["cumulative_diff"].tolist() == [1, 1, 0]
    assert json.loads(json_path.read_text()) == frame.to_dict(
        "records"
    )


def test_fills_missing_index_day_outside_recent_five_day_window(tmp_path):
    pipeline = make_pipeline(tmp_path)
    pipeline.run(date(2025, 1, 13), full=True)
    history_path, _ = published_paths(tmp_path)
    history = pd.read_csv(history_path)
    history = history.loc[history["date"] != "2025-01-03"]
    history["cumulative_diff"] = history["daily_diff"].cumsum()
    history.to_csv(history_path, index=False)

    result = pipeline.run(date(2025, 1, 13))

    current_csv, _ = published_paths(tmp_path)
    frame = pd.read_csv(current_csv)
    assert "2025-01-03" in result.updated_dates
    assert frame["date"].tolist() == INDEX_DATES
    assert frame["cumulative_diff"].tolist() == [1, 1, 0, 1, 1, 0, 0, 2]


def test_validation_failure_preserves_published_csv_and_json(tmp_path):
    published_pipeline = make_pipeline(tmp_path)
    published_pipeline.run(date(2025, 1, 6))
    csv_path, json_path = published_paths(tmp_path)
    before_csv = csv_path.read_bytes()
    before_json = json_path.read_bytes()

    broken_pipeline = make_pipeline(tmp_path, FixtureClient(duplicate_symbols=True))

    with pytest.raises(MarketDataValidationError, match="duplicate symbols"):
        broken_pipeline.run(date(2025, 1, 7))

    assert csv_path.read_bytes() == before_csv
    assert json_path.read_bytes() == before_json


def test_manifest_is_unchanged_when_version_write_fails(tmp_path, monkeypatch):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    manifest_path = tmp_path / "public/data/current.json"
    before = manifest_path.read_bytes()
    original_replace = os.replace

    def fail_version_json_replace(source, destination):
        if str(destination).endswith("market_history.json"):
            raise OSError("simulated version JSON replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr("scripts.market_data.pipeline.os.replace", fail_version_json_replace)

    with pytest.raises(OSError, match="simulated version JSON replacement failure"):
        make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(date(2025, 1, 6))

    assert manifest_path.read_bytes() == before


def test_rejects_existing_dates_that_are_not_index_trading_days(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    csv_path, _ = published_paths(tmp_path)
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


def test_publishes_immutable_version_and_switches_only_current_manifest(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    manifest_path = tmp_path / "public/data/current.json"
    first_manifest = json.loads(manifest_path.read_text())
    first_json = tmp_path / "public/data" / first_manifest["json"]
    first_csv = tmp_path / "public/data" / first_manifest["csv"]

    make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(date(2025, 1, 6))

    second_manifest = json.loads(manifest_path.read_text())
    assert second_manifest["version"] != first_manifest["version"]
    assert first_json.exists() and first_csv.exists()
    assert (tmp_path / "public/data" / second_manifest["json"]).exists()
    assert (tmp_path / "public/data" / second_manifest["csv"]).exists()


def test_full_history_keeps_delisted_stock_until_its_delisting_date(tmp_path):
    make_pipeline(tmp_path, FixtureClient(include_delisted=True)).run(date(2025, 1, 8), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["csv"])

    assert frame.loc[frame["date"] == "2025-01-06", "valid_count"].item() == 3
    assert frame.loc[frame["date"] == "2025-01-08", "valid_count"].item() == 2


def test_resumed_stock_uses_its_previous_valid_close_after_suspension(tmp_path):
    make_pipeline(
        tmp_path, FixtureClient(suspended_on="2025-01-06", suspended_close=None)
    ).run(date(2025, 1, 7), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["csv"])

    assert frame.loc[frame["date"] == "2025-01-07", "up_count"].item() == 1
    assert frame.loc[frame["date"] == "2025-01-07", "valid_count"].item() == 2


def test_resumed_stock_ignores_positive_stale_close_on_suspended_day(tmp_path):
    make_pipeline(
        tmp_path,
        FixtureClient(
            suspended_on="2025-01-06",
            suspended_close=99.0,
            close_overrides={("600000", "2025-01-07"): 12.0},
        ),
    ).run(date(2025, 1, 7), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["csv"])

    assert frame.loc[frame["date"] == "2025-01-07", "up_count"].item() == 2
    assert frame.loc[frame["date"] == "2025-01-07", "down_count"].item() == 0


def test_production_history_shape_marks_zero_turnover_stale_close_as_suspended(tmp_path):
    make_pipeline(tmp_path, ProductionShapeFixtureClient()).run(date(2025, 1, 7), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["csv"])

    assert frame.loc[frame["date"] == "2025-01-07", "up_count"].item() == 2
    assert frame.loc[frame["date"] == "2025-01-07", "down_count"].item() == 0


def test_rejects_first_run_when_active_stock_is_completely_missing_for_the_day(tmp_path):
    with pytest.raises(MarketDataValidationError, match="missing price/comparison ratio"):
        make_pipeline(
            tmp_path,
            FixtureClient(omit_symbols={"600000"}, invalid_on={"2025-01-02"}),
        ).run(date(2025, 1, 2))

    assert not (tmp_path / "public/data/current.json").exists()


def test_first_published_index_change_uses_prior_trading_day_close(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 2))

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["csv"])

    assert frame.loc[0, "shanghai_change"] == 10.0
    assert frame.loc[0, "shanghai_change_pct"] == pytest.approx(0.3134796238244514)


def test_rejects_abnormal_valid_stock_count_drop_before_manifest_switch(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 6), full=True)
    before = (tmp_path / "public/data/current.json").read_bytes()

    with pytest.raises(MarketDataValidationError, match="valid stock count dropped"):
        make_pipeline(tmp_path, FixtureClient(invalid_symbols={"600000"}, invalid_on={"2025-01-07"})).run(date(2025, 1, 7))

    assert (tmp_path / "public/data/current.json").read_bytes() == before


def test_rejects_excessive_missing_price_or_comparison_ratio_before_publish(tmp_path):
    with pytest.raises(MarketDataValidationError, match="missing price/comparison ratio"):
        make_pipeline(tmp_path, FixtureClient(invalid_symbols={"600000"})).run(date(2025, 1, 6))

    assert not (tmp_path / "public/data/current.json").exists()
