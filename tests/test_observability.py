import observability
import arbitrage_telebot as bot


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



def test_observability_concurrent_counter_updates_are_atomic():
    from concurrent.futures import ThreadPoolExecutor

    observability.reset_all_states()
    exchange = "atomic_metrics"

    def worker(_: int) -> None:
        observability.record_exchange_attempt(exchange)
        observability.record_exchange_error(exchange, "boom")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(200)))

    snapshot = observability.metrics_snapshot()[exchange]
    assert snapshot["attempts"] == 200
    assert snapshot["errors"] == 200


def test_concurrent_circuit_reads_do_not_break_state():
    from concurrent.futures import ThreadPoolExecutor

    observability.reset_all_states()
    exchange = "atomic_circuit"
    for _ in range(observability.CIRCUIT_FAILURE_THRESHOLD):
        observability.record_exchange_attempt(exchange)
        observability.record_exchange_error(exchange, "boom")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: observability.is_circuit_open(exchange), range(100)))

    assert all(results)


def test_fetch_all_quotes_opens_circuit_and_skips_failed_p2p_venue(monkeypatch):
    observability.reset_all_states()

    venue = "test_p2p"
    pair = "BTC/USDT"

    monkeypatch.setitem(
        bot.CONFIG,
        "venues",
        {
            venue: {
                "p2p": {
                    "enabled": True,
                    "pairs": {
                        pair: {
                            "endpoint": "https://example.invalid/quote",
                            "bid_path": "bid",
                            "ask_path": "ask",
                        }
                    },
                }
            }
        },
    )

    adapter = bot.GenericP2PMarketplace(venue)

    def _raise_transport_error(*args, **kwargs):
        raise bot.HttpError("transport down")

    monkeypatch.setattr(bot, "http_get_json", _raise_transport_error)

    for _ in range(observability.CIRCUIT_FAILURE_THRESHOLD):
        bot.fetch_all_quotes([pair], {venue: adapter})

    snapshot = observability.metrics_snapshot()[venue]
    assert snapshot["errors"] == observability.CIRCUIT_FAILURE_THRESHOLD
    assert snapshot["attempts"] == observability.CIRCUIT_FAILURE_THRESHOLD
    assert snapshot["skips"] == 0
    assert observability.is_circuit_open(venue)

    bot.fetch_all_quotes([pair], {venue: adapter})

    snapshot = observability.metrics_snapshot()[venue]
    assert snapshot["attempts"] == observability.CIRCUIT_FAILURE_THRESHOLD
    assert snapshot["errors"] == observability.CIRCUIT_FAILURE_THRESHOLD
    assert snapshot["skips"] == 1

