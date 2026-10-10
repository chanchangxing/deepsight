import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
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
        include_old_delisted=False,
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
        self.include_old_delisted = include_old_delisted
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
        if self.include_old_delisted:
            result = pd.concat(
                [
                    result,
                    pd.DataFrame(
                        {
                            "symbol": ["000003"],
                            "exchange": ["SZ"],
                            "listing_date": ["1991-01-14"],
                            "delisting_date": ["2002-06-14"],
                        }
                    ),
                ],
                ignore_index=True,
            )
        return result

    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        if symbol == "000003":
            raise AssertionError("out-of-window delisted symbol must not be requested")
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


class FailsOnceDuringStockHistoryClient(FixtureClient):
    def __init__(self):
        super().__init__()
        self.failed = False

    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        if symbol == "000001" and not self.failed:
            self.failed = True
            raise RuntimeError("simulated interrupted stock history request")
        history = FixtureClient.get_stock_history(self, symbol, start, end)
        self.history_requests.pop()
        return history


class RangeAwareHistoryClient(FixtureClient):
    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        history = FixtureClient.get_stock_history(self, symbol, start, end)
        self.history_requests.pop()
        return history.loc[history["date"].between(start, end)].reset_index(drop=True)


class RevisableHistoryClient(RangeAwareHistoryClient):
    def __init__(self):
        super().__init__()
        self.revisions = {}

    def get_stock_history(self, symbol, start, end):
        history = super().get_stock_history(symbol, start, end).copy()
        for (revision_symbol, revision_date), close in self.revisions.items():
            if revision_symbol == symbol:
                history.loc[history["date"] == revision_date, "current_close"] = close
        return history


class DecadesHistoryClient:
    def __init__(self, symbol_count=1):
        self.symbol_count = symbol_count
        self.history_requests = []

    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        old_dates = pd.date_range("1991-01-02", "2024-12-31", freq="B")
        window_dates = pd.date_range("2025-01-02", "2025-01-31", freq="B")
        dates = old_dates.append(window_dates)
        return pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "symbol": [symbol] * len(dates),
                "current_close": [10.0] * len(dates),
                "is_suspended": [False] * len(dates),
            }
        )


class CrossYearSuspensionClient(FixtureClient):
    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        if symbol == "600000":
            return pd.DataFrame(
                {
                    "date": [
                        "2023-12-29",
                        "2024-12-31",
                        "2025-01-02",
                        "2025-01-03",
                        "2025-01-06",
                    ],
                    "symbol": [symbol] * 5,
                    "current_close": [9.0, 9.0, 9.0, 9.0, 11.0],
                    "is_suspended": [False, True, True, True, False],
                }
            ).loc[lambda frame: frame["date"].between(start, end)]
        return FixtureClient.get_stock_history(self, symbol, start, end)


class SuspendedExchangeClient(FixtureClient):
    def get_security_universe(self):
        return pd.DataFrame(
            {
                "symbol": ["600000", "600001", "000001", "000002"],
                "exchange": ["SH", "SH", "SZ", "SZ"],
                "listing_date": ["2020-01-01"] * 4,
                "delisting_date": [None] * 4,
            }
        )

    def get_stock_history(self, symbol, start, end):
        self.history_requests.append((symbol, start, end))
        is_shanghai = symbol.startswith("6")
        return pd.DataFrame(
            {
                "date": ["2024-12-31", "2025-01-02"],
                "symbol": [symbol, symbol],
                "current_close": [10.0, 10.0 if is_shanghai else 11.0],
                "is_suspended": [False, is_shanghai],
            }
        ).loc[lambda frame: frame["date"].between(start, end)]


class HistoryShapeApi:
    def __init__(self, fixture):
        self.fixture = fixture

    def stock_zh_a_daily(self, **kwargs):
        symbol = kwargs["symbol"][2:]
        frame = FixtureClient.get_stock_history(
            self.fixture, symbol, "2024-12-31", "2025-01-07"
        )
        result = frame.rename(
            columns={"current_close": "close"}
        ).loc[:, ["date", "close"]]
        result["volume"] = 100.0
        result["amount"] = 1000.0
        if symbol == "600000":
            suspended = result["date"] == "2025-01-06"
            result.loc[suspended, "volume"] = 0.0
            result.loc[suspended, "amount"] = 0.0
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


