import arbitrage_telebot as bot


def test_emit_pair_coverage_logs_structured_event(monkeypatch):
    events = []

    def fake_log_event(event, **payload):
        events.append((event, payload))

    printed = []

    monkeypatch.setattr(bot, "log_event", fake_log_event)
    monkeypatch.setattr("builtins.print", lambda message: printed.append(message))

    bot.emit_pair_coverage("BTC/USDT", ["bybit", "binance"])

    assert events == [
        (
            "run.coverage",
            {
                "pair": "BTC/USDT",
                "venues": ["binance", "bybit"],
                "venues_count": 2,
            },
        )
    ]
    assert printed == ["[COVERAGE] BTC/USDT: ['binance', 'bybit']"]

