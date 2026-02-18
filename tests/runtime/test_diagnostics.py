import time
from typing import Optional

import pytest

from arbitrage_telebot import ExchangeAdapter, Quote, diagnose_exchange_pairs


class _BaseAdapter(ExchangeAdapter):
    depth_supported = False

    def normalize_symbol(self, pair: str) -> str:
        return pair


class _GoodAdapter(_BaseAdapter):
    name = "good"

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        return Quote(pair, 1.0, 2.0, int(time.time() * 1000), source="live")


class _OfflineAdapter(_BaseAdapter):
    name = "offline"

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        return Quote(pair, 0.5, 0.6, int(time.time() * 1000), source="offline")


class _NoDataAdapter(_BaseAdapter):
    name = "nodata"

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        return None


class _ErrorAdapter(_BaseAdapter):
    name = "error"

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        raise RuntimeError("boom")


class _InvalidQuoteAdapter(_BaseAdapter):
    name = "invalid"

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        return Quote(pair, 1.0, 0.5, int(time.time() * 1000), source="live")


def test_diagnose_exchange_pairs_returns_statuses():
    adapters = {
        "good": _GoodAdapter(),
        "offline": _OfflineAdapter(),
        "nodata": _NoDataAdapter(),
        "error": _ErrorAdapter(),
        "invalid": _InvalidQuoteAdapter(),
    }

    results = diagnose_exchange_pairs(["BTC/USDT"], adapters, max_workers=1)

    assert len(results) == 5
    results_by_venue = {item["venue"]: item for item in results}

    assert results_by_venue["good"]["status"] == "ok"
    assert pytest.approx(results_by_venue["good"]["bid"], rel=1e-9) == 1.0
    assert not results_by_venue["good"].get("offline_source")

    assert results_by_venue["offline"]["status"] == "ok"
    assert results_by_venue["offline"].get("offline_source") is True

    assert results_by_venue["nodata"]["status"] == "no_data"

    assert results_by_venue["error"]["status"] == "error"
    assert "RuntimeError" in results_by_venue["error"].get("error", "")

    assert results_by_venue["invalid"]["status"] == "error"
    assert "InvalidQuote" in results_by_venue["invalid"].get("error", "")

    for item in results:
        assert item["latency_ms"] >= 0.0


def test_diagnose_exchange_pairs_handles_empty_inputs():
    assert diagnose_exchange_pairs([], {}) == []
