import os
from typing import Type

import pytest

from arbitrage_telebot import Binance, Bybit, ExchangeAdapter, KuCoin, OKX, Quote


pytestmark = pytest.mark.skipif(
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
