import pytest

import arbitrage_telebot as bot


def test_generic_p2p_accepts_lowercase_placeholders(monkeypatch):
    venue_name = "testagg"
    pair = "BTC/ARS"
    config_entry = {
        "enabled": True,
        "adapter": "generic_p2p",
        "taker_fee_percent": 0.5,
        "fees": {
            "default": {
                "taker": 0.5,
                "maker": 0.4,
                "slippage_bps": 10.0,
            }
        },
        "p2p": {
            "enabled": True,
            "method": "GET",
            "endpoint": "https://example.com/api/{venue_lower}/{asset_lower}/{fiat_lower}",
            "bid_path": ["bid"],
            "ask_path": ["ask"],
            "timestamp_path": ["time"],
            "pairs": {
                pair: {
                    "asset": "BTC",
                    "fiat": "ARS",
                    "static_quote": {"bid": 16000000.0, "ask": 17000000.0},
                    "metadata": {"aggregator": "unit-test"},
                }
            },
        },
    }
    monkeypatch.setitem(bot.CONFIG["venues"], venue_name, config_entry)

    requested_urls = []

    def fake_http_get_json(url, **kwargs):
        requested_urls.append(url)
        return bot.HttpJsonResponse(
            {"bid": 15800000.0, "ask": 16200000.0, "time": 1_700_000_000},
            "checksum",
            bot.current_millis(),
        )

    monkeypatch.setattr(bot, "http_get_json", fake_http_get_json)

    adapter = bot.GenericP2PMarketplace(venue_name)
    quote = adapter.fetch_quote(pair)

    assert requested_urls == ["https://example.com/api/testagg/btc/ars"]
    assert quote is not None
    assert quote.bid == pytest.approx(15800000.0)
    assert quote.ask == pytest.approx(16200000.0)
    assert quote.metadata.get("aggregator") == "unit-test"
    assert quote.metadata.get("fiat") == "ARS"
