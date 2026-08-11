"""Validated, idempotent publishing of A-share market-breadth history."""

from __future__ import annotations

import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .akshare_client import AkshareClient
from .calculations import add_cumulative_diff, calculate_index_changes, classify_breadth


HISTORY_START = date(2025, 1, 1)
REQUIRED_COLUMNS = [
    "date",
    "shanghai_close",
    "shanghai_change",
    "shanghai_change_pct",
    "up_count",
    "down_count",
    "flat_count",
    "valid_count",
    "daily_diff",
    "cumulative_diff",
]


class MarketDataValidationError(ValueError):
    """Raised when data cannot safely replace the current published history."""


@dataclass(frozen=True)
class UpdateResult:
    updated_dates: tuple[str, ...]
    skipped: bool = False


def validate_history(frame: pd.DataFrame) -> None:
    """Check the invariants shared by the reviewable CSV and public JSON."""
    missing = set(REQUIRED_COLUMNS).difference(frame.columns)
    if missing:
        raise MarketDataValidationError(f"missing required columns: {sorted(missing)}")
    if frame.empty:
        return

    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    try:
        normalized_dates = pd.to_datetime(result["date"], errors="raise").dt.strftime(
            "%Y-%m-%d"
        )
        numeric_columns = [column for column in REQUIRED_COLUMNS if column != "date"]
        for column in numeric_columns:
            result[column] = pd.to_numeric(result[column], errors="raise")
    except (TypeError, ValueError) as error:
        raise MarketDataValidationError(str(error)) from error

    if normalized_dates.duplicated().any():
        raise MarketDataValidationError("duplicate dates")
    if not normalized_dates.is_monotonic_increasing:
        raise MarketDataValidationError("dates are not strictly increasing")
    if result.drop(columns="date").isna().any().any():
        raise MarketDataValidationError("missing required values")
    if (result[["up_count", "down_count", "flat_count", "valid_count"]] < 0).any().any():
        raise MarketDataValidationError("negative breadth count")
    if not (
        result["up_count"] + result["down_count"] + result["flat_count"]
        == result["valid_count"]
    ).all():
        raise MarketDataValidationError("breadth count mismatch")
    if not (result["daily_diff"] == result["up_count"] - result["down_count"]).all():
        raise MarketDataValidationError("daily diff mismatch")
    if not (result["cumulative_diff"] == result["daily_diff"].cumsum()).all():
        raise MarketDataValidationError("cumulative diff mismatch")


