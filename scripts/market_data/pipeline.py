"""Validated, idempotent publishing of A-share market-breadth history."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .akshare_client import AkshareClient
from .calculations import add_cumulative_diff, calculate_index_changes, classify_breadth


HISTORY_START = date(2025, 1, 1)
INDEX_LOOKBACK_DAYS = 31
STOCK_HISTORY_START = date(1990, 12, 1)
CHECKPOINT_NAMESPACE = "v3-akshare-unadjusted"
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

    def __init__(
        self,
        root: Path | str | None = None,
        client: Any | None = None,
        now: Callable[[], datetime] | None = None,
        checkpoint_generation: str | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self.client = client if client is not None else AkshareClient()
        self.now = now if now is not None else lambda: datetime.now(timezone.utc)
        self.checkpoint_generation = (
            checkpoint_generation
            or os.environ.get("MARKET_DATA_CHECKPOINT_GENERATION")
        )
        if self.checkpoint_generation is not None and not re.fullmatch(
            r"[A-Za-z0-9._-]{1,128}", self.checkpoint_generation
        ):
            raise ValueError("checkpoint generation contains unsafe characters")
        self.csv_path = self.root / "data" / "market_history.csv"
        self.public_data_path = self.root / "public" / "data"
        self.manifest_path = self.public_data_path / "current.json"

    def run(self, as_of: date, full: bool = False) -> UpdateResult:
        if as_of < HISTORY_START:
            raise ValueError("as_of must be on or after 2025-01-01")
        checkpoint_generation = self.checkpoint_generation or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", checkpoint_generation):
            raise ValueError("checkpoint generation contains unsafe characters")
        self._active_checkpoint_generation = checkpoint_generation

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
        latest_index_date = index_dates[-1]
        as_of_is_trading_day = latest_index_date == as_of.isoformat()
        if (
            not full
            and not missing_dates
            and not existing.empty
            and existing["date"].iloc[-1] == latest_index_date
            and (as_of_is_trading_day or as_of > date.fromisoformat(latest_index_date))
        ):
            return UpdateResult((), skipped=True)
        recent_dates = index_dates if full else index_dates[-5:]
        dates_to_update = [
            value for value in index_dates if value in set(missing_dates).union(recent_dates)
        ]
        if not dates_to_update:
            return UpdateResult((), skipped=True)

        refresh_start = (
            HISTORY_START.isoformat() if full else min(dates_to_update)
        )
        self._prepare_checkpoint_generation(
            full=full, as_of=as_of.isoformat(), refresh_start=refresh_start
        )
        replacements = self._collect_records(
            index_history,
            dates_to_update,
            as_of,
            existing,
            full=full,
            refresh_start=refresh_start,
        )
        retained = existing.loc[~existing["date"].isin(dates_to_update)]
        combined = replacements if retained.empty else pd.concat([retained, replacements], ignore_index=True)
        combined = combined.loc[:, REQUIRED_COLUMNS].sort_values("date").reset_index(drop=True)
        combined = add_cumulative_diff(combined).loc[:, REQUIRED_COLUMNS]
        validate_history(combined)
        if self._history_csv_bytes(combined) == self._history_csv_bytes(existing):
            self._mark_checkpoint_generation_successful()
            return UpdateResult((), skipped=True)
        self._atomic_publish(combined)
        self._mark_checkpoint_generation_successful()
        return UpdateResult(tuple(dates_to_update))

    def _read_history(self) -> pd.DataFrame:
        source = self.csv_path
        if self.manifest_path.exists():
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                history_csv = manifest.get("history_csv", manifest.get("csv"))
                if not isinstance(history_csv, str):
                    raise KeyError("history_csv")
                source = self.public_data_path / history_csv
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise MarketDataValidationError(f"cannot read current manifest: {error}") from error
            self._reconcile_canonical(source)
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
        *,
        full: bool,
        refresh_start: str,
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

        market = market.loc[
            market["delisting_date"].isna()
            | (market["delisting_date"] >= HISTORY_START.isoformat())
        ].reset_index(drop=True)

        history_start = STOCK_HISTORY_START.isoformat()
        histories = []
        for symbol in market["symbol"]:
            history = self._stock_history(
                symbol,
                history_start,
                as_of.isoformat(),
                full=full,
                refresh_start=refresh_start,
            ).copy()
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
        valid_closes = prices["current_close"].where(
            prices["current_close"].gt(0) & ~prices["is_suspended"].fillna(False)
        )
        prices["previous_close"] = valid_closes.groupby(prices["symbol"], sort=False).transform(
            lambda closes: closes.ffill().shift()
        )

        index_changes = calculate_index_changes(index_history).set_index("date")
        records = []
        for value in dates_to_update:
            active_market = market.loc[
                (market["listing_date"].isna() | (market["listing_date"] <= value))
                & (market["delisting_date"].isna() | (market["delisting_date"] >= value))
            ]
            daily_prices = active_market[["symbol", "exchange"]].merge(
                prices.loc[prices["date"] == value, [
                    "symbol", "current_close", "is_suspended", "previous_close"
                ]],
                on="symbol",
                how="left",
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
                self._valid_price_mask(daily_prices)
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

    def _stock_history(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        full: bool = False,
        refresh_start: str | None = None,
    ) -> pd.DataFrame:
        checkpoint_root = self._checkpoint_directory(start, end)
        generation_directory = (
            checkpoint_root / "runs" / self._checkpoint_generation / "symbols"
        )
        generation_checkpoint = generation_directory / f"{symbol}.csv"
        if generation_checkpoint.exists():
            try:
                checkpoint = pd.read_csv(
                    generation_checkpoint,
                    dtype={"date": str, "symbol": str},
                )
                return self._trim_stock_history(self._validated_stock_history(
                    checkpoint, symbol, start, end
                ))
            except (OSError, ValueError, pd.errors.ParserError):
                generation_checkpoint.unlink(missing_ok=True)

        successful = self._read_successful_checkpoint(
            checkpoint_root, symbol, start, end
        )
        if full or successful is None:
            request_start = start
            prefix = None
        else:
            request_start = refresh_start or HISTORY_START.isoformat()
            prefix = successful.loc[successful["date"] < request_start]

        history = self.client.get_stock_history(symbol, request_start, end).copy()
        history["symbol"] = symbol
        try:
            normalized_history_dates = pd.to_datetime(
                history["date"], errors="raise"
            ).dt.strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError) as error:
            raise MarketDataValidationError(
                f"stock history is incomplete for {symbol}"
            ) from error
        history = history.loc[
            normalized_history_dates.between(request_start, end)
        ].copy()
        history["date"] = normalized_history_dates.loc[history.index]
        extension = self._validated_stock_history(history, symbol, request_start, end)
        checkpoint = (
            extension
            if prefix is None
            else pd.concat([prefix, extension], ignore_index=True)
        )
        checkpoint = self._validated_stock_history(checkpoint, symbol, start, end)
        checkpoint = self._trim_stock_history(checkpoint)
        payload = checkpoint.to_csv(index=False).encode("utf-8")
        checkpoint_temp = self._write_temp(generation_checkpoint, payload)
        try:
            os.replace(checkpoint_temp, generation_checkpoint)
            self._fsync_directory(generation_checkpoint.parent)
        finally:
            checkpoint_temp.unlink(missing_ok=True)
        return checkpoint.loc[checkpoint["date"].between(start, end)].reset_index(drop=True)

    def _read_successful_checkpoint(
        self, checkpoint_root: Path, symbol: str, start: str, end: str
    ) -> pd.DataFrame | None:
        pointer_path = checkpoint_root / "latest-success.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            generation = pointer["generation"]
            if not isinstance(generation, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,128}", generation
            ):
                raise ValueError("invalid successful checkpoint generation")
            checkpoint_path = (
                checkpoint_root / "runs" / generation / "symbols" / f"{symbol}.csv"
            )
            checkpoint = pd.read_csv(
                checkpoint_path, dtype={"date": str, "symbol": str}
            )
            return self._trim_stock_history(
                self._validated_stock_history(checkpoint, symbol, start, end)
            )
        except (
            FileNotFoundError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            pd.errors.ParserError,
        ):
            return None

    @staticmethod
    def _trim_stock_history(frame: pd.DataFrame) -> pd.DataFrame:
        window_start = HISTORY_START.isoformat()
        before_window = frame.loc[
            (frame["date"] < window_start)
            & frame["current_close"].gt(0)
            & ~frame["is_suspended"].fillna(False)
        ].tail(1)
        in_window = frame.loc[frame["date"] >= window_start]
        return pd.concat([before_window, in_window], ignore_index=True)

    def _prepare_checkpoint_generation(
        self, *, full: bool, as_of: str, refresh_start: str
    ) -> None:
        checkpoint_root = self._checkpoint_directory(
            STOCK_HISTORY_START.isoformat(), ""
        )
        metadata_path = (
            checkpoint_root / "runs" / self._checkpoint_generation / "run.json"
        )
        metadata = {
            "schema": 1,
            "generation": self._checkpoint_generation,
            "mode": "full" if full else "routine",
            "as_of": as_of,
            "history_start": STOCK_HISTORY_START.isoformat(),
            "refresh_start": refresh_start,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise MarketDataValidationError(
                    f"cannot read checkpoint generation metadata: {error}"
                ) from error
            if existing != metadata:
                raise MarketDataValidationError(
                    "checkpoint generation metadata does not match this run"
                )
            return
        metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        metadata_temp = self._write_temp(metadata_path, metadata_bytes)
        try:
            os.replace(metadata_temp, metadata_path)
            self._fsync_directory(metadata_path.parent)
        finally:
            metadata_temp.unlink(missing_ok=True)

    def _mark_checkpoint_generation_successful(self) -> None:
        checkpoint_root = self._checkpoint_directory(
            STOCK_HISTORY_START.isoformat(), ""
        )
        pointer_path = checkpoint_root / "latest-success.json"
        pointer_bytes = json.dumps(
            {"schema": 1, "generation": self._checkpoint_generation},
            separators=(",", ":"),
        ).encode("utf-8")
        pointer_temp = self._write_temp(pointer_path, pointer_bytes)
        try:
            os.replace(pointer_temp, pointer_path)
            self._fsync_directory(pointer_path.parent)
        finally:
            pointer_temp.unlink(missing_ok=True)
        runs_directory = checkpoint_root / "runs"
        for generation_directory in runs_directory.iterdir():
            if (
                generation_directory.is_dir()
                and generation_directory.name != self._checkpoint_generation
            ):
                shutil.rmtree(generation_directory)

    @property
    def _checkpoint_generation(self) -> str:
        generation = getattr(self, "_active_checkpoint_generation", None)
        if generation is None:
            generation = self.checkpoint_generation or uuid.uuid4().hex
            self._active_checkpoint_generation = generation
        return generation

    @staticmethod
    def _validated_stock_history(
        frame: pd.DataFrame, symbol: str, start: str, end: str
    ) -> pd.DataFrame:
        required = {"date", "symbol", "current_close", "is_suspended"}
        if not required.issubset(frame.columns):
            raise ValueError("checkpoint schema is incomplete")
        result = frame.loc[
            :, ["date", "symbol", "current_close", "is_suspended"]
        ].copy()
        try:
            result["date"] = pd.to_datetime(result["date"], errors="raise").dt.strftime(
                "%Y-%m-%d"
            )
            result["current_close"] = pd.to_numeric(
                result["current_close"], errors="raise"
            )
        except (TypeError, ValueError) as error:
            raise ValueError("checkpoint values are invalid") from error
        if not result["symbol"].astype(str).str.zfill(6).eq(symbol).all():
            raise ValueError("checkpoint symbol does not match")
        suspended_values = result["is_suspended"]
        if not pd.api.types.is_bool_dtype(suspended_values):
            normalized = suspended_values.astype(str).str.lower().map(
                {"true": True, "false": False}
            )
            if normalized.isna().any():
                raise ValueError("checkpoint suspension flag is invalid")
            result["is_suspended"] = normalized.astype(bool)
        if not result["date"].between(start, end).all():
            raise ValueError("checkpoint dates fall outside its coverage")
        if result["date"].duplicated().any():
            raise ValueError("checkpoint contains duplicate dates")
        return result.sort_values("date").reset_index(drop=True)

    def _checkpoint_directory(self, start: str, end: str) -> Path:
        return (
            self.root
            / "work"
            / "market-data-checkpoints"
            / CHECKPOINT_NAMESPACE
            / start
        )

    def _validate_daily_quality(
        self,
        value: str,
        prices: pd.DataFrame,
        universe: pd.DataFrame,
        existing: pd.DataFrame,
    ) -> None:
        if universe.empty:
            raise MarketDataValidationError(f"empty historical security universe for {value}")
        suspended = prices.get("is_suspended", pd.Series(False, index=prices.index)).fillna(False)
        comparable = self._valid_price_mask(prices)
        quality_expected = int((~suspended).sum())
        missing_ratio = 1 - (int(comparable.sum()) / quality_expected) if quality_expected else 0
        prior = existing.loc[existing["date"] < value, "valid_count"].tail(
            RECENT_VALID_COUNT_DAYS
        )
        suspended_count = int(suspended.sum())
        baseline_valid = int(comparable.sum()) + suspended_count
        if (
            not prior.empty
            and baseline_valid < prior.median() * MIN_VALID_COUNT_RATIO
        ):
            raise MarketDataValidationError(f"valid stock count dropped for {value}")
        if missing_ratio > MAX_MISSING_PRICE_OR_COMPARISON_RATIO:
            raise MarketDataValidationError(
                f"missing price/comparison ratio {missing_ratio:.1%} exceeds "
                f"{MAX_MISSING_PRICE_OR_COMPARISON_RATIO:.1%} for {value}"
            )

    @staticmethod
    def _valid_price_mask(prices: pd.DataFrame) -> pd.Series:
        suspended = prices.get(
            "is_suspended", pd.Series(False, index=prices.index)
        ).fillna(False)
        return (
            prices["current_close"].gt(0)
            & prices["previous_close"].gt(0)
            & ~suspended
        )

    def _atomic_publish(self, frame: pd.DataFrame) -> None:
        csv_bytes = self._history_csv_bytes(frame)
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
            generated_at = self.now().astimezone(timezone.utc).replace(microsecond=0)
            manifest = {
                "version": version,
                "history_csv": f"versions/{version}/market_history.csv",
                "history_json": f"versions/{version}/market_history.json",
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            }
            manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
            manifest_temp = self._write_temp(self.manifest_path, manifest_bytes)
            try:
                os.replace(manifest_temp, self.manifest_path)
                self._fsync_directory(self.public_data_path)
                self._reconcile_canonical(csv_path)
            finally:
                manifest_temp.unlink(missing_ok=True)
        except Exception:
            try:
                self._reconcile_canonical_from_manifest()
            except Exception:
                # The manifest is the single live pointer. A later run repairs
                # this derived mirror if the best-effort write cannot finish.
                pass
            raise
        finally:
            csv_temp.unlink(missing_ok=True)
            json_temp.unlink(missing_ok=True)

    def _reconcile_canonical_from_manifest(self) -> None:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            history_csv = manifest.get("history_csv", manifest.get("csv"))
            if not isinstance(history_csv, str):
                raise KeyError("history_csv")
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise MarketDataValidationError(f"cannot read current manifest: {error}") from error
        self._reconcile_canonical(self.public_data_path / history_csv)

    def _reconcile_canonical(self, active_csv: Path) -> None:
        try:
            active_bytes = active_csv.read_bytes()
        except OSError as error:
            raise MarketDataValidationError(
                f"cannot read active history CSV: {error}"
            ) from error
        try:
            canonical_bytes = self.csv_path.read_bytes()
        except FileNotFoundError:
            canonical_bytes = None
        except OSError as error:
            raise MarketDataValidationError(
                f"cannot read canonical history CSV: {error}"
            ) from error
        if canonical_bytes == active_bytes:
            return
        canonical_temp = self._write_temp(self.csv_path, active_bytes)
        try:
            os.replace(canonical_temp, self.csv_path)
            self._fsync_directory(self.csv_path.parent)
        finally:
            canonical_temp.unlink(missing_ok=True)

    @staticmethod
    def _history_csv_bytes(frame: pd.DataFrame) -> bytes:
        return frame.loc[:, REQUIRED_COLUMNS].to_csv(
            index=False, float_format="%.12g", lineterminator="\n"
        ).encode("utf-8")

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
