"""Small, testable adapter around the AKShare market-data endpoints."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.error import URLError

import pandas as pd

try:  # Keep injected, offline test clients usable before dependencies are installed.
    import akshare as ak
except ImportError:  # pragma: no cover - production installs this from requirements.txt
    ak = None


T = TypeVar("T")
TRANSIENT_ERRORS = (TimeoutError, ConnectionError, socket.timeout, URLError)
SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688")
SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")


class MarketDataUnavailable(RuntimeError):
    """Raised after the upstream market-data service exhausts its retries."""


class MarketDataSchemaError(ValueError):
    """Raised when AKShare returns data that cannot meet this adapter's contract."""


class AkshareClient:
    """Fetch and normalize the limited AKShare data used by the market pipeline."""

    def __init__(
        self,
        api: Any = ak,
        sleeper: Callable[[float], None] = time.sleep,
        delays: tuple[int, ...] = (30, 90, 180),
    ) -> None:
        self.api = api
        self.sleeper = sleeper
        self.delays = delays

    def _with_retry(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except Exception as first_error:
            if not self._is_transient_error(first_error):
                raise
            error = first_error

        for delay in self.delays:
            self.sleeper(delay)
            try:
                return operation()
            except Exception as next_error:
                if not self._is_transient_error(next_error):
                    raise
                error = next_error

        raise MarketDataUnavailable(str(error)) from error

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        if isinstance(error, TRANSIENT_ERRORS):
            return True

        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        status_code = getattr(error, "status", getattr(error, "code", status_code))
        if status_code == 429:
            return True

        return (
            error.__class__.__module__.startswith("requests")
            and error.__class__.__name__
            in {"Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"}
        )

    def get_index_history(self, start: str, end: str) -> pd.DataFrame:
        frame = self._with_retry(
            lambda: self._require_api().stock_zh_index_daily_em(
                symbol="sh000001",
                start_date=self._api_date(start),
                end_date=self._api_date(end),
            )
        )
        return self._normalize(
            frame,
            {
                "date": ("date", "日期"),
                "close": ("close", "收盘"),
            },
            numeric_columns=("close",),
        )

    def get_current_market(self) -> pd.DataFrame:
        shanghai = self._normalize_market(
            self._with_retry(lambda: self._require_api().stock_sh_a_spot_em()), "SH"
        )
        shenzhen = self._normalize_market(
            self._with_retry(lambda: self._require_api().stock_sz_a_spot_em()), "SZ"
        )
        return pd.concat([shanghai, shenzhen], ignore_index=True)

    def get_security_universe(self) -> pd.DataFrame:
        """Return current and delisted eligible A shares with lifecycle boundaries."""
        current = self.get_current_market().loc[:, ["symbol", "exchange"]].copy()
        current["listing_date"] = None
        current["delisting_date"] = None
        shanghai = self._normalize_delisted_market(
            self._with_retry(
                lambda: self._require_api().stock_info_sh_delist(indicator="终止上市公司")
            ),
            "SH",
            symbol_columns=("symbol", "SECURITY_CODE_A", "证券代码"),
            listing_columns=("listing_date", "LISTING_DATE", "上市日期"),
            delisting_columns=("delisting_date", "CHANGE_DATE", "终止上市日期"),
        )
        shenzhen = self._normalize_delisted_market(
            self._with_retry(
                lambda: self._require_api().stock_info_sz_delist(indicator="终止上市公司")
            ),
            "SZ",
            symbol_columns=("symbol", "证券代码", "代码"),
            listing_columns=("listing_date", "上市日期", "LISTING_DATE"),
            delisting_columns=("delisting_date", "终止上市日期", "CHANGE_DATE"),
        )
        result = pd.concat([current, shanghai, shenzhen], ignore_index=True)
        result = result.drop_duplicates("symbol", keep="first")
        return result.sort_values("symbol").reset_index(drop=True)

    def get_stock_history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        if not self._is_a_share_symbol(symbol):
            raise ValueError(
                f"{symbol!r} is not an eligible Shanghai or Shenzhen A-share symbol"
            )
        frame = self._with_retry(
            lambda: self._require_api().stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=self._api_date(start),
                end_date=self._api_date(end),
                adjust="",
            )
        )
        result = self._normalize(
            frame,
            {
                "date": ("date", "日期"),
                "symbol": ("symbol", "股票代码", "代码"),
                "current_close": ("current_close", "收盘"),
            },
            numeric_columns=("current_close",),
        )
        return result

    def _normalize_market(self, frame: pd.DataFrame, exchange: str) -> pd.DataFrame:
        result = self._normalize(
            frame,
            {
                "symbol": ("symbol", "代码", "股票代码"),
                "name": ("name", "名称"),
                "current_close": ("current_close", "最新价"),
                "previous_close": ("previous_close", "昨收"),
            },
            numeric_columns=("current_close", "previous_close"),
        )
        result["symbol"] = result["symbol"].astype(str).str.zfill(6)
        result["exchange"] = exchange
        result["is_suspended"] = result["current_close"].isna() | result[
            "current_close"
        ].le(0)

        prefixes = SH_A_SHARE_PREFIXES if exchange == "SH" else SZ_A_SHARE_PREFIXES
        return result.loc[result["symbol"].str.startswith(prefixes)].reset_index(drop=True)

    def _normalize_delisted_market(
        self,
        frame: pd.DataFrame,
        exchange: str,
        symbol_columns: tuple[str, ...],
        listing_columns: tuple[str, ...],
        delisting_columns: tuple[str, ...],
    ) -> pd.DataFrame:
        result = self._normalize(
            frame,
            {
                "symbol": symbol_columns,
                "listing_date": listing_columns,
                "delisting_date": delisting_columns,
            },
        )
        result["symbol"] = result["symbol"].astype(str).str.zfill(6)
        for column in ("listing_date", "delisting_date"):
            result[column] = pd.to_datetime(result[column], errors="raise").dt.strftime(
                "%Y-%m-%d"
            )
        prefixes = SH_A_SHARE_PREFIXES if exchange == "SH" else SZ_A_SHARE_PREFIXES
        result = result.loc[result["symbol"].str.startswith(prefixes)].copy()
        result["exchange"] = exchange
        return result[["symbol", "exchange", "listing_date", "delisting_date"]]

    @staticmethod
    def _is_a_share_symbol(symbol: str) -> bool:
        normalized = str(symbol).strip()
        return (
            len(normalized) == 6
            and normalized.isascii()
            and normalized.isdigit()
            and normalized.startswith(SH_A_SHARE_PREFIXES + SZ_A_SHARE_PREFIXES)
        )

    @staticmethod
    def _normalize(
        frame: pd.DataFrame,
        aliases: dict[str, tuple[str, ...]],
        numeric_columns: tuple[str, ...] = (),
    ) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise MarketDataSchemaError("AKShare response is not a DataFrame")

        columns: dict[str, pd.Series] = {}
        for normalized_name, candidates in aliases.items():
            source_name = next((name for name in candidates if name in frame.columns), None)
            if source_name is None:
                raise MarketDataSchemaError(
                    f"missing required column: {normalized_name}"
                )
            columns[normalized_name] = frame[source_name]

        result = pd.DataFrame(columns)
        try:
            if "date" in result:
                result["date"] = pd.to_datetime(result["date"], errors="raise").dt.strftime(
                    "%Y-%m-%d"
                )
            for column in numeric_columns:
                result[column] = pd.to_numeric(result[column], errors="raise")
        except (TypeError, ValueError) as error:
            raise MarketDataSchemaError(str(error)) from error
        return result

    def _require_api(self) -> Any:
        if self.api is None:
            raise MarketDataUnavailable("AKShare is not installed")
        return self.api

    @staticmethod
    def _api_date(value: str) -> str:
        return pd.Timestamp(value).strftime("%Y%m%d")
