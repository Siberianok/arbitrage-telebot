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

    def fake_api(method, params=None, http_method="get", request_timeout=None):
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

    def fake_api(method, params=None, http_method="get", request_timeout=None):
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




def test_tg_process_updates_uses_long_poll_http_timeout(monkeypatch):
    _setup_common(monkeypatch)

    calls = []

    def fake_api(method, params=None, http_method="get", request_timeout=None):
        calls.append((method, params, request_timeout))
        return {"ok": True, "result": []}

    monkeypatch.setattr(bot, "tg_api_request", fake_api)
    monkeypatch.setattr(bot, "log_event", lambda *args, **kwargs: None)

    bot.tg_process_updates(enabled=True)

    assert calls
    method, params, request_timeout = calls[0]
    assert method == "getUpdates"
    assert params["timeout"] == bot.TELEGRAM_POLL_TIMEOUT_SECONDS
    assert request_timeout > params["timeout"]
    assert request_timeout == params["timeout"] + bot.TELEGRAM_POLL_HTTP_TIMEOUT_GRACE_SECONDS


def test_tg_process_updates_logs_poll_timeout_as_non_critical(monkeypatch):
    _setup_common(monkeypatch)

    def fake_api(method, params=None, http_method="get", request_timeout=None):
        raise bot.HttpError("poll timeout", is_timeout=True)

    events = []

    def fake_log_event(event, **payload):
        events.append((event, payload))

    monkeypatch.setattr(bot, "tg_api_request", fake_api)
    monkeypatch.setattr(bot, "log_event", fake_log_event)

    bot.tg_process_updates(enabled=True)

    timeout_events = [payload for event, payload in events if event == "telegram.poll.timeout"]
    assert timeout_events
    assert timeout_events[0]["polling_timeout_seconds"] == bot.TELEGRAM_POLL_TIMEOUT_SECONDS
    assert timeout_events[0]["request_timeout_seconds"] > timeout_events[0]["polling_timeout_seconds"]
    assert not any(event == "telegram.poll.error" for event, _ in events)


def test_tg_handle_command_status_includes_minimum_gain_threshold(monkeypatch):
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
    assert any("Umbral mínimo de ganancia: 00,30% en adelante" in msg for msg in messages)
    assert any("0.550%" in msg for msg in messages)


def test_tg_sync_command_menu_registers_commands(monkeypatch):
    calls = []

    monkeypatch.setattr(bot, "get_bot_token", lambda: "token")
    monkeypatch.setattr(bot, "log_event", lambda *args, **kwargs: None)

    def fake_tg_api_request(method, params=None, http_method="get", request_timeout=None):
        calls.append((method, params, http_method))
        return {"ok": True}

    monkeypatch.setattr(bot, "tg_api_request", fake_tg_api_request)

    bot.tg_sync_command_menu(enabled=True)

    assert calls[0][0] == "deleteMyCommands"
    assert calls[1][0] == "setChatMenuButton"


def test_tg_handle_pending_input_cancel_restores_command_keyboard(monkeypatch):
    sent_payloads = []
    monkeypatch.setitem(bot.PENDING_CHAT_ACTIONS, "42", "delpair")
    monkeypatch.setattr(bot, "tg_send_message", lambda text, **payload: sent_payloads.append(payload))

    handled = bot.tg_handle_pending_input("42", "⬅️ Volver", enabled=True)

    assert handled is True
    assert bot.get_pending_action("42") is None
    assert sent_payloads[0]["reply_markup"] == bot.tg_commands_reply_markup()



def test_tg_handle_command_ping_returns_pong(monkeypatch):
    messages = []

    monkeypatch.setattr(bot, "tg_send_message", lambda text, **payload: messages.append(text))

    bot.tg_handle_command("/ping", "", "123", enabled=True)

    assert messages == ["pong"]


def test_build_health_payload_reports_telegram_polling_alive(monkeypatch):
    monkeypatch.setattr(bot, "PROCESS_ROLE", "telegram-worker")
    monkeypatch.setattr(bot, "metrics_snapshot", lambda: {})

    class _AliveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(bot, "TELEGRAM_POLLING_THREAD", _AliveThread())
    monkeypatch.setattr(bot, "SCANNER_LOOP_THREAD", None)
    monkeypatch.setattr(bot, "LAST_TELEGRAM_SEND_TS", 0, raising=False)
    monkeypatch.setattr(bot, "TELEGRAM_POLL_HEARTBEAT_TS", 0.0, raising=False)

    with bot.STATE_LOCK:
        bot.DASHBOARD_STATE["last_run_summary"] = {"ts": 1, "ts_str": "1970-01-01T00:00:01Z"}

    payload = bot.build_health_payload()

    assert payload["process"]["role"] == "telegram-worker"
    assert payload["process"]["checks"]["telegram_polling"]["required"] is True
    assert payload["process"]["checks"]["telegram_polling"]["alive"] is True
    assert payload["status"] == "ok"


def test_build_health_payload_marks_degraded_when_telegram_poll_stale(monkeypatch):
    monkeypatch.setattr(bot, "PROCESS_ROLE", "telegram-worker")
    monkeypatch.setattr(bot, "metrics_snapshot", lambda: {})
    monkeypatch.setattr(bot, "TELEGRAM_POLLING_THREAD", type("Alive", (), {"is_alive": lambda self: True})())
    monkeypatch.setattr(bot, "SCANNER_LOOP_THREAD", None)
    monkeypatch.setattr(bot, "LAST_TELEGRAM_SEND_TS", 0, raising=False)
    monkeypatch.setattr(bot, "TELEGRAM_POLL_HEARTBEAT_TS", 1.0, raising=False)

    class _FakeTime:
        def time(self):
            return 200.0

        def monotonic(self):
            return 200.0

    monkeypatch.setattr(bot, "time", _FakeTime())

    with bot.STATE_LOCK:
        bot.DASHBOARD_STATE["last_run_summary"] = {"ts": 150.0, "ts_str": "1970-01-01T00:02:30Z"}

    payload = bot.build_health_payload()

    assert payload["status"] == "degraded"
    assert payload["process"]["checks"]["telegram_polling"]["stale_seconds"] > 100


def test_health_status_code_returns_503_on_stale_live_check():
    payload = {
        "status": "ok",
        "process": {
            "checks": {
                "scanner_loop": {"required": False, "alive": True},
                "telegram_polling": {
                    "required": True,
                    "alive": True,
                    "stale_seconds": bot.TELEGRAM_POLL_TIMEOUT_SECONDS + bot.TELEGRAM_POLL_HTTP_TIMEOUT_GRACE_SECONDS + 60,
                },
            }
        },
    }

    assert bot.health_status_code("/live", payload) == 503
    assert bot.health_status_code("/health", payload) == 200
