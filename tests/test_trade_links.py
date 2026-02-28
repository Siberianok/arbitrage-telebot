from copy import deepcopy

import arbitrage_telebot as bot


def test_build_trade_link_resolves_device_with_fallback():
    original = deepcopy(bot.CONFIG.get("venues", {}))
    try:
        bot.CONFIG.setdefault("venues", {})["binance"] = {
            "trade_links": {
                "default": "https://default.example/{pair}",
                "default_mobile": "https://m.example/{pair}",
                "BTC/USDT_desktop": "https://desktop.example/{base}-{quote}",
            }
        }

        assert (
            bot.build_trade_link("binance", "BTC/USDT", device="desktop")
            == "https://desktop.example/BTC-USDT"
        )
        assert (
            bot.build_trade_link("binance", "BTC/USDT", device="mobile")
            == "https://m.example/BTC/USDT"
        )
        assert (
            bot.build_trade_link("binance", "BTC/USDT", device="tablet")
            == "https://default.example/BTC/USDT"
        )
    finally:
        bot.CONFIG["venues"] = original


def test_build_trade_link_items_generic_only_returns_desktop_row():
    original = deepcopy(bot.CONFIG.get("venues", {}))
    try:
        bot.CONFIG["venues"] = {
            "binance": {"trade_links": {"default": "https://b.example/{pair}"}},
            "bybit": {"trade_links": {"default": "https://y.example/{pair}"}},
        }

        items = bot.build_trade_link_items("binance", "bybit", "BTC/USDT")
        assert len(items) == 2
        assert [item["device"] for item in items] == ["desktop", "desktop"]

        keyboard = bot.build_trade_links_inline_keyboard(items)
        assert keyboard == {
            "inline_keyboard": [
                [
                    {
                        "text": "Comprar (Binance · Desktop)",
                        "url": "https://b.example/BTC/USDT",
                    },
                    {
                        "text": "Vender (Bybit · Desktop)",
                        "url": "https://y.example/BTC/USDT",
                    },
                ]
            ]
        }
    finally:
        bot.CONFIG["venues"] = original


def test_build_trade_link_items_with_mobile_adds_second_row():
    original = deepcopy(bot.CONFIG.get("venues", {}))
    try:
        bot.CONFIG["venues"] = {
            "binance": {
                "trade_links": {
                    "default": "https://b.example/{pair}",
                    "default_mobile": "https://mb.example/{pair}",
                }
            },
            "bybit": {"trade_links": {"default": "https://y.example/{pair}"}},
        }

        items = bot.build_trade_link_items("binance", "bybit", "BTC/USDT")
        assert len(items) == 4
        assert [item["device"] for item in items] == [
            "desktop",
            "desktop",
            "mobile",
            "mobile",
        ]

        keyboard = bot.build_trade_links_inline_keyboard(items)
        assert keyboard == {
            "inline_keyboard": [
                [
                    {
                        "text": "Comprar (Binance · Desktop)",
                        "url": "https://b.example/BTC/USDT",
                    },
                    {
                        "text": "Vender (Bybit · Desktop)",
                        "url": "https://y.example/BTC/USDT",
                    },
                ],
                [
                    {
                        "text": "Comprar (Binance · Móvil)",
                        "url": "https://mb.example/BTC/USDT",
                    },
                    {
                        "text": "Vender (Bybit · Móvil)",
                        "url": "https://y.example/BTC/USDT",
                    },
                ],
            ]
        }
    finally:
        bot.CONFIG["venues"] = original
