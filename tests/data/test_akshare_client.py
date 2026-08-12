from urllib.error import HTTPError

import pandas as pd
import pytest
from akshare.utils.demjson import JSONDecodeError
from requests.exceptions import ProxyError

from scripts.market_data.akshare_client import (
    AkshareClient,
    MarketDataSchemaError,
    MarketDataUnavailable,
)


SAMPLE_FRAME = pd.DataFrame({"date": ["2025-01-02"], "close": [3_200.0]})


class FakeAkshareApi:
    def stock_zh_index_daily(self, **kwargs):
        self.index_daily_request = kwargs
        return pd.DataFrame(
            {
                "date": ["2024-12-31", "2025-01-02", "2025-01-06"],
                "close": [3_190.0, 3_200.0, 3_210.0],
            }
        )

    def stock_zh_index_daily_em(self, **kwargs):
        self.index_request = kwargs
        return pd.DataFrame({"日期": ["2025-01-02"], "收盘": [3_200.0]})

    def stock_info_a_code_name(self):
        return pd.DataFrame(
            {
                "code": ["600000", "688001", "000001", "300001", "430047"],
                "name": ["浦发银行", "科创板", "平安银行", "创业板", "北交所"],
            }
        )

    def stock_sh_a_spot_em(self):
        return pd.DataFrame(
            {
                "代码": ["600000", "900901", "688001"],
                "名称": ["浦发银行", "沪市 B 股", "科创板"],
                "最新价": [10.5, 1.0, None],
                "昨收": [10.0, 1.0, 20.0],
            }
        )

    def stock_sz_a_spot_em(self):
        return pd.DataFrame(
            {
                "代码": ["000001", "200001", "300001"],
                "名称": ["平安银行", "深市 B 股", "创业板"],
                "最新价": [11.0, 2.0, 8.0],
                "昨收": [10.0, 2.0, 8.0],
            }
        )

    def stock_zh_a_hist(self, **kwargs):
        self.history_request = kwargs
        return pd.DataFrame(
            {
                "日期": ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"],
                "股票代码": ["600000"] * 4,
                "收盘": [10.0, 10.0, 10.0, 11.0],
                "成交量": [100.0, 0.0, 0.0, 0.0],
                "成交额": [1000.0, 0.0, 1000.0, None],
            }
        )

    def stock_zh_a_daily(self, **kwargs):
        self.daily_history_request = kwargs
        return pd.DataFrame(
            {
                "date": ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"],
                "close": [10.0, 10.0, 10.0, 11.0],
                "volume": [100.0, 0.0, 0.0, 0.0],
                "amount": [1000.0, 0.0, 1000.0, None],
            }
        )

    def stock_info_sh_delist(self, symbol="全部"):
        assert symbol == "全部"
        return pd.DataFrame(
            {
                "公司代码": ["600999", "900999"],
                "上市日期": ["2020-01-01", "2020-01-01"],
                "暂停上市日期": ["2025-01-07", "2025-01-07"],
            }
        )

    def stock_info_sz_delist(self, symbol="终止上市公司"):
        assert symbol == "终止上市公司"
        return pd.DataFrame(
            {
                "证券代码": ["000999", "200999"],
                "上市日期": ["2020-01-01", "2020-01-01"],
                "终止上市日期": ["2025-01-07", "2025-01-07"],
            }
        )


def test_retry_uses_three_delays():
    outcomes = iter([TimeoutError(), TimeoutError(), TimeoutError(), SAMPLE_FRAME])
    delays = []
    client = AkshareClient(api=object(), sleeper=delays.append)

    def operation():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result = client._with_retry(operation)

    assert result is SAMPLE_FRAME
    assert delays == [30, 90, 180]


def test_retry_raises_unavailable_after_all_transient_attempts():
    delays = []
    client = AkshareClient(api=object(), sleeper=delays.append)

    with pytest.raises(MarketDataUnavailable, match="timed out"):
        client._with_retry(lambda: (_ for _ in ()).throw(TimeoutError("timed out")))

    assert delays == [30, 90, 180]


def test_retry_retries_rate_limited_http_requests():
    outcomes = iter(
        [
            HTTPError("https://example.test", 429, "Too Many Requests", {}, None),
            SAMPLE_FRAME,
        ]
    )
    delays = []
    client = AkshareClient(api=object(), sleeper=delays.append)

    def operation():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert client._with_retry(operation) is SAMPLE_FRAME
    assert delays == [30]


def test_retry_retries_transient_proxy_disconnects():
    outcomes = iter([ProxyError("proxy disconnected"), SAMPLE_FRAME])
    delays = []
    client = AkshareClient(api=object(), sleeper=delays.append)

    def operation():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert client._with_retry(operation) is SAMPLE_FRAME
    assert delays == [30]


def test_retry_does_not_retry_non_transient_errors():
    delays = []
    client = AkshareClient(api=object(), sleeper=delays.append)

    with pytest.raises(ValueError, match="unexpected schema"):
        client._with_retry(lambda: (_ for _ in ()).throw(ValueError("unexpected schema")))

    assert delays == []


