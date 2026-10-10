"""Small, testable adapter around the AKShare market-data endpoints."""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.error import URLError

import pandas as pd

try:  # Keep injected, offline test clients usable before dependencies are installed.
    import akshare as ak
    from akshare.utils.demjson import JSONDecodeError as AkshareJSONDecodeError
except ImportError:  # pragma: no cover - production installs this from requirements.txt
    ak = None
    AkshareJSONDecodeError = json.JSONDecodeError


LOGGER = logging.getLogger(__name__)
SOURCE_CONTRACT = "unadjusted-v2"
T = TypeVar("T")
TRANSIENT_ERRORS = (TimeoutError, ConnectionError, socket.timeout, URLError)
SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688")
SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")


class MarketDataUnavailable(RuntimeError):
    """Raised when all contract-compatible sources fail."""

    def __init__(self, message: str, failures: list[dict] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []


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

    @property
    def cache_version(self) -> str:
        return (
            f"{SOURCE_CONTRACT}:akshare-{getattr(self.api, '__version__', 'injected')}"
        )

    def _from_sources(
        self,
        sources: list[tuple[str, Callable[[], Any]]],
        *,
        normalize: Callable[[Any], T] | None = None,
        **context: Any,
    ) -> T:
        failures = []
        for source, operation in sources:
            try:

                def fetch():
                    value = operation()
                    return normalize(value) if normalize is not None else value

                result = self._with_retry(fetch)
                LOGGER.info(json.dumps({**context, "source": source, "status": "ok"}))
                return result
            except Exception as error:
                if not (
                    self._is_transient_error(error)
                    or isinstance(
                        error,
                        (
                            MarketDataUnavailable,
                            MarketDataSchemaError,
                            AkshareJSONDecodeError,
                            json.JSONDecodeError,
                        ),
                    )
                ):
                    raise
                cause = error.__cause__ or error
                failure = {
                    **context,
                    "source": source,
                    "status": "failed",
                    "exception": type(cause).__name__,
                    "message": str(cause),
                }
                failures.append(failure)
                LOGGER.warning(json.dumps(failure, ensure_ascii=False))
        raise MarketDataUnavailable(
            f"all sources failed for {context}: "
            + "; ".join(
                f"{item['source']} {item['exception']}: {item['message']}"
                for item in failures
            ),
            failures,
        )

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
        if status_code == 429 or (
            isinstance(status_code, int) and 500 <= status_code <= 599
        ):
            return True

        return error.__class__.__module__.startswith(
            "requests"
        ) and error.__class__.__name__ in {
            "Timeout",
            "ConnectTimeout",
            "ReadTimeout",
            "ConnectionError",
            "ProxyError",
            "ChunkedEncodingError",
        }

    def get_index_history(self, start: str, end: str) -> pd.DataFrame:
        def normalize(frame):
            result = self._normalize(
                frame,
                {"date": ("date", "日期"), "close": ("close", "收盘")},
                numeric_columns=("close",),
            )
            if (
                result.empty
                or result["close"].isna().any()
                or result["close"].le(0).any()
            ):
                raise MarketDataSchemaError("empty or invalid index history")
            return result

        result = self._from_sources(
            [
                (
                    "sina.index",
                    lambda: self._require_api().stock_zh_index_daily(symbol="sh000001"),
                ),
                (
                    "eastmoney.index",
                    lambda: self._require_api().stock_zh_index_daily_em(
                        symbol="sh000001",
                        start_date=self._api_date(start),
                        end_date=self._api_date(end),
                    ),
                ),
            ],
            normalize=normalize,
            stage="index",
            symbol="sh000001",
            start=start,
            end=end,
        )
        return result.loc[
            result["date"].between(start, end), ["date", "close"]
        ].reset_index(drop=True)

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

        def normalize_current(frame):
            result = self._normalize(
                frame,
                {
                    "symbol": ("symbol", "code", "证券代码", "A股代码"),
                    "name": ("name", "证券简称", "A股简称"),
                },
            )
            result["symbol"] = result["symbol"].astype(str).str.zfill(6)
            result = result.loc[result["symbol"].map(self._is_a_share_symbol)]
            if not (
                result["symbol"].str.startswith(SH_A_SHARE_PREFIXES).any()
                and result["symbol"].str.startswith(SZ_A_SHARE_PREFIXES).any()
            ):
                raise MarketDataSchemaError("current universe requires both exchanges")
            if result["symbol"].duplicated().any():
                raise MarketDataSchemaError("duplicate current universe symbols")
            return result

        def eastmoney_current():
            return pd.concat(
                [
                    self._normalize_market(
                        self._require_api().stock_sh_a_spot_em(), "SH"
                    ),
                    self._normalize_market(
                        self._require_api().stock_sz_a_spot_em(), "SZ"
                    ),
                ],
                ignore_index=True,
            )

        current = self._from_sources(
            [
                (
                    "exchange.current",
                    lambda: self._require_api().stock_info_a_code_name(),
                ),
                ("eastmoney.current", eastmoney_current),
            ],
            normalize=normalize_current,
            stage="universe",
            symbol=None,
            start=None,
            end=None,
        )
        current["symbol"] = current["symbol"].astype(str).str.zfill(6)
        current = current.loc[current["symbol"].map(self._is_a_share_symbol)].copy()
        current["exchange"] = current["symbol"].map(
            lambda symbol: "SH" if symbol.startswith(SH_A_SHARE_PREFIXES) else "SZ"
        )
        current = current.loc[:, ["symbol", "exchange"]]
        current["listing_date"] = None
        current["delisting_date"] = None
        shanghai = self._normalize_delisted_market(
            self._from_sources(
                [
                    (
                        "sse.delisted",
                        lambda: self._require_api().stock_info_sh_delist(symbol="全部"),
                    )
                ],
                stage="universe",
                symbol="SH.delisted",
                start=None,
                end=None,
            ),
            "SH",
            symbol_columns=("symbol", "公司代码", "SECURITY_CODE_A", "证券代码"),
            listing_columns=("listing_date", "LISTING_DATE", "上市日期"),
            delisting_columns=(
                "delisting_date",
                "暂停上市日期",
                "CHANGE_DATE",
                "终止上市日期",
            ),
        )
        shenzhen = self._normalize_delisted_market(
            self._from_sources(
                [
                    (
                        "szse.delisted",
                        lambda: self._require_api().stock_info_sz_delist(
                            symbol="终止上市公司"
                        ),
                    )
                ],
                stage="universe",
                symbol="SZ.delisted",
                start=None,
                end=None,
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

        def normalize(frame):
            result = self._normalize(
                frame,
                {
                    "date": ("date", "日期"),
                    "current_close": ("current_close", "close", "收盘"),
                    "volume": ("volume", "成交量"),
                    "amount": ("amount", "成交额"),
                },
                numeric_columns=("current_close", "volume", "amount"),
            )
            if (
                result.empty
                or result["date"].isna().any()
                or result["date"].duplicated().any()
            ):
                raise MarketDataSchemaError("empty history or invalid stock dates")
            result["symbol"] = symbol
            result["is_suspended"] = result["volume"].eq(0) & result["amount"].eq(0)
            return result

        return self._from_sources(
            [
                (
                    "sina.daily",
                    lambda: self._require_api().stock_zh_a_daily(
                        symbol=(
                            "sh" if symbol.startswith(SH_A_SHARE_PREFIXES) else "sz"
                        )
                        + symbol,
                        start_date=self._api_date(start),
                        end_date=self._api_date(end),
                        adjust="",
                    ),
                ),
                (
                    "eastmoney.hist",
                    lambda: self._require_api().stock_zh_a_hist(
                        symbol=symbol,
                        period="daily",
                        start_date=self._api_date(start),
                        end_date=self._api_date(end),
                        adjust="",
                        timeout=20,
                    ),
                ),
            ],
            normalize=normalize,
            stage="stock_history",
            symbol=symbol,
            start=start,
            end=end,
        )

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
