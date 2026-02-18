import pytest
import arbitrage_telebot as bot


def test_update_analysis_state_updates_globals(monkeypatch):
    analysis = bot.HistoricalAnalysis(
        rows_considered=5,
        success_rate=0.62,
        average_net_percent=0.45,
        average_effective_percent=0.41,
        recommended_threshold=0.42,
        pair_volatility={},
        max_volatility=0.0,
        backtest=bot.BacktestReport(),
    )

    events = []

    monkeypatch.setattr(
        bot,
        "analyze_historical_performance",
        lambda path, capital: analysis,
    )
    monkeypatch.setattr(bot, "log_event", lambda event, **payload: events.append((event, payload)))

    monkeypatch.setattr(bot, "DYNAMIC_THRESHOLD_PERCENT", 0.30, raising=False)
    monkeypatch.setattr(bot, "LATEST_ANALYSIS", None, raising=False)
    with bot.STATE_LOCK:
        bot.DASHBOARD_STATE["analysis"] = None

    bot.update_analysis_state(5000.0, "logs/opportunities.csv")

    assert bot.LATEST_ANALYSIS is analysis
    assert bot.DYNAMIC_THRESHOLD_PERCENT == pytest.approx(0.42)

    with bot.STATE_LOCK:
        analysis_state = bot.DASHBOARD_STATE["analysis"]

    assert analysis_state["recommended_threshold"] == pytest.approx(0.42)
    assert analysis_state["rows_considered"] == 5
    assert any(event == "analysis.updated" for event, _ in events)


def test_update_analysis_state_handles_failures(monkeypatch):
    def failing_analysis(path, capital):
        raise RuntimeError("boom")

    events = []
    monkeypatch.setattr(bot, "analyze_historical_performance", failing_analysis)
    monkeypatch.setattr(bot, "log_event", lambda event, **payload: events.append((event, payload)))

    monkeypatch.setattr(bot, "DYNAMIC_THRESHOLD_PERCENT", 0.28, raising=False)
    bot.update_analysis_state(2500.0, "logs/opportunities.csv")

    assert bot.DYNAMIC_THRESHOLD_PERCENT == 0.28
    assert any(event == "analysis.error" for event, _ in events)
