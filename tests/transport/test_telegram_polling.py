import arbitrage_telebot as bot


class _FixedTime:
    def __init__(self, value: float):
        self._value = value

    def monotonic(self) -> float:
        return self._value


def _setup_common(monkeypatch, monotonic_value: float = 100.0):
    fake_time = _FixedTime(monotonic_value)
    monkeypatch.setattr(bot, "time", fake_time)
    monkeypatch.setattr(bot, "get_bot_token", lambda: "token")
    bot.TELEGRAM_LAST_UPDATE_ID = 0
    bot.TELEGRAM_POLL_BACKOFF_UNTIL = 0.0
    bot.TELEGRAM_LAST_WEBHOOK_RESET_TS = 0.0
    return fake_time


def test_tg_process_updates_resets_webhook_on_conflict(monkeypatch):
    _setup_common(monkeypatch)

    methods = []

    def fake_api(method, params=None, http_method="get"):
        if method == "getUpdates":
            raise bot.HttpError("HTTP 409 -> conflict", status_code=409)
        methods.append(method)
        return {"ok": True}

    events = []

    def fake_log_event(event, **payload):
        events.append((event, payload))

    monkeypatch.setattr(bot, "tg_api_request", fake_api)
    monkeypatch.setattr(bot, "log_event", fake_log_event)

    bot.tg_process_updates(enabled=True)

    assert methods == ["deleteWebhook"]
    conflict_events = [payload for event, payload in events if event == "telegram.poll.conflict"]
    assert conflict_events and conflict_events[0]["backoff_seconds"] == bot.TELEGRAM_POLL_CONFLICT_BACKOFF_SECONDS
    assert any(event == "telegram.poll.reset_webhook.success" for event, _ in events)
    assert bot.TELEGRAM_POLL_BACKOFF_UNTIL == bot.time.monotonic() + bot.TELEGRAM_POLL_CONFLICT_BACKOFF_SECONDS
    assert bot.TELEGRAM_LAST_WEBHOOK_RESET_TS == bot.time.monotonic()


def test_tg_process_updates_skips_webhook_reset_during_cooldown(monkeypatch):
    fake_time = _setup_common(monkeypatch, monotonic_value=200.0)

    monkeypatch.setattr(bot, "TELEGRAM_WEBHOOK_RESET_COOLDOWN_SECONDS", 300.0, raising=False)
    bot.TELEGRAM_LAST_WEBHOOK_RESET_TS = 100.0

    def fake_api(method, params=None, http_method="get"):
        if method == "getUpdates":
            raise bot.HttpError("HTTP 409 -> conflict", status_code=409)
        raise AssertionError("deleteWebhook should not be called during cooldown")

    events = []

    def fake_log_event(event, **payload):
        events.append((event, payload))

    monkeypatch.setattr(bot, "tg_api_request", fake_api)
    monkeypatch.setattr(bot, "log_event", fake_log_event)

    bot.tg_process_updates(enabled=True)

    assert any(event == "telegram.poll.reset_webhook.skip" for event, _ in events)
    assert bot.TELEGRAM_POLL_BACKOFF_UNTIL == fake_time.monotonic() + bot.TELEGRAM_POLL_CONFLICT_BACKOFF_SECONDS
    assert bot.TELEGRAM_LAST_WEBHOOK_RESET_TS == 100.0


def test_tg_handle_command_status_includes_dynamic_threshold(monkeypatch):
    messages = []

    monkeypatch.setattr(bot, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "tg_enable_menu_button", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "tg_send_message", lambda text, **payload: messages.append(text))
    monkeypatch.setattr(bot, "register_telegram_chat", lambda chat_id: str(chat_id))
    monkeypatch.setattr(bot, "get_registered_chat_ids", lambda: ["123"])

    monkeypatch.setitem(bot.CONFIG, "pairs", ["BTC/USDT"])
    bot.TELEGRAM_CHAT_IDS = {"123"}
    bot.CONFIG["threshold_percent"] = 0.3
    bot.DYNAMIC_THRESHOLD_PERCENT = 0.55

    bot.tg_handle_command("/status", "", "123", enabled=True)

    assert messages
    assert any("0.550%" in msg for msg in messages)



def test_tg_handle_command_threshold_read_and_update_valid(monkeypatch):
    messages = []
    events = []

    monkeypatch.setattr(bot, "tg_enable_menu_button", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "tg_send_message", lambda text, **payload: messages.append(text))
    monkeypatch.setattr(bot, "register_telegram_chat", lambda chat_id: str(chat_id))
    monkeypatch.setattr(bot, "refresh_config_snapshot", lambda: None)
    monkeypatch.setattr(bot, "log_event", lambda event, **payload: events.append((event, payload)))

    monkeypatch.setitem(bot.CONFIG, "analysis", {"min_threshold_percent": 0.1, "max_threshold_percent": 1.0})
    bot.CONFIG["threshold_percent"] = 0.3
    bot.TELEGRAM_ADMIN_IDS = {"123"}

    bot.tg_handle_command("/threshold", "", "123", enabled=True)
    bot.tg_handle_command("/threshold", "0.45", "123", enabled=True)

    assert any("Threshold actual" in msg for msg in messages)
    assert any("0,300" in msg for msg in messages)
    assert bot.CONFIG["threshold_percent"] == 0.45
    threshold_events = [payload for event, payload in events if event == "telegram.threshold.updated"]
    assert threshold_events
    assert threshold_events[0]["chat_id"] == "123"
    assert threshold_events[0]["previous_threshold"] == 0.3
    assert threshold_events[0]["new_threshold"] == 0.45


def test_tg_handle_command_threshold_rejects_invalid_value(monkeypatch):
    messages = []

    monkeypatch.setattr(bot, "tg_enable_menu_button", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "tg_send_message", lambda text, **payload: messages.append(text))
    monkeypatch.setattr(bot, "register_telegram_chat", lambda chat_id: str(chat_id))
    monkeypatch.setattr(bot, "refresh_config_snapshot", lambda: None)
    monkeypatch.setattr(bot, "log_event", lambda *args, **kwargs: None)

    monkeypatch.setitem(bot.CONFIG, "analysis", {"min_threshold_percent": 0.1, "max_threshold_percent": 1.0})
    bot.CONFIG["threshold_percent"] = 0.3
    bot.TELEGRAM_ADMIN_IDS = {"123"}

    bot.tg_handle_command("/threshold", "9", "123", enabled=True)

    assert bot.CONFIG["threshold_percent"] == 0.3
    assert any("fuera de rango" in msg for msg in messages)


def test_tg_handle_command_threshold_requires_admin(monkeypatch):
    messages = []

    monkeypatch.setattr(bot, "tg_enable_menu_button", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "tg_send_message", lambda text, **payload: messages.append(text))
    monkeypatch.setattr(bot, "register_telegram_chat", lambda chat_id: str(chat_id))
    monkeypatch.setattr(bot, "refresh_config_snapshot", lambda: None)
    monkeypatch.setattr(bot, "log_event", lambda *args, **kwargs: None)

    monkeypatch.setitem(bot.CONFIG, "analysis", {"min_threshold_percent": 0.1, "max_threshold_percent": 1.0})
    bot.CONFIG["threshold_percent"] = 0.3
    bot.TELEGRAM_ADMIN_IDS = {"999"}

    bot.tg_handle_command("/threshold", "0.4", "123", enabled=True)

    assert bot.CONFIG["threshold_percent"] == 0.3
    assert any("requiere privilegios" in msg for msg in messages)
