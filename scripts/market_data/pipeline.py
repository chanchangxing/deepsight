"""Validated, idempotent publishing of A-share market-breadth history."""

from __future__ import annotations

import io
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .akshare_client import AkshareClient
from .calculations import add_cumulative_diff, calculate_index_changes, classify_breadth


HISTORY_START = date(2025, 1, 1)
INDEX_LOOKBACK_DAYS = 31
RECENT_VALID_COUNT_DAYS = 5
MIN_VALID_COUNT_RATIO = 0.60
MAX_MISSING_PRICE_OR_COMPARISON_RATIO = 0.20
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
        self.public_data_path = self.root / "public" / "data"
        self.manifest_path = self.public_data_path / "current.json"

    def run(self, as_of: date, full: bool = False) -> UpdateResult:
        if as_of < HISTORY_START:
            raise ValueError("as_of must be on or after 2025-01-01")

        existing = self._read_history()
        validate_history(existing)
        index_history = self._index_history(as_of)
        index_dates = index_history.loc[
            index_history["date"] >= HISTORY_START.isoformat(), "date"
        ].tolist()
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

        replacements = self._collect_records(index_history, dates_to_update, as_of, existing)
        retained = existing.loc[~existing["date"].isin(dates_to_update)]
        combined = replacements if retained.empty else pd.concat([retained, replacements], ignore_index=True)
        combined = combined.loc[:, REQUIRED_COLUMNS].sort_values("date").reset_index(drop=True)
        combined = add_cumulative_diff(combined).loc[:, REQUIRED_COLUMNS]
        validate_history(combined)
        self._atomic_publish(combined)
        return UpdateResult(tuple(dates_to_update))

    def _read_history(self) -> pd.DataFrame:
        source = self.csv_path
        if self.manifest_path.exists():
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                source = self.public_data_path / manifest["csv"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise MarketDataValidationError(f"cannot read current manifest: {error}") from error
        if not source.exists():
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        try:
            return pd.read_csv(source, dtype={"date": str})
        except (OSError, pd.errors.ParserError, ValueError) as error:
            raise MarketDataValidationError(f"cannot read existing history: {error}") from error

    def _index_history(self, as_of: date) -> pd.DataFrame:
        start = HISTORY_START - timedelta(days=INDEX_LOOKBACK_DAYS)
        frame = self.client.get_index_history(start.isoformat(), as_of.isoformat()).copy()
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
            (frame["date"] <= as_of.isoformat()),
            ["date", "close"],
        ].sort_values("date").reset_index(drop=True)
        if frame["date"].duplicated().any():
            raise MarketDataValidationError("duplicate index dates")
        if frame["close"].isna().any() or frame["close"].le(0).any():
            raise MarketDataValidationError("invalid index close")
        return frame

    def _collect_records(
        self,
        index_history: pd.DataFrame,
        dates_to_update: list[str],
        as_of: date,
        existing: pd.DataFrame,
    ) -> pd.DataFrame:
        market = self.client.get_security_universe().copy()
        required_market_columns = {"symbol", "exchange", "listing_date", "delisting_date"}
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
            if "is_suspended" not in history:
                history["is_suspended"] = False
            histories.append(history[["date", "symbol", "current_close", "is_suspended"]])
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
        first_valid_dates = (
            prices.loc[prices["current_close"].gt(0)]
            .groupby("symbol")["date"]
            .min()
        )
        market["listing_date"] = market["listing_date"].fillna(
            market["symbol"].map(first_valid_dates)
        )
        prices["previous_close"] = prices.groupby("symbol", sort=False)["current_close"].transform(
            lambda closes: closes.where(closes.gt(0)).ffill().shift()
        )

        index_changes = calculate_index_changes(index_history).set_index("date")
        records = []
        for value in dates_to_update:
            active_market = market.loc[
                (market["listing_date"].isna() | (market["listing_date"] <= value))
                & (market["delisting_date"].isna() | (market["delisting_date"] >= value))
            ]
            daily_prices = prices.loc[prices["date"] == value].merge(
                active_market[["symbol", "exchange"]], on="symbol", how="inner"
            )
            pending_records = pd.DataFrame(records)
            quality_history = (
                existing
                if pending_records.empty
                else pd.concat([existing, pending_records], ignore_index=True)
            )
            self._validate_daily_quality(value, daily_prices, active_market, quality_history)
            counts = classify_breadth(daily_prices)
            if counts.valid == 0:
                raise MarketDataValidationError(f"no valid stock prices for {value}")
            active_by_exchange = active_market.groupby("exchange").size()
            valid_by_exchange = daily_prices.loc[
                daily_prices["current_close"].gt(0) & daily_prices["previous_close"].gt(0)
            ].groupby("exchange").size()
            required_exchanges = {
                exchange
                for exchange, size in active_by_exchange.items()
                if size > 1
            }
            if not required_exchanges.issubset(valid_by_exchange.index):
                raise MarketDataValidationError(f"missing valid exchange breadth for {value}")
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

    def _validate_daily_quality(
        self,
        value: str,
        prices: pd.DataFrame,
        universe: pd.DataFrame,
        existing: pd.DataFrame,
    ) -> None:
        if universe.empty:
            raise MarketDataValidationError(f"empty historical security universe for {value}")
        expected = len(universe)
        available = prices["current_close"].gt(0)
        comparable = available & prices["previous_close"].gt(0)
        suspended = prices.get("is_suspended", pd.Series(False, index=prices.index)).fillna(False)
        quality_expected = int((~suspended).sum())
        missing_ratio = 1 - (int(comparable.sum()) / quality_expected) if quality_expected else 0
        prior = existing.loc[existing["date"] < value, "valid_count"].tail(
            RECENT_VALID_COUNT_DAYS
        )
        if (
            not prior.empty
            and not suspended.any()
            and int(comparable.sum()) < prior.median() * MIN_VALID_COUNT_RATIO
        ):
            raise MarketDataValidationError(f"valid stock count dropped for {value}")
        if missing_ratio > MAX_MISSING_PRICE_OR_COMPARISON_RATIO:
            raise MarketDataValidationError(
                f"missing price/comparison ratio {missing_ratio:.1%} exceeds "
                f"{MAX_MISSING_PRICE_OR_COMPARISON_RATIO:.1%} for {value}"
            )

    def _atomic_publish(self, frame: pd.DataFrame) -> None:
        csv_bytes = frame.to_csv(index=False).encode("utf-8")
        csv_frame = pd.read_csv(io.BytesIO(csv_bytes), dtype={"date": str})
        json_bytes = json.dumps(
            csv_frame.to_dict(orient="records"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        version = uuid.uuid4().hex
        version_path = self.public_data_path / "versions" / version
        version_path.mkdir(parents=True, exist_ok=False)
        csv_path = version_path / "market_history.csv"
        json_path = version_path / "market_history.json"
        csv_temp = self._write_temp(csv_path, csv_bytes)
        json_temp = self._write_temp(json_path, json_bytes)
        try:
            os.replace(csv_temp, csv_path)
            os.replace(json_temp, json_path)
            self._fsync_directory(version_path)
            manifest = {
                "version": version,
                "csv": f"versions/{version}/market_history.csv",
                "json": f"versions/{version}/market_history.json",
            }
            manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
            manifest_temp = self._write_temp(self.manifest_path, manifest_bytes)
            try:
                os.replace(manifest_temp, self.manifest_path)
                self._fsync_directory(self.public_data_path)
            finally:
                manifest_temp.unlink(missing_ok=True)
        finally:
            csv_temp.unlink(missing_ok=True)
            json_temp.unlink(missing_ok=True)

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