def test_get_index_history_normalizes_dates_closes_and_request_parameters():
    api = FakeAkshareApi()
    result = AkshareClient(api=api).get_index_history("2025-01-01", "2025-01-03")

    assert result.to_dict("records") == [{"date": "2025-01-02", "close": 3_200.0}]
    assert api.index_daily_request == {"symbol": "sh000001"}


def test_current_market_filters_to_shanghai_and_shenzhen_a_shares():
    result = AkshareClient(api=FakeAkshareApi()).get_current_market()

    assert result[["symbol", "exchange"]].to_dict("records") == [
        {"symbol": "600000", "exchange": "SH"},
        {"symbol": "688001", "exchange": "SH"},
        {"symbol": "000001", "exchange": "SZ"},
        {"symbol": "300001", "exchange": "SZ"},
    ]
    assert result.loc[result["symbol"] == "688001", "is_suspended"].item() is True
    assert set(result["exchange"]) == {"SH", "SZ"}


def test_security_universe_merges_current_and_delisted_a_shares_with_lifecycle_dates():
    result = AkshareClient(api=FakeAkshareApi()).get_security_universe()

    assert result.to_dict("records") == [
        {"symbol": "000001", "exchange": "SZ", "listing_date": None, "delisting_date": None},
        {"symbol": "000999", "exchange": "SZ", "listing_date": "2020-01-01", "delisting_date": "2025-01-07"},
        {"symbol": "300001", "exchange": "SZ", "listing_date": None, "delisting_date": None},
        {"symbol": "600000", "exchange": "SH", "listing_date": None, "delisting_date": None},
        {"symbol": "600999", "exchange": "SH", "listing_date": "2020-01-01", "delisting_date": "2025-01-07"},
        {"symbol": "688001", "exchange": "SH", "listing_date": None, "delisting_date": None},
    ]


def test_security_universe_does_not_depend_on_eastmoney_spot_pages():
    class ExchangeListApi(FakeAkshareApi):
        def stock_sh_a_spot_em(self):
            raise AssertionError("Eastmoney spot endpoint must not be called")

        def stock_sz_a_spot_em(self):
            raise AssertionError("Eastmoney spot endpoint must not be called")

    result = AkshareClient(api=ExchangeListApi()).get_security_universe()

    assert set(result["symbol"]) == {
        "000001",
        "000999",
        "300001",
        "600000",
        "600999",
        "688001",
    }


def test_get_stock_history_uses_unadjusted_daily_prices_and_normalizes_columns():
    api = FakeAkshareApi()
    result = AkshareClient(api=api).get_stock_history(
        "600000", "2025-01-01", "2025-01-03"
    )

    assert result["is_suspended"].tolist() == [False, True, False, False]
    assert result[["date", "symbol", "current_close", "volume"]].to_dict("records") == [
        {"date": "2025-01-02", "symbol": "600000", "current_close": 10.0, "volume": 100.0},
        {"date": "2025-01-03", "symbol": "600000", "current_close": 10.0, "volume": 0.0},
        {"date": "2025-01-06", "symbol": "600000", "current_close": 10.0, "volume": 0.0},
        {"date": "2025-01-07", "symbol": "600000", "current_close": 11.0, "volume": 0.0},
    ]
    assert api.daily_history_request == {
        "symbol": "sh600000",
        "start_date": "20250101",
        "end_date": "20250103",
        "adjust": "",
    }


def test_get_stock_history_falls_back_when_sina_returns_non_json_for_delisted_stock():
    class DelistedStockApi(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            self.daily_history_request = kwargs
            raise JSONDecodeError("No value to decode")

    api = DelistedStockApi()

    result = AkshareClient(api=api).get_stock_history(
        "000004", "2025-01-01", "2025-01-03"
    )

    assert result["current_close"].tolist() == [10.0, 10.0, 10.0, 11.0]
    assert api.daily_history_request["symbol"] == "sz000004"
    assert api.history_request == {
        "symbol": "000004",
        "period": "daily",
        "start_date": "20250101",
        "end_date": "20250103",
        "adjust": "",
        "timeout": 20,
    }


@pytest.mark.parametrize("symbol", ["900901", "200001", "430047", "510300"])
def test_get_stock_history_rejects_non_a_share_symbols_before_fetching(symbol):
    class RecordingApi:
        def __init__(self):
            self.history_calls = []

        def stock_zh_a_hist(self, **kwargs):
            self.history_calls.append(kwargs)
            return pd.DataFrame(
                {
                    "日期": ["2025-01-02"],
                    "股票代码": [symbol],
                    "收盘": [10.5],
                }
            )

    api = RecordingApi()

    with pytest.raises(ValueError, match="not an eligible Shanghai or Shenzhen A-share"):
        AkshareClient(api=api).get_stock_history(symbol, "2025-01-01", "2025-01-03")

    assert api.history_calls == []


def test_schema_changes_raise_without_retrying():
    class BrokenApi:
        def stock_zh_index_daily(self, **kwargs):
            return pd.DataFrame({"日期": ["2025-01-02"]})

    client = AkshareClient(api=BrokenApi(), sleeper=lambda _: pytest.fail("retried"))

    with pytest.raises(MarketDataSchemaError, match="close"):
        client.get_index_history("2025-01-01", "2025-01-03")
