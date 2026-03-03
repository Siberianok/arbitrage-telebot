"""Thread-safe runtime state store for dashboard and polling consumers."""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, Iterable, List, Optional


class RuntimeState:
    """Encapsulates mutable runtime data behind a single lock."""

    def __init__(self, max_alert_history: int = 20):
        self._lock = threading.Lock()
        self._max_alert_history = max_alert_history
        self._last_run_summary: Optional[Dict[str, Any]] = None
        self._latest_alerts: List[Dict[str, Any]] = []
        self._exchange_health: Dict[str, Dict[str, Any]] = {}
        self._analysis: Optional[Dict[str, Any]] = None
        self._config_snapshot: Dict[str, Any] = {}
        self._latest_quotes: Dict[str, Dict[str, Any]] = {}
        self._last_quote_latency_ms: Optional[int] = None
        self._last_quote_count: int = 0

    def set_config_snapshot(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self._config_snapshot = copy.deepcopy(snapshot)

    def set_analysis(self, analysis: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._analysis = copy.deepcopy(analysis) if analysis is not None else None

    def update_last_quote_state(
        self,
        *,
        quote_latency_ms: int,
        quote_count: int,
        latest_quotes: Dict[str, Dict[str, Any]],
    ) -> None:
        with self._lock:
            self._last_quote_latency_ms = int(quote_latency_ms)
            self._last_quote_count = int(quote_count)
            self._latest_quotes = copy.deepcopy(latest_quotes)

    def update_run_state(
        self,
        *,
        summary: Dict[str, Any],
        exchange_health: Dict[str, Dict[str, Any]],
        new_alerts: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> None:
        with self._lock:
            self._last_run_summary = copy.deepcopy(summary)
            self._exchange_health = copy.deepcopy(exchange_health)
            if new_alerts:
                self._latest_alerts.extend(copy.deepcopy(list(new_alerts)))
                self._latest_alerts.sort(key=lambda item: item.get("ts", 0), reverse=True)
                self._latest_alerts = self._latest_alerts[: self._max_alert_history]

    def health_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_run_summary": copy.deepcopy(self._last_run_summary),
                "latest_alerts": copy.deepcopy(self._latest_alerts[:5]),
                "latest_quotes": copy.deepcopy(self._latest_quotes),
                "last_quote_latency_ms": self._last_quote_latency_ms,
                "last_quote_count": self._last_quote_count,
                "exchange_health": copy.deepcopy(self._exchange_health),
            }

    def dashboard_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_run_summary": copy.deepcopy(self._last_run_summary),
                "latest_alerts": copy.deepcopy(self._latest_alerts),
                "config_snapshot": copy.deepcopy(self._config_snapshot),
                "exchange_metrics": copy.deepcopy(self._exchange_health),
                "analysis": copy.deepcopy(self._analysis),
            }


__all__ = ["RuntimeState"]
