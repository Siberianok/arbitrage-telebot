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


def test_compute_spot_p2p_opportunities_applies_execution_filters(monkeypatch):
    monkeypatch.setitem(
        bot.CONFIG,
        "p2p_execution",
        {
            "allowed_payment_methods": ["BANK_TRANSFER"],
            "min_advertiser_reputation": 0.90,
        },
    )
    monkeypatch.setitem(bot.CONFIG, "simulation_capital_quote", 10_000)

    spot_quotes = {"binance": bot.Quote("USDTARS", bid=1000.0, ask=1010.0, ts=1)}
    p2p_quotes = {
        "binance": bot.Quote(
            "USDTARS",
            bid=1040.0,
            ask=980.0,
            ts=1,
            source="p2p",
            metadata={
                "fiat": "ARS",
                "payment_method": "BANK_TRANSFER",
                "bank": "Banco Uno",
                "amount_min": 5000,
                "amount_max": 25000,
                "min_notional": 5000,
                "max_notional": 25000,
                "advertiser_reputation": 0.95,
                "available_notional": 20000,
            },
        ),
        "bybit": bot.Quote(
            "USDTARS",
            bid=1100.0,
            ask=900.0,
            ts=1,
            source="p2p",
            metadata={
                "fiat": "ARS",
                "payment_method": "CASH",
                "advertiser_reputation": 0.99,
                "amount_min": 1000,
                "amount_max": 50000,
            },
        ),
    }
    fees = {"binance": bot.VenueFees(venue="binance", default=bot.FeeSchedule(taker_fee_percent=0.1))}

    opps = bot.compute_spot_p2p_opportunities("USDT/ARS", spot_quotes, p2p_quotes, fees)

    assert opps
    assert all("bybit" not in (opp.buy_venue + opp.sell_venue) for opp in opps)
    assert all(opp.notes.get("payment_method") == "BANK_TRANSFER" for opp in opps)
    assert all(opp.notes.get("bank") == "Banco Uno" for opp in opps)
    assert all(opp.notes.get("executable_qty_real", 0) > 0 for opp in opps)


def test_http_get_json_uses_fallback_after_http_404_and_403(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.content = b"{}"

        def json(self):
            return self._payload

    responses = {
        "https://primary-404.example/api": FakeResponse(404, {}),
        "https://primary-403.example/api": FakeResponse(403, {}),
        "https://fallback.example/api": FakeResponse(200, {"ok": True}),
    }

    def fake_get(url, **kwargs):
        calls.append(url)
        return responses[url]

    monkeypatch.setattr(bot.requests, "get", fake_get)
    monkeypatch.setattr(bot.time, "sleep", lambda *_: None)
    monkeypatch.setattr(bot.random, "uniform", lambda *_: 0.0)

    resp_404 = bot.http_get_json(
        "https://primary-404.example/api",
        retries=1,
        fallback_endpoints=[("https://fallback.example/api", None)],
    )
    assert resp_404.data == {"ok": True}

    resp_403 = bot.http_get_json(
        "https://primary-403.example/api",
        retries=1,
        fallback_endpoints=[("https://fallback.example/api", None)],
    )
    assert resp_403.data == {"ok": True}

    assert calls == [
        "https://primary-404.example/api",
        "https://fallback.example/api",
        "https://primary-403.example/api",
        "https://fallback.example/api",
    ]


def test_http_get_json_uses_fallback_after_dns_like_error(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"bid": 100.0, "ask": 101.0}

    def fake_get(url, **kwargs):
        calls.append(url)
        if "primary" in url:
            raise bot.requests.exceptions.ConnectionError("Name or service not known")
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "get", fake_get)
    monkeypatch.setattr(bot.time, "sleep", lambda *_: None)
    monkeypatch.setattr(bot.random, "uniform", lambda *_: 0.0)

    response = bot.http_get_json(
        "https://primary.example/api",
        retries=1,
        fallback_endpoints=[("https://fallback.example/api", None)],
    )

    assert response.data == {"bid": 100.0, "ask": 101.0}
    assert calls == ["https://primary.example/api", "https://fallback.example/api"]
