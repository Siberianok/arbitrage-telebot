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
        context_thresholds={"spot_spot|BTC/USDT|NA": 0.55},
        bucket_metrics={"spot_spot|BTC/USDT|NA": {"samples": 2.0, "hit_rate_real": 0.5}},
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
    assert analysis_state["context_thresholds"]["spot_spot|BTC/USDT|NA"] == pytest.approx(0.55)
    assert "bucket_metrics" in analysis_state
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


def test_resolve_threshold_for_context_uses_specific_strategy_pair(monkeypatch):
    analysis = bot.HistoricalAnalysis(
        rows_considered=3,
        success_rate=0.7,
        average_net_percent=0.8,
        average_effective_percent=0.6,
        recommended_threshold=0.4,
        pair_volatility={},
        max_volatility=0.0,
        backtest=bot.BacktestReport(),
        context_thresholds={
            "spot_spot|BTC/USDT|NA": 0.65,
            "spot_p2p|USDT/ARS|ARS": 0.42,
        },
    )
    monkeypatch.setattr(bot, "LATEST_ANALYSIS", analysis, raising=False)

    spot_ctx = bot.ThresholdContext(strategy="spot_spot", pair="BTC/USDT")
    p2p_ctx = bot.ThresholdContext(strategy="spot_p2p", pair="USDT/ARS", fiat="ARS")
    unknown_ctx = bot.ThresholdContext(strategy="spot_spot", pair="ETH/USDT")

    assert bot.resolve_threshold_for_context(spot_ctx, 0.3, 0.33) == pytest.approx(0.65)
    # piso inicial de 0.50% para rutas ARS/USDT
    assert bot.resolve_threshold_for_context(p2p_ctx, 0.3, 0.33) == pytest.approx(0.5)
    assert bot.resolve_threshold_for_context(unknown_ctx, 0.3, 0.33) == pytest.approx(0.33)


def test_compute_bucket_metrics_reports_hit_rate_and_drawdown():
    rows = [
        {
            "pair": "BTC/USDT",
            "strategy": "spot_spot",
            "fiat": "",
            "bucket": "spot_spot|BTC/USDT|NA",
            "net_%": "1.20",
            "effective_net_%": "0.90",
        },
        {
            "pair": "BTC/USDT",
            "strategy": "spot_spot",
            "fiat": "",
            "bucket": "spot_spot|BTC/USDT|NA",
            "net_%": "0.10",
            "effective_net_%": "-0.05",
        },
    ]

    metrics = bot.compute_bucket_metrics(rows, effective_by_index=[0.9, -0.05])
    bucket = metrics["spot_spot|BTC/USDT|NA"]

    assert bucket["samples"] == pytest.approx(2.0)
    assert bucket["hit_rate_real"] == pytest.approx(0.5)
    assert bucket["avg_slippage_drawdown_percent"] == pytest.approx(0.225)
