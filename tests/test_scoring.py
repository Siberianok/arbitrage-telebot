import time

import pytest

import arbitrage_telebot as bot
from arbitrage_telebot import (
    DepthInfo,
    FeeSchedule,
    Opportunity,
    Quote,
    TriangleLeg,
    TriangularRoute,
    VenueFees,
    build_degradation_alerts,
    classify_confidence,
    compute_liquidity_score,
    compute_priority_score,
    compute_triangular_opportunity,
)
from observability import reset_all_states


def make_depth(*, best_bid: float, best_ask: float, bid_volume: float, ask_volume: float, levels: int) -> DepthInfo:
    return DepthInfo(
        best_bid=best_bid,
        best_ask=best_ask,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        levels=levels,
        ts=int(time.time() * 1000),
        checksum="chk",
    )


def make_quote(symbol: str, bid: float, ask: float) -> Quote:
    return Quote(symbol=symbol, bid=bid, ask=ask, ts=int(time.time() * 1000))


def test_compute_liquidity_score_blends_depth_and_coverage():
    opp = Opportunity(
        pair="BTC/USDT",
        buy_venue="binance",
        sell_venue="okx",
        buy_price=30000.0,
        sell_price=30100.0,
        gross_percent=1.2,
        net_percent=1.0,
        buy_depth=make_depth(best_bid=29990.0, best_ask=30005.0, bid_volume=8.0, ask_volume=12.0, levels=20),
        sell_depth=make_depth(best_bid=30110.0, best_ask=30120.0, bid_volume=15.0, ask_volume=9.0, levels=18),
    )

    score = compute_liquidity_score(opp, required_base_qty=5.0)
    assert pytest.approx(0.985, rel=1e-3) == score


def test_classify_confidence_high_medium_low():
    high = classify_confidence(
        net_percent=1.0,
        threshold=0.6,
        liquidity_score=0.8,
        volatility_score=0.2,
        priority_score=0.9,
    )
    assert high == "alta"

    medium_priority = compute_priority_score(0.65, liquidity_score=0.5, volatility_score=0.2)
    medium = classify_confidence(
        net_percent=0.65,
        threshold=0.6,
        liquidity_score=0.5,
        volatility_score=0.2,
        priority_score=medium_priority,
    )
    assert medium == "media"

    low = classify_confidence(
        net_percent=0.4,
        threshold=0.6,
        liquidity_score=0.2,
        volatility_score=0.4,
        priority_score=0.3,
    )
    assert low == "baja"


def test_compute_triangular_opportunity_applies_fees():
    route = TriangularRoute(
        name="USDT-USDC-BUSD",
        venue="binance",
        start_asset="USDT",
        legs=[
            TriangleLeg(pair="USDT/USDC", action="SELL_BASE"),
            TriangleLeg(pair="USDC/BUSD", action="SELL_BASE"),
            TriangleLeg(pair="BUSD/USDT", action="SELL_BASE"),
        ],
    )

    quotes = {
        "USDT/USDC": {"binance": make_quote("USDTUSDC", bid=1.0, ask=1.0002)},
        "USDC/BUSD": {"binance": make_quote("USDCBUSD", bid=1.0, ask=1.0001)},
        "BUSD/USDT": {"binance": make_quote("BUSDUSDT", bid=1.01, ask=1.011)},
    }

    fees = {"binance": VenueFees(venue="binance", default=FeeSchedule(taker_fee_percent=0.1))}

    opportunity = compute_triangular_opportunity(route, quotes, fees, start_capital=100.0)
    assert opportunity is not None
    assert pytest.approx(1.0, rel=1e-6) == opportunity.gross_percent
    assert pytest.approx(0.6973, rel=1e-3) == opportunity.net_percent


def test_build_degradation_alerts_triggers_and_debounces():
    reset_all_states()
    snapshot = {
        "binance": {"attempts": 10, "errors": 6, "successes": 4, "no_data": 0},
        "kucoin": {"attempts": 0, "errors": 0, "successes": 0, "no_data": 0},
    }

    alerts_first = build_degradation_alerts(snapshot)
    assert any("binance" in alert for alert in alerts_first)
    assert any("kucoin" in alert for alert in alerts_first)

    alerts_second = build_degradation_alerts(snapshot)
    assert alerts_second == []



def test_compute_p2p_cross_opportunities_filters_low_capacity(monkeypatch):
    monkeypatch.setitem(bot.CONFIG, "simulation_capital_quote", 10_000)
    monkeypatch.setitem(
        bot.CONFIG,
        "p2p_execution",
        {"allowed_payment_methods": ["BANK_TRANSFER"], "min_advertiser_reputation": 0.8},
    )

    quotes = {
        "binance": Quote(
            "USDTARS",
            bid=1110.0,
            ask=1000.0,
            ts=1,
            source="p2p",
            metadata={
                "payment_method": "BANK_TRANSFER",
                "advertiser_reputation": 0.99,
                "amount_min": 1000,
                "amount_max": 2000,
                "max_notional": 2000,
            },
        ),
        "bybit": Quote(
            "USDTARS",
            bid=1200.0,
            ask=1020.0,
            ts=1,
            source="p2p",
            metadata={
                "payment_method": "BANK_TRANSFER",
                "advertiser_reputation": 0.95,
                "amount_min": 1000,
                "amount_max": 1500,
                "max_notional": 1500,
            },
        ),
    }

    opps = bot.compute_p2p_cross_opportunities("USDT/ARS", quotes)

    assert opps == []


def test_compute_p2p_cross_opportunities_populates_execution_notes(monkeypatch):
    monkeypatch.setitem(bot.CONFIG, "simulation_capital_quote", 1000)
    monkeypatch.setitem(
        bot.CONFIG,
        "p2p_execution",
        {"allowed_payment_methods": ["BANK_TRANSFER"], "min_advertiser_reputation": 0.8},
    )

    quotes = {
        "binance": Quote(
            "USDTARS",
            bid=1050.0,
            ask=1000.0,
            ts=1,
            source="p2p",
            metadata={
                "bank": "Banco A",
                "payment_method": "BANK_TRANSFER",
                "advertiser_reputation": 0.95,
                "amount_min": 100,
                "amount_max": 5000,
                "max_notional": 5000,
            },
        ),
        "bybit": Quote(
            "USDTARS",
            bid=1100.0,
            ask=1020.0,
            ts=1,
            source="p2p",
            metadata={
                "bank": "Banco B",
                "payment_method": "BANK_TRANSFER",
                "advertiser_reputation": 0.91,
                "amount_min": 100,
                "amount_max": 6000,
                "max_notional": 6000,
            },
        ),
    }

    opps = bot.compute_p2p_cross_opportunities("USDT/ARS", quotes)

    assert opps
    top = opps[0]
    assert top.notes["buy_payment_method"] == "BANK_TRANSFER"
    assert top.notes["sell_payment_method"] == "BANK_TRANSFER"
    assert top.notes["executable_qty_real"] > 0
