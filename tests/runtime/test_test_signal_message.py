from arbitrage_telebot import CONFIG, build_test_signal_message


def test_build_test_signal_message_uses_professional_intro(monkeypatch):
    monkeypatch.setitem(CONFIG, "simulation_capital_quote", 1000.0)

    message = build_test_signal_message()

    assert "🧪✨ *SEÑAL DE PRUEBA* ✨🧪" in message
    assert "📢 *Demo profesional del formato de alerta*" in message
    assert "⚠️ *No ejecutar* — mensaje solo para validación visual" in message
    assert "*Par:* `BTC/USDT`" in message