def make_pipeline(
    tmp_path: Path, client=None, now=None, checkpoint_generation=None
) -> MarketDataPipeline:
    return MarketDataPipeline(
        root=tmp_path,
        client=client or FixtureClient(),
        now=now,
        checkpoint_generation=checkpoint_generation,
    )


def published_paths(tmp_path):
    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    public_data = tmp_path / "public/data"
    return (
        public_data / manifest["history_csv"],
        public_data / manifest["history_json"],
    )


def test_published_manifest_uses_canonical_history_path_keys(tmp_path):
    make_pipeline(
        tmp_path,
        now=lambda: datetime(2025, 1, 2, 9, 10, tzinfo=timezone.utc),
    ).run(date(2025, 1, 2))

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())

    assert set(manifest) == {
        "version",
        "history_csv",
        "history_json",
        "generated_at",
    }
    assert manifest["generated_at"] == "2025-01-02T09:10:00Z"
    assert manifest["history_csv"] == (
        f"versions/{manifest['version']}/market_history.csv"
    )
    assert manifest["history_json"] == (
        f"versions/{manifest['version']}/market_history.json"
    )


def test_successful_publish_keeps_canonical_csv_identical_to_current_version(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 6))

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    canonical = tmp_path / "data/market_history.csv"
    current_version = tmp_path / "public/data" / manifest["history_csv"]

    assert canonical.read_bytes() == current_version.read_bytes()


def test_full_run_resumes_completed_stock_history_after_failure(tmp_path):
    client = FailsOnceDuringStockHistoryClient()

    with pytest.raises(RuntimeError, match="publication blocked"):
        make_pipeline(tmp_path, client, checkpoint_generation="full-attempt-1").run(
            date(2025, 1, 6), full=True
        )

    metadata_path = (
        tmp_path
        / "work/market-data-checkpoints/v4-akshare-unadjusted"
        / "1990-12-01/runs/full-attempt-1/run.json"
    )
    metadata = json.loads(metadata_path.read_text())
    assert metadata["schema"] == 2
    assert metadata["generation"] == "full-attempt-1"
    assert metadata["mode"] == "full"
    assert metadata["as_of"] == "2025-01-06"
    assert metadata["source_version"] == "injected-unadjusted-v2"
    assert set(metadata["coverage"]) == {"index_sha256", "universe_sha256"}

    make_pipeline(tmp_path, client, checkpoint_generation="full-attempt-1").run(
        date(2025, 1, 6), full=True
    )

    requested_symbols = [symbol for symbol, _, _ in client.history_requests]
    assert requested_symbols.count("600000") == 1
    assert requested_symbols.count("000001") == 2


def test_stock_checkpoint_reuses_a_completed_symbol_within_one_generation(tmp_path):
    client = RangeAwareHistoryClient()
    pipeline = make_pipeline(tmp_path, client)

    first = pipeline._stock_history("600000", "2024-12-31", "2025-01-06")
    second = pipeline._stock_history("600000", "2024-12-31", "2025-01-06")

    assert first["date"].tolist() == [
        "2024-12-31",
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
    ]
    assert second.equals(first)
    assert client.history_requests == [("600000", "2024-12-31", "2025-01-06")]


def test_decades_response_retains_only_one_baseline_plus_window_rows(tmp_path):
    client = DecadesHistoryClient()
    pipeline = make_pipeline(
        tmp_path, client, checkpoint_generation="bounded-generation"
    )

    history = pipeline._stock_history("600000", "1990-12-01", "2025-01-31", full=True)

    assert history["date"].tolist() == [
        "2024-12-31",
        *pd.date_range("2025-01-02", "2025-01-31", freq="B").strftime("%Y-%m-%d"),
    ]
    checkpoint = pd.read_csv(
        tmp_path
        / "work/market-data-checkpoints/v4-akshare-unadjusted"
        / "1990-12-01/runs/bounded-generation/symbols/600000.csv"
    )
    assert checkpoint["date"].tolist() == history["date"].tolist()


