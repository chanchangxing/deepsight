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
        def stock_zh_index_daily_em(self, **kwargs):
            return pd.DataFrame({"日期": ["2025-01-02"]})

        def stock_zh_index_daily(self, **kwargs):
            return pd.DataFrame({"日期": ["2025-01-02"]})

    client = AkshareClient(api=BrokenApi(), sleeper=lambda _: pytest.fail("retried"))

    with pytest.raises(MarketDataUnavailable, match="close"):
        client.get_index_history("2025-01-01", "2025-01-03")


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        ProxyError("remote disconnected"),
        JSONDecodeError("No value to decode"),
    ],
)
def test_stock_source_switches_after_controlled_upstream_errors(error, caplog):
    class UnavailableSina(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise error

    api = UnavailableSina()
    result = AkshareClient(api=api, delays=()).get_stock_history(
        "600000", "2025-01-01", "2025-01-03"
    )
    assert not result.empty
    assert api.history_request["adjust"] == ""
    import json

    diagnostic = json.loads(caplog.records[0].message)
    assert diagnostic["symbol"] == "600000"
    assert diagnostic["source"] == "sina.daily"
    assert diagnostic["start"] == "2025-01-01"
    assert diagnostic["exception"] == type(error).__name__


def test_both_stock_sources_fail_with_original_causes():
    class UnavailableApi(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise JSONDecodeError("No value to decode")

        def stock_zh_a_hist(self, **kwargs):
            raise ProxyError("remote disconnected")

    with pytest.raises(MarketDataUnavailable) as caught:
        AkshareClient(api=UnavailableApi(), delays=()).get_stock_history(
            "600000", "2025-01-01", "2025-01-03"
        )
    assert [item["source"] for item in caught.value.failures] == [
        "sina.daily",
        "eastmoney.hist",
    ]
    assert [item["exception"] for item in caught.value.failures] == [
        "JSONDecodeError",
        "ProxyError",
    ]


def test_programming_error_does_not_silently_switch_sources():
    class BrokenApi(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise TypeError("programming error")

    with pytest.raises(TypeError):
        AkshareClient(api=BrokenApi()).get_stock_history(
            "600000", "2025-01-01", "2025-01-03"
        )


def test_list_network_failure_uses_complete_current_alternative_and_keeps_delisted():
    class UnavailableListApi(FakeAkshareApi):
        def stock_info_a_code_name(self):
            raise ConnectionError("SSE unreachable")

    result = AkshareClient(api=UnavailableListApi(), delays=()).get_security_universe()
    assert {"600999", "000999", "600000", "000001"}.issubset(set(result["symbol"]))


def test_incomplete_current_alternative_and_missing_delisted_list_fail_closed():
    class UnavailableListApi(FakeAkshareApi):
        def stock_info_a_code_name(self):
            raise ConnectionError("SSE unreachable")

        def stock_sz_a_spot_em(self):
            return super().stock_sz_a_spot_em().iloc[:0]

    with pytest.raises(MarketDataUnavailable, match="both exchanges"):
        AkshareClient(api=UnavailableListApi(), delays=()).get_security_universe()

    class MissingDelistedApi(FakeAkshareApi):
        def stock_info_sh_delist(self, **kwargs):
            raise TimeoutError("delisted unavailable")

    with pytest.raises(MarketDataUnavailable, match="delisted"):
        AkshareClient(api=MissingDelistedApi(), delays=()).get_security_universe()


def test_missing_share_capital_recovers_prices_without_eastmoney_or_skipping_stock():
    class NullShareCapitalApi(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise JSONDecodeError("No value to decode")

        def stock_zh_a_hist(self, **kwargs):
            pytest.fail("complete Sina prices should prevent Eastmoney request")

    calls = []

    def prices(symbol, start, end):
        calls.append((symbol, start, end))
        return pd.DataFrame(
            {
                "date": ["2026-07-10", "2026-07-13"],
                "close": [0.51, 0.51],
                "volume": [100, 100],
                "amount": [51, 51],
            }
        )

    result = AkshareClient(
        api=NullShareCapitalApi(), delays=(), sina_price_history=prices
    ).get_stock_history("000004", "2025-01-01", "2026-10-10")
    assert calls == [("sz000004", "2025-01-01", "2026-10-10")]
    assert result["date"].tolist() == ["2026-07-10", "2026-07-13"]
    assert result["symbol"].tolist() == ["000004", "000004"]
    assert result["is_suspended"].tolist() == [False, False]


def test_price_fallback_failure_still_tries_eastmoney():
    class UnavailableApi(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise JSONDecodeError("null shares")

    def unavailable(*args):
        raise TimeoutError("price timeout")

    api = UnavailableApi()
    result = AkshareClient(
        api=api, delays=(), sina_price_history=unavailable
    ).get_stock_history("000004", "2025-01-01", "2026-10-10")
    assert not result.empty
    assert api.history_request["symbol"] == "000004"


def test_all_three_sources_failure_preserves_each_cause():
    class UnavailableApi(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise JSONDecodeError("null shares")

        def stock_zh_a_hist(self, **kwargs):
            raise ProxyError("Eastmoney disconnected")

    def unavailable(*args):
        raise MarketDataSchemaError("bad price assignment")

    with pytest.raises(MarketDataUnavailable) as caught:
        AkshareClient(
            api=UnavailableApi(), delays=(), sina_price_history=unavailable
        ).get_stock_history("000004", "2025-01-01", "2026-10-10")
    assert [item["source"] for item in caught.value.failures] == [
        "sina.daily",
        "sina.prices",
        "eastmoney.hist",
    ]


RAW_PRICE_ROWS = [
    {"date": "2024-12-31T00:00:00.000Z", "close": 0.5, "volume": 100, "amount": 50},
    {"date": "2026-07-10T00:00:00.000Z", "close": 0.51, "volume": 100, "amount": 51},
    {"date": "2026-07-13T00:00:00.000Z", "close": 0.51, "volume": 100, "amount": 51},
]


def mock_sina_price_payload(
    monkeypatch, text='var KLC_K2_sz000004="K2/ABC";', rows=...
):
    import py_mini_racer
    import requests

    calls = []

    class Reply:
        def __init__(self):
            self.text = text

        def raise_for_status(self):
            calls.append("checked HTTP status")

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Reply()

    class Decoder:
        def eval(self, source):
            from akshare.stock.cons import hk_js_decode

            assert source == hk_js_decode

        def call(self, name, payload):
            calls.append((name, payload))
            return RAW_PRICE_ROWS if rows is ... else rows

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(py_mini_racer, "MiniRacer", Decoder)
    return calls


def test_raw_sina_prices_are_bounded_and_preserve_identical_dated_turnover(monkeypatch):
    calls = mock_sina_price_payload(monkeypatch)
    result = AkshareClient._sina_price_history("sz000004", "2025-01-01", "2026-10-10")
    assert result["date"].tolist() == ["2026-07-10", "2026-07-13"]
    assert result["close"].tolist() == [0.51, 0.51]
    assert calls[0][1] == {"timeout": 20}
    assert "checked HTTP status" in calls
    assert calls[-1] == ("d", "K2/ABC")


@pytest.mark.parametrize(
    "payload",
    [
        "var KLC_K2_sz000004=null;",
        'var KLC_K2_sz000001="K2/ABC";',
        'var KLC_K2_sz000004="K2/ABC"; alert("unexpected script");',
        "<html>upstream unavailable</html>",
    ],
)
def test_raw_sina_rejects_null_wrong_symbol_and_extra_code(monkeypatch, payload):
    calls = mock_sina_price_payload(monkeypatch, text=payload)
    with pytest.raises(MarketDataSchemaError, match="assignment"):
        AkshareClient._sina_price_history("sz000004", "2025-01-01", "2026-10-10")
    assert not any(isinstance(item, tuple) and item[0] == "d" for item in calls)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        None,
        {},
        ["invalid row"],
        [{"date": "2026-07-13", "close": 0.51, "volume": 100}],
        [RAW_PRICE_ROWS[-1], RAW_PRICE_ROWS[-1]],
        [{**RAW_PRICE_ROWS[-1], "amount": None}],
        [{**RAW_PRICE_ROWS[-1], "close": float("inf")}],
    ],
)
def test_raw_sina_rejects_incomplete_or_duplicate_rows(monkeypatch, rows):
    mock_sina_price_payload(monkeypatch, rows=rows)
    with pytest.raises(MarketDataSchemaError):
        AkshareClient._sina_price_history("sz000004", "2025-01-01", "2026-10-10")


def test_empty_post_delisting_request_does_not_invent_prices(monkeypatch):
    mock_sina_price_payload(monkeypatch)
    result = AkshareClient._sina_price_history("sz000004", "2026-07-14", "2026-10-10")
    assert result.empty


@pytest.mark.parametrize(
    "suffix", ["\n/* wlSZEhybD7iGzvb1xBU4BjcGg6+4CD */", "\n/* first */\n/* second */"]
)
def test_raw_sina_accepts_trailing_block_comments_without_evaluating_them(
    monkeypatch, suffix
):
    mock_sina_price_payload(monkeypatch, text='var KLC_K2_sz000004="K2/ABC";' + suffix)
    result = AkshareClient._sina_price_history("sz000004", "2025-01-01", "2026-10-10")
    assert len(result) == 2


def test_comment_does_not_hide_executable_trailing_code(monkeypatch):
    mock_sina_price_payload(
        monkeypatch,
        text='var KLC_K2_sz000004="K2/ABC"; /* first */ alert("extra"); /* last */',
    )
    with pytest.raises(MarketDataSchemaError, match="assignment"):
        AkshareClient._sina_price_history("sz000004", "2025-01-01", "2026-10-10")


def test_raw_sina_http_failure_switches_to_eastmoney_without_retry(monkeypatch):
    import requests

    class FailedDaily(FakeAkshareApi):
        def stock_zh_a_daily(self, **kwargs):
            raise MarketDataSchemaError("auxiliary capital unavailable")

    def denied(*args, **kwargs):
        raise requests.HTTPError("404 missing price payload")

    sleeps = []
    client = AkshareClient(
        api=FailedDaily(),
        sleeper=sleeps.append,
        sina_price_history=AkshareClient._sina_price_history,
    )
    monkeypatch.setattr(requests, "get", denied)
    history = client.get_stock_history("000004", "2025-01-01", "2026-10-10")
    assert not history.empty
    assert sleeps == []
