import pytest

import arbitrage_telebot as bot


def test_ensure_telegram_startup_requirements_aborts_without_token(monkeypatch):
    events = []

    monkeypatch.setattr(bot, "get_bot_token", lambda: "")
    monkeypatch.setattr(bot, "log_event", lambda event, **payload: events.append((event, payload)))

    with pytest.raises(SystemExit) as exc_info:
        bot.ensure_telegram_startup_requirements("scanner", tg_enabled=True)

    assert exc_info.value.code == 1
    assert events
    event, payload = events[0]
    assert event == "telegram.startup.missing_token"
    assert payload["role"] == "scanner"


def test_ensure_telegram_startup_requirements_skips_when_telegram_disabled(monkeypatch):
    called = []

    monkeypatch.setattr(bot, "get_bot_token", lambda: "")
    monkeypatch.setattr(bot, "log_event", lambda event, **payload: called.append((event, payload)))

    bot.ensure_telegram_startup_requirements("scanner", tg_enabled=False)

    assert called == []


def test_ensure_telegram_startup_requirements_skips_when_role_not_telegram_related(monkeypatch):
    called = []

    monkeypatch.setattr(bot, "get_bot_token", lambda: "")
    monkeypatch.setattr(bot, "log_event", lambda event, **payload: called.append((event, payload)))

    bot.ensure_telegram_startup_requirements("api", tg_enabled=True)

    assert called == []
