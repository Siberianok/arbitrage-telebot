import time

import arbitrage_telebot as bot


class DummyAdapter(bot.ExchangeAdapter):
    def __init__(self, quotes):
        self.quotes = quotes

    def normalize_symbol(self, pair: str) -> str:
        return pair

    def fetch_quote(self, pair: str):
        return self.quotes.get(pair)


def _quote(symbol: str, bid: float, ask: float, ts: int, source: str = "ticker") -> bot.Quote:
    return bot.Quote(symbol=symbol, bid=bid, ask=ask, ts=ts, source=source)


def test_validate_quote_quality_rejects_stale_and_outlier(monkeypatch):
    now_ms = int(time.time() * 1000)
    monkeypatch.setitem(
        bot.CONFIG,
        "quote_quality",
        {
            "max_age_seconds_by_venue": {"default": 10, "binance": 2},
            "max_timestamp_skew_ms_by_source": {"default": 5_000},
            "max_mid_deviation_percent": 1.0,
            "max_spread_percent": 0.6,
        },
    )

    pair_quotes = {
        "binance": _quote("BTC/USDT", 33000.0, 33500.0, now_ms - 20_000),
        "bybit": _quote("BTC/USDT", 30010.0, 30020.0, now_ms),
        "okx": _quote("BTC/USDT", 30011.0, 30021.0, now_ms),
    }

    valid, reasons, quality_score = bot.validate_quote_quality(
        pair="BTC/USDT",
        venue="binance",
        quote=pair_quotes["binance"],
        pair_quotes=pair_quotes,
        now_ms=now_ms,
    )

    assert valid is False
    assert "stale_quote" in reasons
    assert "intervenue_outlier" in reasons
    assert "anomalous_spread" in reasons
    assert quality_score < 0.5


def test_fetch_all_quotes_discards_invalid_quotes(monkeypatch):
    now_ms = int(time.time() * 1000)
    monkeypatch.setitem(
        bot.CONFIG,
        "quote_quality",
        {
            "max_age_seconds_by_venue": {"default": 10},
            "max_timestamp_skew_ms_by_source": {"default": 10_000},
            "max_mid_deviation_percent": 10.0,
            "max_spread_percent": 5.0,
        },
    )

    adapters = {
        "binance": DummyAdapter({"BTC/USDT": _quote("BTC/USDT", 30000.0, 30010.0, now_ms)}),
        "bybit": DummyAdapter({"BTC/USDT": _quote("BTC/USDT", 30100.0, 30050.0, now_ms)}),
    }

    pair_quotes, discards = bot.fetch_all_quotes(["BTC/USDT"], adapters)

    assert "binance" in pair_quotes["BTC/USDT"]
    assert "bybit" not in pair_quotes["BTC/USDT"]
    assert discards
    assert discards[0]["reason"] == "inverted_spread"
