import arbitrage_telebot as bot


def test_check_account_limit_blocks_when_monthly_limit_is_exceeded(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setitem(
        bot.CONFIG,
        "account_limits",
        {
            "ledger_path": str(ledger_path),
            "venues": {
                "binance": {
                    "default": {
                        "monthly_fiat_limit": 1000.0,
                        "daily_payment_method_volume": {"SPOT": 5000.0},
                        "cooldown_seconds": 0,
                    }
                }
            },
        },
    )

    first_ok, first_reason, _ = bot.check_account_limit(
        "binance", fiat_amount=700.0, payment_method="SPOT", now_ts=1704067200, consume=True
    )
    second_ok, second_reason, second_details = bot.check_account_limit(
        "binance", fiat_amount=400.0, payment_method="SPOT", now_ts=1704067300, consume=False
    )

    assert first_ok is True
    assert first_reason is None
    assert second_ok is False
    assert second_reason == "account_limit"
    assert second_details["scope"] == "monthly"


def test_check_account_limit_reactivates_when_month_changes(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setitem(
        bot.CONFIG,
        "account_limits",
        {
            "ledger_path": str(ledger_path),
            "venues": {
                "binance": {
                    "default": {
                        "monthly_fiat_limit": 1000.0,
                        "daily_payment_method_volume": {"SPOT": 5000.0},
                        "cooldown_seconds": 0,
                    }
                }
            },
        },
    )

    jan_31 = 1706659200  # 2024-01-31 UTC
    feb_1 = 1706745600   # 2024-02-01 UTC

    bot.check_account_limit("binance", fiat_amount=900.0, payment_method="SPOT", now_ts=jan_31, consume=True)
    blocked, _, details_blocked = bot.check_account_limit(
        "binance", fiat_amount=200.0, payment_method="SPOT", now_ts=jan_31 + 100, consume=False
    )
    allowed_next_month, reason_next_month, _ = bot.check_account_limit(
        "binance", fiat_amount=200.0, payment_method="SPOT", now_ts=feb_1, consume=False
    )

    assert blocked is False
    assert details_blocked["scope"] == "monthly"
    assert allowed_next_month is True
    assert reason_next_month is None
