from urllib.error import HTTPError

import pandas as pd
import pytest

from scripts.market_data.akshare_client import (
    AkshareClient,
    MarketDataSchemaError,
    MarketDataUnavailable,
)


SAMPLE_FRAME = pd.DataFrame({"date": ["2025-01-02"], "close": [3_200.0]})


class FakeAkshareApi:
    def stock_zh_index_daily_em(self, **kwargs):
        self.index_request = kwargs
        return pd.DataFrame({"日期": ["2025-01-02"], "收盘": [3_200.0]})

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
                "日期": ["2025-01-02"],
                "股票代码": ["600000"],
                "收盘": [10.5],
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
    assert api.index_request == {
        "symbol": "sh000001",
        "start_date": "20250101",
        "end_date": "20250103",
    }


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


def test_get_stock_history_uses_unadjusted_daily_prices_and_normalizes_columns():
    api = FakeAkshareApi()
    result = AkshareClient(api=api).get_stock_history(
        "600000", "2025-01-01", "2025-01-03"
    )

    assert result.to_dict("records") == [
        {"date": "2025-01-02", "symbol": "600000", "current_close": 10.5}
    ]
    assert api.history_request == {
        "symbol": "600000",
        "period": "daily",
        "start_date": "20250101",
        "end_date": "20250103",
        "adjust": "",
    }


def test_schema_changes_raise_without_retrying():
    class BrokenApi:
        def stock_zh_index_daily_em(self, **kwargs):
            return pd.DataFrame({"日期": ["2025-01-02"]})

    client = AkshareClient(api=BrokenApi(), sleeper=lambda _: pytest.fail("retried"))

    with pytest.raises(MarketDataSchemaError, match="close"):
        client.get_index_history("2025-01-01", "2025-01-03")
