import arbitrage_telebot as bot
from arbitrage_telebot import CONFIG, build_test_signal_message


def test_build_test_signal_message_uses_professional_intro(monkeypatch):
    monkeypatch.setitem(CONFIG, "simulation_capital_quote", 1000.0)

    message = build_test_signal_message()

    assert "🧪✨ *SEÑAL DE PRUEBA* ✨🧪" in message
    assert "📢 *Demo profesional del formato de alerta*" in message
    assert "⚠️ *No ejecutar* — mensaje solo para validación visual" in message
    assert "*Par:* `BTC/USDT`" in message


def test_tg_handle_command_test_sends_inline_keyboard_when_links_exist(monkeypatch):
    payloads = []

    monkeypatch.setattr(bot, "register_telegram_chat", lambda _chat_id: None)
    monkeypatch.setattr(
        bot,
        "tg_send_message",
        lambda text, **kwargs: payloads.append({"text": text, **kwargs}),
    )

    bot.tg_handle_command("/test", "", "123", enabled=True)

    assert payloads
    assert payloads[0]["reply_markup"] is not None
    assert "inline_keyboard" in payloads[0]["reply_markup"]
    buttons = payloads[0]["reply_markup"]["inline_keyboard"]
    assert buttons[0][0]["text"] == "Comprar en Binance"
    assert buttons[1][0]["text"] == "Vender en Bybit"
    assert "*Acciones rápidas:* Comprar en Binance · Vender en Bybit" in payloads[0]["text"]
