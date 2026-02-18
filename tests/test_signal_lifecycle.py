import csv

import arbitrage_telebot as bot


def test_parse_manual_settlement_ok():
    parsed = bot.parse_manual_settlement("abc123 gano 12.5 cierre_manual")
    assert parsed is not None
    assert parsed["signal_id"] == "abc123"
    assert parsed["outcome"] == "win"
    assert parsed["pnl_real_quote"] == 12.5


def test_settle_signal_result_writes_execution_csv(tmp_path, monkeypatch):
    lifecycle = tmp_path / "signal_lifecycle.csv"
    results = tmp_path / "execution_results.csv"
    monkeypatch.setitem(bot.CONFIG, "signal_lifecycle_csv_path", str(lifecycle))
    monkeypatch.setitem(bot.CONFIG, "execution_results_csv_path", str(results))

    bot.SIGNAL_REGISTRY.clear()
    bot.SIGNAL_REGISTRY["sig01"] = {
        "pair": "BTC/USDT",
        "strategy": "spot_spot",
        "buy_venue": "binance",
        "sell_venue": "bybit",
        "est_profit_quote": 10.0,
        "capital_used_quote": 1000.0,
    }

    ok, message = bot.settle_signal_result(
        {"signal_id": "sig01", "outcome": "win", "pnl_real_quote": 8.0, "reason": "manual"}
    )

    assert ok
    assert "sig01" in message

    with open(results, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["signal_id"] == "sig01"
    assert rows[0]["delta_quote"] == "-2.000000"