def test_synthetic_checkpoint_rows_scale_with_window_not_listing_age(tmp_path):
    client = DecadesHistoryClient(symbol_count=120)
    pipeline = make_pipeline(
        tmp_path, client, checkpoint_generation="scale-generation"
    )

    retained_rows = 0
    for value in range(client.symbol_count):
        symbol = f"6{value:05d}"
        retained_rows += len(
            pipeline._stock_history(
                symbol, "1990-12-01", "2025-01-31", full=True
            )
        )

    january_rows = len(pd.date_range("2025-01-02", "2025-01-31", freq="B"))
    assert retained_rows <= client.symbol_count * (january_rows + 1)


def test_successful_generation_prunes_superseded_checkpoint_rows(tmp_path):
    client = DecadesHistoryClient(symbol_count=10)
    for generation in ("old-generation", "current-generation"):
        pipeline = make_pipeline(tmp_path, client, checkpoint_generation=generation)
        pipeline._prepare_checkpoint_generation(
            full=True, as_of="2025-01-31", refresh_start="2025-01-01"
        )
        for value in range(client.symbol_count):
            pipeline._stock_history(
                f"6{value:05d}", "1990-12-01", "2025-01-31", full=True
            )
        pipeline._mark_checkpoint_generation_successful()

    runs = (
        tmp_path
        / "work/market-data-checkpoints/v4-akshare-unadjusted"
        / "1990-12-01/runs"
    )
    assert [path.name for path in runs.iterdir()] == ["current-generation"]
    checkpoint_rows = sum(
        len(pd.read_csv(path)) for path in runs.rglob("symbols/*.csv")
    )
    january_rows = len(pd.date_range("2025-01-02", "2025-01-31", freq="B"))
    assert checkpoint_rows <= client.symbol_count * (january_rows + 1)


def test_new_full_generation_refetches_history_and_applies_revision(tmp_path):
    client = RevisableHistoryClient()
    make_pipeline(tmp_path, client, checkpoint_generation="full-generation-a").run(
        date(2025, 1, 10), full=True
    )
    before_requests = len(client.history_requests)
    client.revisions[("600000", "2025-01-03")] = 8.0

    make_pipeline(tmp_path, client, checkpoint_generation="full-generation-b").run(
        date(2025, 1, 10), full=True
    )

    new_requests = client.history_requests[before_requests:]
    assert ("600000", "1990-12-01", "2025-01-10") in new_requests
    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])
    revised = frame.loc[frame["date"] == "2025-01-03"].iloc[0]
    assert revised["down_count"] == 2
    assert revised["daily_diff"] == -2


def test_routine_generation_refreshes_recent_five_dates_and_applies_revision(tmp_path):
    client = RevisableHistoryClient()
    make_pipeline(tmp_path, client, checkpoint_generation="full-generation").run(
        date(2025, 1, 10), full=True
    )
    before_requests = len(client.history_requests)
    client.revisions[("600000", "2025-01-08")] = 9.0

    make_pipeline(tmp_path, client, checkpoint_generation="routine-generation").run(
        date(2025, 1, 13)
    )

    new_requests = client.history_requests[before_requests:]
    assert ("600000", "2025-01-07", "2025-01-13") in new_requests
    assert ("000001", "2025-01-07", "2025-01-13") in new_requests
    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])
    revised = frame.loc[frame["date"] == "2025-01-08"].iloc[0]
    assert revised["down_count"] == 2
    assert revised["daily_diff"] == -2


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


def test_same_trading_day_second_run_skips_collection_and_manifest_switch(tmp_path):
    client = FixtureClient()
    pipeline = make_pipeline(tmp_path, client)
    pipeline.run(date(2025, 1, 6))
    manifest_path = tmp_path / "public/data/current.json"
    before_manifest = manifest_path.read_bytes()
    before_requests = list(client.history_requests)
    before_versions = sorted((tmp_path / "public/data/versions").iterdir())

    result = pipeline.run(date(2025, 1, 6))

    assert result.skipped is True
    assert client.history_requests == before_requests
    assert manifest_path.read_bytes() == before_manifest
    assert sorted((tmp_path / "public/data/versions").iterdir()) == before_versions


