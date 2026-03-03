import os
from typing import Type

import pytest

import arbitrage_telebot as bot
from arbitrage_telebot import Binance, Bybit, ExchangeAdapter, KuCoin, OKX, Quote


@pytest.mark.integration
@pytest.mark.skipif(
    bool(os.getenv("SKIP_NETWORK_TESTS")),
    reason="network tests disabled via SKIP_NETWORK_TESTS",
)
@pytest.mark.parametrize(
    "adapter_cls,pair",
    [
        (Binance, "BTC/USDT"),
        (Bybit, "BTC/USDT"),
        (KuCoin, "BTC/USDT"),
        (OKX, "BTC/USDT"),
    ],
)
def test_exchange_connectivity(adapter_cls: Type[ExchangeAdapter], pair: str):
    adapter = adapter_cls()
    try:
        quote: Quote = adapter.fetch_quote(pair)
    except Exception as exc:  # pragma: no cover - real network failure
        pytest.fail(f"Conectividad fallida para {adapter_cls.__name__}: {exc}")

    assert quote is not None, f"{adapter_cls.__name__} devolvió sin datos"
    assert quote.bid > 0
    assert quote.ask > 0


def test_exchange_adapter_contract_smoke_with_mocked_http(monkeypatch):
    now_ms = bot.current_millis()

    def fake_http_get_json(url, params=None, **kwargs):
        if "binance" in url:
            data = {"bidPrice": "100.0", "askPrice": "101.0", "time": now_ms}
        elif "bybit" in url:
            data = {"result": {"list": [{"bid1Price": "100.0", "ask1Price": "101.0", "time": now_ms}]}}
        elif "kucoin" in url:
            data = {"data": {"bestBid": "100.0", "bestAsk": "101.0", "time": now_ms}}
        elif "okx" in url:
            data = {"data": [{"bidPx": "100.0", "askPx": "101.0", "ts": str(now_ms)}]}
        else:
            raise AssertionError(f"Unexpected url {url}")
        return bot.HttpJsonResponse(data, "mocked", bot.current_millis())

    monkeypatch.setattr(bot, "http_get_json", fake_http_get_json)
    monkeypatch.setattr(bot.ExchangeAdapter, "_attach_depth", lambda self, pair, quote: quote)

    for adapter_cls in (Binance, Bybit, KuCoin, OKX):
        quote = adapter_cls().fetch_quote("BTC/USDT")
        assert quote is not None
        assert quote.bid == pytest.approx(100.0)
        assert quote.ask == pytest.approx(101.0)
