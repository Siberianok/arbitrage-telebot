import arbitrage_telebot as bot


def test_resolve_keepalive_url_normalizes(monkeypatch):
    monkeypatch.setenv("KEEPALIVE_URL", "arbitrage-telebot-web.onrender.com")
    assert bot.resolve_keepalive_url() == "https://arbitrage-telebot-web.onrender.com/health"

    monkeypatch.setenv("KEEPALIVE_URL", "https://example.com/")
    assert bot.resolve_keepalive_url() == "https://example.com/health"

    monkeypatch.setenv("KEEPALIVE_URL", "https://example.com/health")
    assert bot.resolve_keepalive_url() == "https://example.com/health"


def test_ensure_keepalive_thread_skips_without_url(monkeypatch):
    events = []

    def fake_log(event, **payload):
        events.append((event, payload))

    monkeypatch.delenv("KEEPALIVE_URL", raising=False)
    monkeypatch.setenv("KEEPALIVE_ENABLED", "true")
    monkeypatch.setattr(bot, "log_event", fake_log)
    bot.KEEPALIVE_THREAD = None

    bot.ensure_keepalive_thread()

    assert any(event == "keepalive.skip" and payload.get("reason") == "missing_url" for event, payload in events)