def test_holiday_without_a_new_index_trading_day_is_a_no_op(tmp_path):
    client = FixtureClient()
    pipeline = make_pipeline(tmp_path, client)
    pipeline.run(date(2025, 1, 10), full=True)
    manifest_path = tmp_path / "public/data/current.json"
    before_manifest = manifest_path.read_bytes()
    before_requests = list(client.history_requests)

    result = pipeline.run(date(2025, 1, 11))

    assert result.skipped is True
    assert client.history_requests == before_requests
    assert manifest_path.read_bytes() == before_manifest


def test_identical_full_recalculation_does_not_create_a_new_version(tmp_path):
    pipeline = make_pipeline(tmp_path)
    pipeline.run(date(2025, 1, 6), full=True)
    manifest_path = tmp_path / "public/data/current.json"
    before_manifest = manifest_path.read_bytes()
    before_versions = sorted((tmp_path / "public/data/versions").iterdir())

    result = pipeline.run(date(2025, 1, 6), full=True)

    assert result.skipped is True
    assert manifest_path.read_bytes() == before_manifest
    assert sorted((tmp_path / "public/data/versions").iterdir()) == before_versions


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
        make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(
            date(2025, 1, 6), full=True
        )

    assert manifest_path.read_bytes() == before


def test_manifest_switch_failure_rolls_back_canonical_csv(tmp_path, monkeypatch):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    manifest_path = tmp_path / "public/data/current.json"
    canonical_path = tmp_path / "data/market_history.csv"
    before_manifest = manifest_path.read_bytes()
    before_canonical = canonical_path.read_bytes()
    original_replace = os.replace

    def fail_manifest_replace(source, destination):
        if Path(destination) == manifest_path:
            raise OSError("simulated manifest switch failure")
        return original_replace(source, destination)

    monkeypatch.setattr("scripts.market_data.pipeline.os.replace", fail_manifest_replace)

    with pytest.raises(OSError, match="simulated manifest switch failure"):
        make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(
            date(2025, 1, 7), full=True
        )

    assert manifest_path.read_bytes() == before_manifest
    assert canonical_path.read_bytes() == before_canonical


def test_post_manifest_fsync_failure_keeps_canonical_aligned_with_active_version(
    tmp_path, monkeypatch
):
    make_pipeline(tmp_path).run(date(2025, 1, 6))
    manifest_path = tmp_path / "public/data/current.json"
    canonical_path = tmp_path / "data/market_history.csv"
    before_manifest = manifest_path.read_bytes()
    original_fsync_directory = MarketDataPipeline._fsync_directory
    manifest_switched = False

    def fail_after_manifest_switch(path):
        nonlocal manifest_switched
        if Path(path) == tmp_path / "public/data":
            manifest_switched = True
            raise OSError("simulated public data fsync failure")
        return original_fsync_directory(path)

    monkeypatch.setattr(
        MarketDataPipeline, "_fsync_directory", staticmethod(fail_after_manifest_switch)
    )

    with pytest.raises(OSError, match="simulated public data fsync failure"):
        make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(
            date(2025, 1, 7), full=True
        )

    assert manifest_switched is True
    assert manifest_path.read_bytes() != before_manifest
    manifest = json.loads(manifest_path.read_text())
    active_csv = tmp_path / "public/data" / manifest["history_csv"]
    assert canonical_path.read_bytes() == active_csv.read_bytes()


def test_new_pipeline_reconciles_canonical_from_active_manifest(tmp_path):
    pipeline = make_pipeline(tmp_path)
    pipeline.run(date(2025, 1, 6))
    canonical_path = tmp_path / "data/market_history.csv"
    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    active_csv = tmp_path / "public/data" / manifest["history_csv"]
    canonical_path.write_text("interrupted canonical bytes\n", encoding="utf-8")

    make_pipeline(tmp_path)._read_history()

    assert canonical_path.read_bytes() == active_csv.read_bytes()


