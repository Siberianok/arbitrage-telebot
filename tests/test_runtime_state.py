from concurrent.futures import ThreadPoolExecutor

from runtime_state import RuntimeState


def test_runtime_state_parallel_updates_are_consistent():
    state = RuntimeState(max_alert_history=20)

    def worker(i: int) -> None:
        state.update_run_state(
            summary={"ts": i, "alerts_sent": i},
            exchange_health={"binance": {"attempts": i}},
            new_alerts=[{"ts": i, "message": f"alert-{i}"}],
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(100)))

    snapshot = state.dashboard_snapshot()
    assert snapshot["last_run_summary"]["ts"] == 99
    assert len(snapshot["latest_alerts"]) == 20
    assert snapshot["latest_alerts"][0]["ts"] == 99
    assert snapshot["latest_alerts"][-1]["ts"] == 80


def test_runtime_state_snapshots_are_immutable_copies():
    state = RuntimeState(max_alert_history=5)
    state.update_last_quote_state(
        quote_latency_ms=10,
        quote_count=1,
        latest_quotes={"BTC/USDT": {"binance": {"bid": 1}}},
    )

    snap = state.health_snapshot()
    snap["latest_quotes"]["BTC/USDT"]["binance"]["bid"] = 999

    snap2 = state.health_snapshot()
    assert snap2["latest_quotes"]["BTC/USDT"]["binance"]["bid"] == 1