class MarketDataPipeline:
    """Build and publish market history only after the complete result validates."""

    def __init__(self, root: Path | str | None = None, client: Any | None = None) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self.client = client if client is not None else AkshareClient()
        self.csv_path = self.root / "data" / "market_history.csv"
        self.json_path = self.root / "public" / "data" / "market_history.json"

    def run(self, as_of: date, full: bool = False) -> UpdateResult:
        if as_of < HISTORY_START:
            raise ValueError("as_of must be on or after 2025-01-01")

        existing = self._read_history()
        validate_history(existing)
        index_history = self._index_history(as_of)
        index_dates = index_history["date"].tolist()
        non_trading_dates = set(existing["date"]).difference(index_dates)
        if non_trading_dates:
            raise MarketDataValidationError(
                f"existing history contains non-trading dates: {sorted(non_trading_dates)}"
            )
        if not index_dates:
            return UpdateResult((), skipped=True)

        existing_dates = set(existing["date"])
        missing_dates = [value for value in index_dates if value not in existing_dates]
        recent_dates = index_dates if full else index_dates[-5:]
        dates_to_update = [
            value for value in index_dates if value in set(missing_dates).union(recent_dates)
        ]
        if not dates_to_update:
            return UpdateResult((), skipped=True)

        replacements = self._collect_records(index_history, dates_to_update, as_of)
        retained = existing.loc[~existing["date"].isin(dates_to_update)]
        combined = replacements if retained.empty else pd.concat([retained, replacements], ignore_index=True)
        combined = combined.loc[:, REQUIRED_COLUMNS].sort_values("date").reset_index(drop=True)
        combined = add_cumulative_diff(combined).loc[:, REQUIRED_COLUMNS]
        validate_history(combined)
        self._atomic_publish(combined)
        return UpdateResult(tuple(dates_to_update))

    def _read_history(self) -> pd.DataFrame:
        if not self.csv_path.exists():
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        try:
            return pd.read_csv(self.csv_path, dtype={"date": str})
        except (OSError, pd.errors.ParserError, ValueError) as error:
            raise MarketDataValidationError(f"cannot read existing history: {error}") from error

    def _index_history(self, as_of: date) -> pd.DataFrame:
        frame = self.client.get_index_history(HISTORY_START.isoformat(), as_of.isoformat()).copy()
        if not {"date", "close"}.issubset(frame.columns):
            raise MarketDataValidationError("index history is missing date or close")
        try:
            frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime(
                "%Y-%m-%d"
            )
            frame["close"] = pd.to_numeric(frame["close"], errors="raise")
        except (TypeError, ValueError) as error:
            raise MarketDataValidationError(f"invalid index history: {error}") from error
        frame = frame.loc[
            (frame["date"] >= HISTORY_START.isoformat())
            & (frame["date"] <= as_of.isoformat()),
            ["date", "close"],
        ].sort_values("date").reset_index(drop=True)
        if frame["date"].duplicated().any():
            raise MarketDataValidationError("duplicate index dates")
        if frame["close"].isna().any() or frame["close"].le(0).any():
            raise MarketDataValidationError("invalid index close")
        return frame

    def _collect_records(
        self, index_history: pd.DataFrame, dates_to_update: list[str], as_of: date
    ) -> pd.DataFrame:
        market = self.client.get_current_market().copy()
        required_market_columns = {"symbol", "exchange"}
        if not required_market_columns.issubset(market.columns):
            raise MarketDataValidationError("market snapshot is missing symbol or exchange")
        market["symbol"] = market["symbol"].astype(str).str.zfill(6)
        if market["symbol"].duplicated().any():
            raise MarketDataValidationError("duplicate symbols")
        exchanges = set(market["exchange"])
        if not {"SH", "SZ"}.issubset(exchanges):
            raise MarketDataValidationError("both Shanghai and Shenzhen markets are required")

        history_start = (HISTORY_START - timedelta(days=1)).isoformat()
        histories = []
        for symbol in market["symbol"]:
            history = self.client.get_stock_history(symbol, history_start, as_of.isoformat()).copy()
            if not {"date", "current_close"}.issubset(history.columns):
                raise MarketDataValidationError(f"stock history is incomplete for {symbol}")
            history["symbol"] = symbol
            histories.append(history[["date", "symbol", "current_close"]])
        if not histories:
            raise MarketDataValidationError("empty market snapshot")

        prices = pd.concat(histories, ignore_index=True)
        try:
            prices["date"] = pd.to_datetime(prices["date"], errors="raise").dt.strftime(
                "%Y-%m-%d"
            )
            prices["current_close"] = pd.to_numeric(prices["current_close"], errors="raise")
        except (TypeError, ValueError) as error:
            raise MarketDataValidationError(f"invalid stock history: {error}") from error
        prices = prices.loc[prices["date"] <= as_of.isoformat()].sort_values(
            ["symbol", "date"]
        )
        if prices.duplicated(["symbol", "date"]).any():
            raise MarketDataValidationError("duplicate stock history dates")
        prices["previous_close"] = prices.groupby("symbol", sort=False)["current_close"].shift()

        index_changes = calculate_index_changes(index_history).set_index("date")
        records = []
        for value in dates_to_update:
            daily_prices = prices.loc[prices["date"] == value]
            counts = classify_breadth(daily_prices)
            if counts.valid == 0:
                raise MarketDataValidationError(f"no valid stock prices for {value}")
            index = index_changes.loc[value]
            records.append(
                {
                    "date": value,
                    "shanghai_close": float(index["shanghai_close"]),
                    "shanghai_change": float(index["shanghai_change"]),
                    "shanghai_change_pct": float(index["shanghai_change_pct"]),
                    "up_count": counts.up,
                    "down_count": counts.down,
                    "flat_count": counts.flat,
                    "valid_count": counts.valid,
                    "daily_diff": counts.up - counts.down,
                    "cumulative_diff": 0,
                }
            )
        return pd.DataFrame(records, columns=REQUIRED_COLUMNS)

    def _atomic_publish(self, frame: pd.DataFrame) -> None:
        csv_bytes = frame.to_csv(index=False).encode("utf-8")
        csv_frame = pd.read_csv(io.BytesIO(csv_bytes), dtype={"date": str})
        json_bytes = json.dumps(
            csv_frame.to_dict(orient="records"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        previous_csv = self.csv_path.read_bytes() if self.csv_path.exists() else None
        previous_json = self.json_path.read_bytes() if self.json_path.exists() else None
        csv_temp = self._write_temp(self.csv_path, csv_bytes)
        try:
            json_temp = self._write_temp(self.json_path, json_bytes)
        except Exception:
            csv_temp.unlink(missing_ok=True)
            raise

        csv_replaced = False
        json_replaced = False
        try:
            os.replace(csv_temp, self.csv_path)
            csv_replaced = True
            os.replace(json_temp, self.json_path)
            json_replaced = True
            self._fsync_directory(self.csv_path.parent)
            self._fsync_directory(self.json_path.parent)
        except Exception:
            if json_replaced:
                self._restore_published_file(self.json_path, previous_json)
            if csv_replaced:
                self._restore_published_file(self.csv_path, previous_csv)
            raise
        finally:
            csv_temp.unlink(missing_ok=True)
            json_temp.unlink(missing_ok=True)

    def _restore_published_file(self, path: Path, previous: bytes | None) -> None:
        if previous is None:
            path.unlink(missing_ok=True)
            return
        restore_temp = self._write_temp(path, previous)
        try:
            os.replace(restore_temp, path)
        finally:
            restore_temp.unlink(missing_ok=True)

    @staticmethod
    def _write_temp(path: Path, payload: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return Path(temp_name)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
