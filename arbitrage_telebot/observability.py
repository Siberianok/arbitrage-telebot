"""Observability helpers for arbitrage_telebot.

This module centralises structured logging, exchange metrics collection,
and circuit breaker management. The goal is to provide an easy integration
point for operational dashboards and alerting without forcing external
dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, Optional


class _JsonFormatter(logging.Formatter):
    """Formatter that serialises the log record as JSON."""

    def format(self, record: logging.LogRecord) -> str:  # pragma: no cover - small wrapper
        base = {
            "level": record.levelname,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "event": getattr(record, "event", record.msg if isinstance(record.msg, str) else "message"),
        }

        message_payload = {}
        if isinstance(record.msg, dict):
            message_payload = record.msg
        else:
            base["message"] = record.getMessage()

        base.update(message_payload)
        if record.args and not isinstance(record.msg, dict):
            base["args"] = record.args
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


_LOGGER = logging.getLogger("arbitrage_telebot")
_LOGGER.setLevel(logging.INFO)
if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    _LOGGER.addHandler(handler)
    _LOGGER.propagate = False


def log_event(event: str, **payload) -> None:
    """Emit a structured log entry."""

    payload = {"event": event, **payload}
    _LOGGER.info(payload)


@dataclass
class ExchangeMetrics:
    attempts: int = 0
    successes: int = 0
    errors: int = 0
    no_data: int = 0
    skips: int = 0
    last_error: Optional[str] = None
    last_success_ts: float = 0.0

    def error_rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return self.errors / float(self.attempts)


@dataclass
class CircuitBreaker:
    consecutive_failures: int = 0
    open_until: float = 0.0

    def is_open(self) -> bool:
        return time.time() < self.open_until


_METRICS_LOCK = threading.Lock()
_EXCHANGE_METRICS: Dict[str, ExchangeMetrics] = {}
_EXCHANGE_CIRCUITS: Dict[str, CircuitBreaker] = {}
_ALERT_STATE: Dict[str, float] = {}

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60
DEGRADATION_ALERT_COOLDOWN = 600
ERROR_RATE_ALERT_THRESHOLD = 0.5


def _get_metrics_locked(exchange: str) -> ExchangeMetrics:
    return _EXCHANGE_METRICS.setdefault(exchange, ExchangeMetrics())


def _get_circuit_locked(exchange: str) -> CircuitBreaker:
    return _EXCHANGE_CIRCUITS.setdefault(exchange, CircuitBreaker())


def record_exchange_attempt(exchange: str, pair: Optional[str] = None) -> None:
    with _METRICS_LOCK:
        metrics = _get_metrics_locked(exchange)
        metrics.attempts += 1
        attempts = metrics.attempts
    payload = {"exchange": exchange, "attempts": attempts}
    if pair:
        payload["pair"] = pair
    log_event("exchange.attempt", **payload)


def record_exchange_success(exchange: str, pair: Optional[str] = None) -> None:
    with _METRICS_LOCK:
        metrics = _get_metrics_locked(exchange)
        metrics.successes += 1
        metrics.last_success_ts = time.time()
        successes = metrics.successes
        circuit = _get_circuit_locked(exchange)
        circuit.consecutive_failures = 0
        consecutive_failures = circuit.consecutive_failures
    payload = {
        "exchange": exchange,
        "successes": successes,
        "consecutive_failures": consecutive_failures,
    }
    if pair:
        payload["pair"] = pair
    log_event("exchange.success", **payload)


def record_exchange_error(exchange: str, error: str, pair: Optional[str] = None) -> None:
    open_until = None
    with _METRICS_LOCK:
        metrics = _get_metrics_locked(exchange)
        metrics.errors += 1
        metrics.last_error = error
        circuit = _get_circuit_locked(exchange)
        circuit.consecutive_failures += 1
        consecutive_failures = circuit.consecutive_failures
        if circuit.consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            circuit.open_until = time.time() + CIRCUIT_COOLDOWN_SECONDS
            open_until = circuit.open_until
    if open_until is not None:
        log_event(
            "exchange.circuit_open",
            exchange=exchange,
            open_until=open_until,
            consecutive_failures=consecutive_failures,
        )
    payload = {
        "exchange": exchange,
        "error": error,
        "consecutive_failures": consecutive_failures,
    }
    if pair:
        payload["pair"] = pair
    log_event("exchange.error", **payload)


def record_exchange_no_data(exchange: str, pair: Optional[str] = None) -> None:
    with _METRICS_LOCK:
        metrics = _get_metrics_locked(exchange)
        metrics.no_data += 1
        no_data = metrics.no_data
    payload = {"exchange": exchange, "no_data": no_data}
    if pair:
        payload["pair"] = pair
    log_event("exchange.no_data", **payload)


def record_exchange_skip(exchange: str, reason: str, pair: Optional[str] = None) -> None:
    with _METRICS_LOCK:
        metrics = _get_metrics_locked(exchange)
        metrics.skips += 1
        skips = metrics.skips
    payload = {"exchange": exchange, "reason": reason, "skips": skips}
    if pair:
        payload["pair"] = pair
    log_event("exchange.skip", **payload)


def is_circuit_open(exchange: str) -> bool:
    should_log_reset = False
    with _METRICS_LOCK:
        circuit = _get_circuit_locked(exchange)
        is_open = circuit.is_open()
        if is_open:
            return True
        if circuit.open_until and not is_open:
            circuit.open_until = 0.0
            circuit.consecutive_failures = 0
            should_log_reset = True
    if should_log_reset:
        log_event("exchange.circuit_reset", exchange=exchange)
    return False


def metrics_snapshot() -> Dict[str, Dict]:
    with _METRICS_LOCK:
        return {name: asdict(metrics) for name, metrics in _EXCHANGE_METRICS.items()}


def register_degradation_alert(exchange: str, reason: str) -> bool:
    key = f"{exchange}:{reason}"
    now = time.time()
    with _METRICS_LOCK:
        last = _ALERT_STATE.get(key, 0)
        if now - last < DEGRADATION_ALERT_COOLDOWN:
            return False
        _ALERT_STATE[key] = now
    log_event("exchange.degradation", exchange=exchange, reason=reason)
    return True


def reset_metrics(exchanges: Iterable[str]) -> None:
    """Reset per-run counters while keeping long term stats."""

    with _METRICS_LOCK:
        for name in exchanges:
            metrics = _EXCHANGE_METRICS.setdefault(name, ExchangeMetrics())
            metrics.attempts = 0
            metrics.successes = 0
            metrics.errors = 0
            metrics.no_data = 0
            metrics.skips = 0


def reset_all_states() -> None:
    """Utility for tests: fully reset metrics, circuits, and alerts."""

    with _METRICS_LOCK:
        _EXCHANGE_METRICS.clear()
        _EXCHANGE_CIRCUITS.clear()
        _ALERT_STATE.clear()


__all__ = [
    "log_event",
    "metrics_snapshot",
    "record_exchange_attempt",
    "record_exchange_success",
    "record_exchange_error",
    "record_exchange_no_data",
    "record_exchange_skip",
    "register_degradation_alert",
    "is_circuit_open",
    "reset_metrics",
    "reset_all_states",
    "ERROR_RATE_ALERT_THRESHOLD",
]

