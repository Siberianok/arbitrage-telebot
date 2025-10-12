import observability


def test_circuit_breaker_opens_and_resets(monkeypatch):
    observability.reset_all_states()

    exchange = "test_circuit"
    pair = "BTC/USDT"

    assert not observability.is_circuit_open(exchange)

    for _ in range(observability.CIRCUIT_FAILURE_THRESHOLD):
        observability.record_exchange_attempt(exchange, pair)
        observability.record_exchange_error(exchange, "boom", pair)

    assert observability.is_circuit_open(exchange)

    base_time = observability.time.time()

    monkeypatch.setattr(
        observability.time,
        "time",
        lambda: base_time + observability.CIRCUIT_COOLDOWN_SECONDS + 1,
    )

    assert not observability.is_circuit_open(exchange)


def test_reset_metrics_clears_counters():
    observability.reset_all_states()

    exchange = "metric_reset"
    pair = "ETH/USDT"

    observability.record_exchange_attempt(exchange, pair)
    observability.record_exchange_error(exchange, "err", pair)

    snapshot = observability.metrics_snapshot()
    assert snapshot[exchange]["attempts"] == 1
    assert snapshot[exchange]["errors"] == 1

    observability.reset_metrics([exchange])
    snapshot = observability.metrics_snapshot()
    assert snapshot[exchange]["attempts"] == 0
    assert snapshot[exchange]["errors"] == 0