@pytest.mark.parametrize("visible_manifest", ["old", "new"])
def test_restart_reconciles_to_whichever_manifest_is_visible(
    tmp_path, visible_manifest
):
    first = make_pipeline(tmp_path)
    first.run(date(2025, 1, 6))
    manifest_path = tmp_path / "public/data/current.json"
    canonical_path = tmp_path / "data/market_history.csv"
    old_manifest_bytes = manifest_path.read_bytes()
    old_manifest = json.loads(old_manifest_bytes)
    old_csv = (tmp_path / "public/data" / old_manifest["history_csv"]).read_bytes()

    make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(
        date(2025, 1, 7), full=True
    )
    new_manifest_bytes = manifest_path.read_bytes()
    new_manifest = json.loads(new_manifest_bytes)
    new_csv = (tmp_path / "public/data" / new_manifest["history_csv"]).read_bytes()

    if visible_manifest == "old":
        manifest_path.write_bytes(old_manifest_bytes)
        canonical_path.write_bytes(new_csv)
        expected_csv = old_csv
    else:
        canonical_path.write_bytes(old_csv)
        expected_csv = new_csv

    class StopsAfterStartup(FixtureClient):
        def get_index_history(self, start, end):
            raise RuntimeError("stop after startup")

    with pytest.raises(RuntimeError, match="stop after startup"):
        make_pipeline(tmp_path, StopsAfterStartup()).run(date(2025, 1, 7))

    assert canonical_path.read_bytes() == expected_csv


def test_routine_early_no_op_reconciles_canonical_before_return(tmp_path):
    client = FixtureClient()
    pipeline = make_pipeline(tmp_path, client)
    pipeline.run(date(2025, 1, 6))
    canonical_path = tmp_path / "data/market_history.csv"
    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    active_csv = tmp_path / "public/data" / manifest["history_csv"]
    canonical_path.write_text("interrupted canonical bytes\n", encoding="utf-8")
    before_requests = list(client.history_requests)

    result = make_pipeline(tmp_path, client).run(date(2025, 1, 6))

    assert result.skipped is True
    assert client.history_requests == before_requests
    assert canonical_path.read_bytes() == active_csv.read_bytes()


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
    first_json = tmp_path / "public/data" / first_manifest["history_json"]
    first_csv = tmp_path / "public/data" / first_manifest["history_csv"]

    make_pipeline(tmp_path, FixtureClient(close_delta=5.0)).run(
        date(2025, 1, 6), full=True
    )

    second_manifest = json.loads(manifest_path.read_text())
    assert second_manifest["version"] != first_manifest["version"]
    assert first_json.exists() and first_csv.exists()
    assert (tmp_path / "public/data" / second_manifest["history_json"]).exists()
    assert (tmp_path / "public/data" / second_manifest["history_csv"]).exists()


def test_full_history_keeps_delisted_stock_until_its_delisting_date(tmp_path):
    make_pipeline(tmp_path, FixtureClient(include_delisted=True)).run(date(2025, 1, 8), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])

    assert frame.loc[frame["date"] == "2025-01-06", "valid_count"].item() == 3
    assert frame.loc[frame["date"] == "2025-01-08", "valid_count"].item() == 2


def test_full_history_does_not_request_stock_delisted_before_history_window(tmp_path):
    client = FixtureClient(include_old_delisted=True)

    make_pipeline(tmp_path, client).run(date(2025, 1, 8), full=True)

    requested_symbols = [symbol for symbol, _, _ in client.history_requests]
    assert "000003" not in requested_symbols


def test_resumed_stock_uses_its_previous_valid_close_after_suspension(tmp_path):
    make_pipeline(
        tmp_path, FixtureClient(suspended_on="2025-01-06", suspended_close=None)
    ).run(date(2025, 1, 7), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])

    assert frame.loc[frame["date"] == "2025-01-07", "up_count"].item() == 1
    assert frame.loc[frame["date"] == "2025-01-07", "valid_count"].item() == 2


def test_cross_year_long_suspension_uses_last_pre_window_valid_close(tmp_path):
    client = CrossYearSuspensionClient()

    make_pipeline(tmp_path, client).run(date(2025, 1, 6), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])
    resumed = frame.loc[frame["date"] == "2025-01-06"].iloc[0]
    assert resumed["up_count"] == 1
    assert resumed["valid_count"] == 2
    assert ("600000", "1990-12-01", "2025-01-06") in client.history_requests


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
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])

    assert frame.loc[frame["date"] == "2025-01-07", "up_count"].item() == 2
    assert frame.loc[frame["date"] == "2025-01-07", "down_count"].item() == 0


def test_production_history_shape_marks_zero_turnover_stale_close_as_suspended(tmp_path):
    make_pipeline(tmp_path, ProductionShapeFixtureClient()).run(date(2025, 1, 7), full=True)

    manifest = json.loads((tmp_path / "public/data/current.json").read_text())
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])

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
    frame = pd.read_csv(tmp_path / "public/data" / manifest["history_csv"])

    assert frame.loc[0, "shanghai_change"] == 10.0
    assert frame.loc[0, "shanghai_change_pct"] == pytest.approx(0.3134796238244514)


def test_rejects_abnormal_valid_stock_count_drop_before_manifest_switch(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 6), full=True)
    before = (tmp_path / "public/data/current.json").read_bytes()

    with pytest.raises(MarketDataValidationError, match="valid stock count dropped"):
        make_pipeline(tmp_path, FixtureClient(invalid_symbols={"600000"}, invalid_on={"2025-01-07"})).run(date(2025, 1, 7))

    assert (tmp_path / "public/data/current.json").read_bytes() == before


def test_rejects_universe_drop_even_when_one_remaining_security_is_suspended(tmp_path):
    pipeline = make_pipeline(tmp_path)
    prices = pd.DataFrame(
        {
            "current_close": [10.0] * 59,
            "previous_close": [9.0] * 59,
            "is_suspended": [False] * 58 + [True],
        }
    )
    universe = pd.DataFrame({"symbol": [f"{value:06d}" for value in range(59)]})
    existing = pd.DataFrame(
        {
            "date": INDEX_DATES[:5],
            "valid_count": [100] * 5,
        }
    )

    with pytest.raises(MarketDataValidationError, match="valid stock count dropped"):
        pipeline._validate_daily_quality("2025-01-09", prices, universe, existing)


def test_rejects_exchange_with_only_positive_stale_suspended_prices(tmp_path):
    with pytest.raises(MarketDataValidationError, match="missing valid exchange breadth"):
        make_pipeline(tmp_path, SuspendedExchangeClient()).run(date(2025, 1, 2))


def test_rejects_excessive_missing_price_or_comparison_ratio_before_publish(tmp_path):
    with pytest.raises(MarketDataValidationError, match="missing price/comparison ratio"):
        make_pipeline(tmp_path, FixtureClient(invalid_symbols={"600000"})).run(date(2025, 1, 6))

    assert not (tmp_path / "public/data/current.json").exists()


def test_failed_first_symbol_does_not_discard_later_success_and_pointer_is_unchanged(
    tmp_path,
):
    make_pipeline(tmp_path).run(date(2025, 1, 3))
    pointer = (tmp_path / "public/data/current.json").read_bytes()
    csv = (tmp_path / "data/market_history.csv").read_bytes()

    class FailingFirst(FixtureClient):
        def get_stock_history(self, symbol, start, end):
            if symbol == "600000":
                raise RuntimeError("both sources failed")
            return super().get_stock_history(symbol, start, end)

    client = FailingFirst()
    with pytest.raises(RuntimeError, match="publication blocked"):
        make_pipeline(tmp_path, client, checkpoint_generation="failed").run(
            date(2025, 1, 6), full=True
        )
    assert client.history_requests == [("000001", "1990-12-01", "2025-01-06")]
    run = (
        tmp_path
        / "work/market-data-checkpoints/v4-akshare-unadjusted/1990-12-01/runs/failed"
    )
    assert (run / "symbols/000001.csv").exists()
    assert not (run / "success.json").exists()
    failure = json.loads((run / "failures.json").read_text())[0]
    assert failure["symbol"] == "600000"
    assert failure["end"] == "2025-01-06"
    assert (tmp_path / "public/data/current.json").read_bytes() == pointer
    assert (tmp_path / "data/market_history.csv").read_bytes() == csv


def test_matching_failed_run_recovers_across_different_github_run_ids(tmp_path):
    client = FailsOnceDuringStockHistoryClient()
    with pytest.raises(RuntimeError):
        make_pipeline(tmp_path, client, checkpoint_generation="run-a").run(
            date(2025, 1, 6), full=True
        )
    assert not (tmp_path / "public/data/current.json").exists()
    make_pipeline(tmp_path, client, checkpoint_generation="run-b").run(
        date(2025, 1, 6), full=True
    )
    assert [symbol for symbol, _, _ in client.history_requests].count("600000") == 1
    assert [symbol for symbol, _, _ in client.history_requests].count("000001") == 2
    assert (tmp_path / "public/data/current.json").exists()


@pytest.mark.parametrize(
    "changed",
    [
        "as_of",
        "mode",
        "source_version",
        "coverage",
        "expired",
        "future",
        "corrupt_csv",
        "short_coverage",
        "empty_csv",
    ],
)
def test_incompatible_or_unverified_failed_checkpoints_are_refetched(tmp_path, changed):
    client = FailsOnceDuringStockHistoryClient()
    now = datetime(2025, 1, 6, 10, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError):
        make_pipeline(
            tmp_path, client, now=lambda: now, checkpoint_generation="run-a"
        ).run(date(2025, 1, 6), full=True)
    run = (
        tmp_path
        / "work/market-data-checkpoints/v4-akshare-unadjusted/1990-12-01/runs/run-a"
    )
    metadata = json.loads((run / "run.json").read_text())
    if changed in {"as_of", "mode", "source_version"}:
        metadata[changed] = "incompatible"
    elif changed == "coverage":
        metadata["coverage"]["universe_sha256"] = "changed"
    elif changed in {"expired", "future"}:
        metadata["created_at"] = (
            now + timedelta(hours=-13 if changed == "expired" else 1)
        ).isoformat()
    elif changed == "corrupt_csv":
        with (run / "symbols/600000.csv").open("a") as handle:
            handle.write("corrupt")
    elif changed == "empty_csv":
        (run / "symbols/600000.csv").write_text(
            "date,symbol,current_close,is_suspended\n"
        )
    elif changed == "short_coverage":
        symbol_metadata = json.loads((run / "symbols/600000.json").read_text())
        symbol_metadata["end"] = "2025-01-03"
        (run / "symbols/600000.json").write_text(json.dumps(symbol_metadata))
    (run / "run.json").write_text(json.dumps(metadata))
    make_pipeline(tmp_path, client, now=lambda: now, checkpoint_generation="run-b").run(
        date(2025, 1, 6), full=True
    )
    assert [symbol for symbol, _, _ in client.history_requests].count("600000") == 2


def test_universe_failure_preserves_pointer_and_prevents_stock_fetch(tmp_path):
    make_pipeline(tmp_path).run(date(2025, 1, 3))
    pointer = (tmp_path / "public/data/current.json").read_bytes()

    class UnavailableUniverse(FixtureClient):
        def get_security_universe(self):
            raise RuntimeError("all list sources unavailable")

    client = UnavailableUniverse()
    with pytest.raises(RuntimeError, match="list sources"):
        make_pipeline(tmp_path, client).run(date(2025, 1, 6))
    assert client.history_requests == []
    assert (tmp_path / "public/data/current.json").read_bytes() == pointer


def test_expired_retry_of_same_generation_refetches_completed_prices(tmp_path):
    client = FailsOnceDuringStockHistoryClient()
    now = datetime(2025, 1, 6, 10, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError):
        make_pipeline(
            tmp_path, client, now=lambda: now, checkpoint_generation="same-run"
        ).run(date(2025, 1, 6), full=True)
    make_pipeline(
        tmp_path,
        client,
        now=lambda: now + timedelta(hours=13),
        checkpoint_generation="same-run",
    ).run(date(2025, 1, 6), full=True)
    assert [symbol for symbol, _, _ in client.history_requests].count("600000") == 2
