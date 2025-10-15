#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import csv
import hashlib
import itertools
import json
import math
import os
import random
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from statistics import StatisticsError, mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Type, Union

import requests

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Gauge,
        generate_latest,
    )
except Exception:  # pragma: no cover - fallback when prometheus_client is unavailable
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class CollectorRegistry:  # type: ignore
        def __init__(self):
            self.metrics: List["_Gauge"] = []

        def register(self, metric: "_Gauge") -> None:
            self.metrics.append(metric)

        def collect(self) -> List["_Gauge"]:
            return list(self.metrics)

    class _GaugeChild:
        def __init__(self, parent: "_Gauge", labels: Tuple[Tuple[str, str], ...]):
            self.parent = parent
            self.labels = labels

        def set(self, value: float) -> None:
            self.parent.samples[self.labels] = float(value)

    class _Gauge:
        def __init__(
            self,
            name: str,
            documentation: str,
            labelnames: Optional[List[str]] = None,
            registry: Optional[CollectorRegistry] = None,
        ):
            self.name = name
            self.documentation = documentation
            self.labelnames = tuple(labelnames or [])
            self.samples: Dict[Tuple[Tuple[str, str], ...], float] = {}
            if registry is not None:
                registry.register(self)

        def labels(self, **kwargs: str) -> _GaugeChild:
            labels = tuple((label, str(kwargs.get(label, ""))) for label in self.labelnames)
            return _GaugeChild(self, labels)

        def set(self, value: float) -> None:
            self.samples[tuple()] = float(value)

    class Gauge(_Gauge):  # type: ignore
        def __init__(self, name: str, documentation: str, labelnames: Optional[List[str]] = None, registry: Optional[CollectorRegistry] = None):
            super().__init__(name, documentation, labelnames=labelnames, registry=registry)

    def generate_latest(registry: CollectorRegistry) -> bytes:
        lines: List[str] = []
        for metric in registry.collect():
            lines.append(f"# HELP {metric.name} {metric.documentation}")
            lines.append(f"# TYPE {metric.name} gauge")
            for labels, value in metric.samples.items():
                if labels:
                    label_str = ",".join(f"{k}=\"{v}\"" for k, v in labels)
                    lines.append(f"{metric.name}{{{label_str}}} {value}")
                else:
                    lines.append(f"{metric.name} {value}")
        return "\n".join(lines).encode("utf-8")

from observability import (
    ERROR_RATE_ALERT_THRESHOLD,
    is_circuit_open,
    log_event,
    metrics_snapshot,
    record_exchange_attempt,
    record_exchange_error,
    record_exchange_no_data,
    record_exchange_skip,
    record_exchange_success,
    register_degradation_alert,
    reset_metrics,
)


LOG_BASE_DIR = os.getenv("LOG_BASE_DIR", "logs")
LOG_BACKUP_DIR = os.getenv("LOG_BACKUP_DIR", "log_backups")
DEFAULT_QUOTE_WORKERS = int(os.getenv("QUOTE_WORKERS", "16"))
DEFAULT_QUOTE_ASSET = os.getenv("DEFAULT_QUOTE_ASSET", "USDT").strip().upper() or "USDT"

PROM_REGISTRY = CollectorRegistry()
PROM_LAST_RUN_TS = Gauge(
    "arbitrage_last_run_timestamp",
    "Timestamp of the last successful scanning cycle",
    registry=PROM_REGISTRY,
)
PROM_LAST_RUN_LATENCY_MS = Gauge(
    "arbitrage_last_run_latency_ms",
    "Execution time in milliseconds of the last scanning cycle",
    registry=PROM_REGISTRY,
)
PROM_ALERTS_SENT = Gauge(
    "arbitrage_alerts_sent_total",
    "Number of alerts emitted in the last scanning cycle",
    registry=PROM_REGISTRY,
)
PROM_TRIANGULAR_ALERTS = Gauge(
    "arbitrage_triangular_alerts_sent_total",
    "Number of triangular alerts emitted in the last scanning cycle",
    registry=PROM_REGISTRY,
)
PROM_EXCHANGE_ATTEMPTS = Gauge(
    "arbitrage_exchange_attempts",
    "Requests attempted per exchange in the last cycle",
    ["exchange"],
    registry=PROM_REGISTRY,
)
PROM_EXCHANGE_ERRORS = Gauge(
    "arbitrage_exchange_errors",
    "Errors observed per exchange in the last cycle",
    ["exchange"],
    registry=PROM_REGISTRY,
)


def emit_pair_coverage(pair: str, venues: Iterable[str]) -> None:
    """Report venue coverage for a trading pair via structured logs."""

    venues_list = sorted(venues)
    log_event(
        "run.coverage",
        pair=pair,
        venues=venues_list,
        venues_count=len(venues_list),
    )
    print(f"[COVERAGE] {pair}: {venues_list}")

# =========================
# CONFIG
# =========================
CONFIG = {
    "threshold_percent": 0.30,      # alerta si neto >= 0.30%
    "pairs": [
        # En modo de prueba solo consideramos los activos solicitados
        "BTC/USDT",
        "ETH/USDT",
        "XRP/USDT",
        "SOL/USDT",
    ],
    "simulation_capital_quote": 10_000,  # capital (USDT) para estimar PnL en alerta
    "max_quote_age_seconds": 12,  # descarta cotizaciones más viejas que este límite
    "capital_weights": {
        "pairs": {
            "default": 1.0,
            "BTC/USDT": 1.5,
            "ETH/USDT": 1.2,
            "XRP/USDT": 1.1,
            "SOL/USDT": 1.1,
        },
        "triangles": {
            "default": 0.6,
        },
    },
    "strategies": {
        "spot_spot": True,
        "spot_p2p": True,
        "p2p_p2p": True,
        "triangular_intra_venue": True,
    },
    "offline_quotes": {
        # Valores de respaldo en caso de faltar la configuración de pruebas
        "BTC/USDT": {"bid": 30050.0, "ask": 30060.0},
        "ETH/USDT": {"bid": 1800.0, "ask": 1801.5},
        "XRP/USDT": {"bid": 0.52, "ask": 0.521},
        "SOL/USDT": {"bid": 22.4, "ask": 22.45},
    },
    "test_mode": {
        "enabled": False,
        "pause_live_requests": False,
        "venues": {
            "binance": {
                "pairs": {
                    "BTC/USDT": {
                        "bid": 30050.5,
                        "ask": 30055.0,
                        "source": "spot-test",
                    },
                    "ETH/USDT": {
                        "bid": 1798.5,
                        "ask": 1800.0,
                        "source": "spot-test",
                    },
                    "XRP/USDT": {
                        "bid": 0.519,
                        "ask": 0.520,
                        "source": "spot-test",
                    },
                    "SOL/USDT": {
                        "bid": 22.35,
                        "ask": 22.4,
                        "source": "spot-test",
                    },
                }
            },
            "bybit": {
                "pairs": {
                    "BTC/USDT": {
                        "bid": 30062.0,
                        "ask": 30066.5,
                        "source": "spot-test",
                    },
                    "ETH/USDT": {
                        "bid": 1799.0,
                        "ask": 1800.6,
                        "source": "spot-test",
                    },
                    "XRP/USDT": {
                        "bid": 0.521,
                        "ask": 0.522,
                        "source": "spot-test",
                    },
                    "SOL/USDT": {
                        "bid": 22.5,
                        "ask": 22.58,
                        "source": "spot-test",
                    },
                }
            },
        },
    },
    "venues": {
        "binance": {
            "enabled": True,
            "taker_fee_percent": 0.10,
            "fees": {
                "default": {
                    "taker": 0.10,
                    "maker": 0.08,
                    "slippage_bps": 1.0,
                    "native_token_discount_percent": 0.025,
                },
                "per_pair": {
                    "BTC/USDT": {"taker": 0.10, "slippage_bps": 0.8},
                    "ETH/USDT": {"taker": 0.10, "slippage_bps": 1.2},
                    "XRP/USDT": {"taker": 0.10, "slippage_bps": 2.0},
                    "SOL/USDT": {"taker": 0.10, "slippage_bps": 1.5},
                },
                "vip_level": "VIP0",
                "vip_multipliers": {
                    "default": 1.0,
                    "VIP0": 1.0,
                    "VIP1": 0.95,
                    "VIP2": 0.90,
                },
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                "fallbacks": [
                    "https://p2p.binance.com/bapi/c2c/v2/public/c2c/adv/search",
                ],
                "rows": 10,
                "merchant_types": [],
                "fees": {
                    "default_percent": 0.80,
                    "per_asset_percent": {
                        "BTC": 1.00,
                        "ETH": 0.95,
                        "XRP": 0.90,
                        "USDT": 0.70,
                        "SOL": 0.90,
                    },
                },
                "min_notional_usdt": {
                    "BTC": 200.0,
                    "ETH": 150.0,
                    "XRP": 80.0,
                    "USDT": 50.0,
                    "SOL": 60.0,
                },
                "payment_methods": ["BANK_TRANSFER"],
                "pairs": {
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "pay_types": [],
                    },
                    "BTC/USD": {
                        "asset": "BTC",
                        "fiat": "USD",
                        "pay_types": [],
                    },
                    "ETH/USD": {
                        "asset": "ETH",
                        "fiat": "USD",
                        "pay_types": [],
                    },
                    "XRP/USD": {
                        "asset": "XRP",
                        "fiat": "USD",
                        "pay_types": [],
                    },
                    "SOL/USD": {
                        "asset": "SOL",
                        "fiat": "USD",
                        "pay_types": [],
                    },
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "pay_types": [],
                    },
                    "BTC/ARS": {
                        "asset": "BTC",
                        "fiat": "ARS",
                        "pay_types": [],
                    },
                    "ETH/ARS": {
                        "asset": "ETH",
                        "fiat": "ARS",
                        "pay_types": [],
                    },
                    "XRP/ARS": {
                        "asset": "XRP",
                        "fiat": "ARS",
                        "pay_types": [],
                    },
                    "SOL/ARS": {
                        "asset": "SOL",
                        "fiat": "ARS",
                        "pay_types": [],
                    },
                },
            },
            "transfers": {
                "BTC": {
                    "withdraw_fee": 0.0004,
                    "withdraw_minutes": 30,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 10,
                },
                "ETH": {
                    "withdraw_fee": 0.002,
                    "withdraw_minutes": 10,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 5,
                },
                "USDT": {
                    "withdraw_fee": 1.0,
                    "withdraw_minutes": 15,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 5,
                },
                "SOL": {
                    "withdraw_fee": 0.01,
                    "withdraw_minutes": 12,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 5,
                },
            },
            "endpoints": {
                "ticker": {
                    "primary": "https://api.binance.com/api/v3/ticker/bookTicker",
                    "fallbacks": [
                        "https://api1.binance.com/api/v3/ticker/bookTicker",
                        "https://api2.binance.com/api/v3/ticker/bookTicker",
                    ],
                },
                "depth": {
                    "primary": "https://api.binance.com/api/v3/depth",
                    "fallbacks": [
                        "https://api1.binance.com/api/v3/depth",
                        "https://api2.binance.com/api/v3/depth",
                    ],
                },
            },
        },
        "bybit": {
            "enabled": True,
            "taker_fee_percent": 0.10,
            "fees": {
                "default": {
                    "taker": 0.10,
                    "maker": 0.10,
                    "slippage_bps": 1.5,
                },
                "per_pair": {
                    "ETH/USDT": {"taker": 0.10, "slippage_bps": 1.5},
                    "XRP/USDT": {"taker": 0.10, "slippage_bps": 2.5},
                    "SOL/USDT": {"taker": 0.10, "slippage_bps": 1.8},
                },
                "vip_level": "VIP0",
                "vip_multipliers": {
                    "default": 1.0,
                    "VIP1": 0.97,
                    "VIP2": 0.93,
                },
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://api2.bybit.com/fiat/otc/item/online",
                "rows": 10,
                "fees": {
                    "default_percent": 0.95,
                    "per_asset_percent": {
                        "BTC": 1.10,
                        "ETH": 1.00,
                        "USDT": 0.75,
                        "XRP": 0.95,
                        "SOL": 0.98,
                    },
                },
                "min_notional_usdt": {
                    "BTC": 180.0,
                    "ETH": 120.0,
                    "USDT": 40.0,
                    "XRP": 60.0,
                    "SOL": 55.0,
                },
                "payment_methods": ["BANK_TRANSFER"],
                "pairs": {
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "BTC/USD": {
                        "asset": "BTC",
                        "fiat": "USD",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "ETH/USD": {
                        "asset": "ETH",
                        "fiat": "USD",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "XRP/USD": {
                        "asset": "XRP",
                        "fiat": "USD",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "SOL/USD": {
                        "asset": "SOL",
                        "fiat": "USD",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "BTC/ARS": {
                        "asset": "BTC",
                        "fiat": "ARS",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "ETH/ARS": {
                        "asset": "ETH",
                        "fiat": "ARS",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "XRP/ARS": {
                        "asset": "XRP",
                        "fiat": "ARS",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                    "SOL/ARS": {
                        "asset": "SOL",
                        "fiat": "ARS",
                        "ask_side": "1",
                        "bid_side": "0",
                    },
                },
            },
            "transfers": {
                "BTC": {
                    "withdraw_fee": 0.0005,
                    "withdraw_minutes": 35,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 15,
                },
                "ETH": {
                    "withdraw_fee": 0.0025,
                    "withdraw_minutes": 12,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 6,
                },
                "USDT": {
                    "withdraw_fee": 1.5,
                    "withdraw_minutes": 20,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 8,
                },
                "SOL": {
                    "withdraw_fee": 0.012,
                    "withdraw_minutes": 18,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 7,
                },
            },
            "endpoints": {
                "ticker": {
                    "primary": "https://api.bybit.com/v5/market/tickers",
                    "fallbacks": [
                        "https://api2.bybit.com/v5/market/tickers",
                        "https://api.bytick.com/v5/market/tickers",
                    ],
                },
                "depth": {
                    "primary": "https://api.bybit.com/v5/market/orderbook",
                    "fallbacks": [
                        "https://api2.bybit.com/v5/market/orderbook",
                        "https://api.bytick.com/v5/market/orderbook",
                    ],
                },
            },
        },
        "feewin": {
            "enabled": True,
            "adapter": "generic_p2p",
            "taker_fee_percent": 0.20,
            "fees": {
                "default": {
                    "taker": 0.20,
                    "maker": 0.20,
                    "slippage_bps": 15.0,
                }
            },
            "trade_links": {
                "default": "https://feewin.com/otc?asset={base}&fiat={quote}",
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://api.feewin.com/v1/otc/rates",
                "method": "GET",
                "data_path": ["data"],
                "bid_path": ["{asset}", "{fiat}", "sell"],
                "ask_path": ["{asset}", "{fiat}", "buy"],
                "invert_sides": True,
                "pairs": {
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "static_quote": {"bid": 565.0, "ask": 575.0},
                    },
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.995, "ask": 1.005},
                    },
                    "BTC/ARS": {
                        "asset": "BTC",
                        "fiat": "ARS",
                        "static_quote": {"bid": 16200000.0, "ask": 16650000.0},
                    },
                    "BTC/USD": {
                        "asset": "BTC",
                        "fiat": "USD",
                        "static_quote": {"bid": 30100.0, "ask": 30400.0},
                    },
                    "ETH/ARS": {
                        "asset": "ETH",
                        "fiat": "ARS",
                        "static_quote": {"bid": 1025000.0, "ask": 1055000.0},
                    },
                    "ETH/USD": {
                        "asset": "ETH",
                        "fiat": "USD",
                        "static_quote": {"bid": 1825.0, "ask": 1845.0},
                    },
                    "XRP/ARS": {
                        "asset": "XRP",
                        "fiat": "ARS",
                        "static_quote": {"bid": 285.0, "ask": 296.0},
                    },
                    "XRP/USD": {
                        "asset": "XRP",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.53, "ask": 0.55},
                    },
                    "SOL/ARS": {
                        "asset": "SOL",
                        "fiat": "ARS",
                        "static_quote": {"bid": 12500.0, "ask": 13000.0},
                    },
                    "SOL/USD": {
                        "asset": "SOL",
                        "fiat": "USD",
                        "static_quote": {"bid": 22.6, "ask": 23.0},
                    },
                },
            },
            "transfers": {
                "USDT": {
                    "withdraw_fee": 1.0,
                    "withdraw_minutes": 25,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 20,
                },
                "USD": {
                    "withdraw_fee": 5.0,
                    "withdraw_minutes": 60,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 45,
                },
            },
        },
        "tiendacrypto": {
            "enabled": True,
            "adapter": "generic_p2p",
            "taker_fee_percent": 0.25,
            "fees": {
                "default": {
                    "taker": 0.25,
                    "maker": 0.20,
                    "slippage_bps": 20.0,
                }
            },
            "trade_links": {
                "default": "https://tiendacrypto.com/otc?asset={base}&fiat={quote}",
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://api.tiendacrypto.com/v1/rates",
                "method": "GET",
                "data_path": ["rates"],
                "bid_path": ["{asset}", "{fiat}", "bid"],
                "ask_path": ["{asset}", "{fiat}", "ask"],
                "pairs": {
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "static_quote": {"bid": 562.0, "ask": 578.0},
                    },
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.994, "ask": 1.006},
                    },
                    "BTC/ARS": {
                        "asset": "BTC",
                        "fiat": "ARS",
                        "static_quote": {"bid": 16150000.0, "ask": 16700000.0},
                    },
                    "BTC/USD": {
                        "asset": "BTC",
                        "fiat": "USD",
                        "static_quote": {"bid": 30080.0, "ask": 30450.0},
                    },
                    "ETH/ARS": {
                        "asset": "ETH",
                        "fiat": "ARS",
                        "static_quote": {"bid": 1018000.0, "ask": 1060000.0},
                    },
                    "ETH/USD": {
                        "asset": "ETH",
                        "fiat": "USD",
                        "static_quote": {"bid": 1818.0, "ask": 1850.0},
                    },
                    "XRP/ARS": {
                        "asset": "XRP",
                        "fiat": "ARS",
                        "static_quote": {"bid": 282.0, "ask": 297.0},
                    },
                    "XRP/USD": {
                        "asset": "XRP",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.52, "ask": 0.545},
                    },
                    "SOL/ARS": {
                        "asset": "SOL",
                        "fiat": "ARS",
                        "static_quote": {"bid": 12450.0, "ask": 13100.0},
                    },
                    "SOL/USD": {
                        "asset": "SOL",
                        "fiat": "USD",
                        "static_quote": {"bid": 22.5, "ask": 23.1},
                    },
                },
            },
            "transfers": {
                "USDT": {
                    "withdraw_fee": 1.5,
                    "withdraw_minutes": 35,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 25,
                },
                "USD": {
                    "withdraw_fee": 7.0,
                    "withdraw_minutes": 70,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 45,
                },
            },
        },
        "onebit": {
            "enabled": True,
            "adapter": "generic_p2p",
            "taker_fee_percent": 0.22,
            "fees": {
                "default": {
                    "taker": 0.22,
                    "maker": 0.18,
                    "slippage_bps": 18.0,
                }
            },
            "trade_links": {
                "default": "https://onebit.com/p2p?asset={base}&fiat={quote}",
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://api.onebit.com/v2/p2p/rates",
                "method": "GET",
                "data_path": ["quotes"],
                "bid_path": ["{asset}", "{fiat}", "sell"],
                "ask_path": ["{asset}", "{fiat}", "buy"],
                "invert_sides": True,
                "pairs": {
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "static_quote": {"bid": 568.0, "ask": 582.0},
                    },
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.996, "ask": 1.004},
                    },
                    "BTC/ARS": {
                        "asset": "BTC",
                        "fiat": "ARS",
                        "static_quote": {"bid": 16280000.0, "ask": 16780000.0},
                    },
                    "BTC/USD": {
                        "asset": "BTC",
                        "fiat": "USD",
                        "static_quote": {"bid": 30150.0, "ask": 30520.0},
                    },
                    "ETH/ARS": {
                        "asset": "ETH",
                        "fiat": "ARS",
                        "static_quote": {"bid": 1029000.0, "ask": 1068000.0},
                    },
                    "ETH/USD": {
                        "asset": "ETH",
                        "fiat": "USD",
                        "static_quote": {"bid": 1822.0, "ask": 1852.0},
                    },
                    "XRP/ARS": {
                        "asset": "XRP",
                        "fiat": "ARS",
                        "static_quote": {"bid": 286.0, "ask": 298.0},
                    },
                    "XRP/USD": {
                        "asset": "XRP",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.531, "ask": 0.548},
                    },
                    "SOL/ARS": {
                        "asset": "SOL",
                        "fiat": "ARS",
                        "static_quote": {"bid": 12560.0, "ask": 13150.0},
                    },
                    "SOL/USD": {
                        "asset": "SOL",
                        "fiat": "USD",
                        "static_quote": {"bid": 22.7, "ask": 23.15},
                    },
                },
            },
            "transfers": {
                "USDT": {
                    "withdraw_fee": 1.2,
                    "withdraw_minutes": 30,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 25,
                },
                "USD": {
                    "withdraw_fee": 6.0,
                    "withdraw_minutes": 65,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 40,
                },
            },
        },
        "ripio": {
            "enabled": True,
            "adapter": "generic_p2p",
            "taker_fee_percent": 0.28,
            "fees": {
                "default": {
                    "taker": 0.28,
                    "maker": 0.22,
                    "slippage_bps": 22.0,
                }
            },
            "trade_links": {
                "default": "https://app.ripio.com/otc?asset={base}&fiat={quote}",
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://api.ripio.com/v1/rates",
                "method": "GET",
                "data_path": ["data"],
                "bid_path": ["{asset}", "{fiat}", "bid"],
                "ask_path": ["{asset}", "{fiat}", "ask"],
                "pairs": {
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "static_quote": {"bid": 558.0, "ask": 579.0},
                    },
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.993, "ask": 1.007},
                    },
                    "BTC/ARS": {
                        "asset": "BTC",
                        "fiat": "ARS",
                        "static_quote": {"bid": 16080000.0, "ask": 16800000.0},
                    },
                    "BTC/USD": {
                        "asset": "BTC",
                        "fiat": "USD",
                        "static_quote": {"bid": 29980.0, "ask": 30580.0},
                    },
                    "ETH/ARS": {
                        "asset": "ETH",
                        "fiat": "ARS",
                        "static_quote": {"bid": 1012000.0, "ask": 1069000.0},
                    },
                    "ETH/USD": {
                        "asset": "ETH",
                        "fiat": "USD",
                        "static_quote": {"bid": 1810.0, "ask": 1855.0},
                    },
                    "XRP/ARS": {
                        "asset": "XRP",
                        "fiat": "ARS",
                        "static_quote": {"bid": 280.0, "ask": 300.0},
                    },
                    "XRP/USD": {
                        "asset": "XRP",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.52, "ask": 0.552},
                    },
                    "SOL/ARS": {
                        "asset": "SOL",
                        "fiat": "ARS",
                        "static_quote": {"bid": 12380.0, "ask": 13200.0},
                    },
                    "SOL/USD": {
                        "asset": "SOL",
                        "fiat": "USD",
                        "static_quote": {"bid": 22.4, "ask": 23.2},
                    },
                },
            },
            "transfers": {
                "USDT": {
                    "withdraw_fee": 1.8,
                    "withdraw_minutes": 40,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 30,
                },
                "USD": {
                    "withdraw_fee": 8.0,
                    "withdraw_minutes": 80,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 50,
                },
            },
        },
        "facebank": {
            "enabled": True,
            "adapter": "generic_p2p",
            "taker_fee_percent": 0.30,
            "fees": {
                "default": {
                    "taker": 0.30,
                    "maker": 0.25,
                    "slippage_bps": 25.0,
                }
            },
            "trade_links": {
                "default": "https://facebank.com/forex?asset={base}&fiat={quote}",
            },
            "p2p": {
                "enabled": True,
                "endpoint": "https://api.facebank.com/v1/rates",
                "method": "GET",
                "data_path": ["data"],
                "bid_path": ["{asset}", "{fiat}", "bid"],
                "ask_path": ["{asset}", "{fiat}", "ask"],
                "pairs": {
                    "USDT/ARS": {
                        "asset": "USDT",
                        "fiat": "ARS",
                        "static_quote": {"bid": 480.0, "ask": 498.0},
                    },
                    "USDT/USD": {
                        "asset": "USDT",
                        "fiat": "USD",
                        "static_quote": {"bid": 0.996, "ask": 1.002},
                    },
                    "USD/ARS": {
                        "asset": "USD",
                        "fiat": "ARS",
                        "static_quote": {"bid": 485.0, "ask": 500.0},
                    },
                    "USD/EUR": {
                        "asset": "USD",
                        "fiat": "EUR",
                        "static_quote": {"bid": 0.90, "ask": 0.92},
                    },
                },
            },
            "transfers": {
                "USDT": {
                    "withdraw_fee": 1.0,
                    "withdraw_minutes": 45,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 40,
                },
                "USD": {
                    "withdraw_fee": 4.5,
                    "withdraw_minutes": 50,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 35,
                },
            },
        },
        "kucoin": {
            "enabled": False,
        },
        "okx": {
            "enabled": False,
        },
        # add more venues aquí
    },
    "triangular_routes": [
        {
            "name": "usdt-btc-eth",
            "venue": "binance",
            "start_asset": "USDT",
            "legs": [
                {"pair": "BTC/USDT", "action": "BUY_BASE"},
                {"pair": "ETH/BTC", "action": "BUY_BASE"},
                {"pair": "ETH/USDT", "action": "SELL_BASE"},
            ],
        },
        {
            "name": "usdt-eth-btc",
            "venue": "binance",
            "start_asset": "USDT",
            "legs": [
                {"pair": "ETH/USDT", "action": "BUY_BASE"},
                {"pair": "ETH/BTC", "action": "SELL_BASE"},
                {"pair": "BTC/USDT", "action": "SELL_BASE"},
            ],
        },
        {
            "name": "usdt-btc-eth",
            "venue": "bybit",
            "start_asset": "USDT",
            "legs": [
                {"pair": "BTC/USDT", "action": "BUY_BASE"},
                {"pair": "ETH/BTC", "action": "BUY_BASE"},
                {"pair": "ETH/USDT", "action": "SELL_BASE"},
            ],
        },
        {
            "name": "usdt-eth-btc",
            "venue": "bybit",
            "start_asset": "USDT",
            "legs": [
                {"pair": "ETH/USDT", "action": "BUY_BASE"},
                {"pair": "ETH/BTC", "action": "SELL_BASE"},
                {"pair": "BTC/USDT", "action": "SELL_BASE"},
            ],
        },
    ],
    "telegram": {
        "enabled": True,                 # poner False para pruebas sin enviar
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_ids_env": "TG_CHAT_IDS",   # coma-separado: "-100123...,123456..."
    },
    "log_csv_path": str(Path(LOG_BASE_DIR) / "opportunities.csv"),
    "triangular_log_csv_path": str(Path(LOG_BASE_DIR) / "triangular_opportunities.csv"),
    "market_rules": {
        "binance": {
            "BTC/USDT": {"min_notional": 10.0, "min_qty": 0.0001, "step_size": 0.000001},
            "ETH/USDT": {"min_notional": 10.0, "min_qty": 0.001, "step_size": 0.0001},
            "XRP/USDT": {"min_notional": 5.0, "min_qty": 1.0, "step_size": 0.1},
            "SOL/USDT": {"min_notional": 10.0, "min_qty": 0.01, "step_size": 0.0001},
        },
        "bybit": {
            "BTC/USDT": {"min_notional": 10.0, "min_qty": 0.0001, "step_size": 0.000001},
            "ETH/USDT": {"min_notional": 10.0, "min_qty": 0.001, "step_size": 0.0001},
            "XRP/USDT": {"min_notional": 5.0, "min_qty": 1.0, "step_size": 0.1},
            "SOL/USDT": {"min_notional": 10.0, "min_qty": 0.01, "step_size": 0.0001},
        },
    },
}

DYNAMIC_THRESHOLD_PERCENT: float = float(CONFIG.get("threshold_percent", 0.0))

TELEGRAM_CHAT_IDS: Set[str] = set()
TELEGRAM_LAST_UPDATE_ID = 0
TELEGRAM_POLLING_THREAD: Optional[threading.Thread] = None
TELEGRAM_ADMIN_IDS: Set[str] = set()
TELEGRAM_POLL_BACKOFF_UNTIL = 0.0

TELEGRAM_POLL_CONFLICT_BACKOFF_SECONDS = 30.0
TELEGRAM_WEBHOOK_RESET_COOLDOWN_SECONDS = 600.0
TELEGRAM_LAST_WEBHOOK_RESET_TS = 0.0

STATE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
DASHBOARD_STATE: Dict[str, Any] = {
    "last_run_summary": None,
    "latest_alerts": [],
    "config_snapshot": {},
    "exchange_metrics": {},
    "analysis": None,
}

MAX_ALERT_HISTORY = 20

WEB_AUTH_USER = os.getenv("WEB_AUTH_USER", "").strip()
WEB_AUTH_PASS = os.getenv("WEB_AUTH_PASS", "").strip()

LATEST_ANALYSIS: Optional[Any] = None
LAST_TELEGRAM_SEND_TS: float = 0.0
PENDING_CHAT_ACTIONS: Dict[str, str] = {}


LOG_HEADER = [
    "ts",
    "pair",
    "buy_venue",
    "sell_venue",
    "buy_price",
    "sell_price",
    "gross_%",
    "net_%",
    "est_profit_quote",
    "base_qty",
    "capital_used_quote",
    "buy_depth_base",
    "sell_depth_base",
    "liquidity_score",
    "volatility_score",
    "priority_score",
    "confidence",
]


def snapshot_public_config() -> Dict[str, Any]:
    venues = {
        name: {
            "enabled": bool(data.get("enabled", False)),
            "taker_fee_percent": float(data.get("taker_fee_percent", 0.0)),
        }
        for name, data in CONFIG.get("venues", {}).items()
    }
    return {
        "threshold_percent": float(CONFIG.get("threshold_percent", 0.0)),
        "pairs": normalize_pair_list(CONFIG.get("pairs", [])),
        "simulation_capital_quote": float(CONFIG.get("simulation_capital_quote", 0.0)),
        "venues": venues,
        "telegram_enabled": bool(CONFIG.get("telegram", {}).get("enabled", False)),
    }


def refresh_config_snapshot() -> None:
    with STATE_LOCK:
        DASHBOARD_STATE["config_snapshot"] = snapshot_public_config()

def update_analysis_state(capital_quote: float, log_path: str) -> None:
    """Refresh cached analysis metrics and dynamic threshold from CSV history."""

    global LATEST_ANALYSIS, DYNAMIC_THRESHOLD_PERCENT

    if not log_path:
        return

    base_threshold = float(CONFIG.get("threshold_percent", 0.0))
    analysis_cfg = CONFIG.get("analysis") or {}
    if not analysis_cfg.get("enabled", True):
        DYNAMIC_THRESHOLD_PERCENT = base_threshold
        with STATE_LOCK:
            DASHBOARD_STATE["analysis"] = None
        return

    try:
        analysis = analyze_historical_performance(log_path, capital_quote)
    except Exception as exc:  # pragma: no cover - defensive logging
        log_event("analysis.error", error=str(exc))
        return

    recommended = float(analysis.recommended_threshold or base_threshold)
    if not math.isfinite(recommended) or recommended <= 0:
        recommended = base_threshold

    LATEST_ANALYSIS = analysis
    DYNAMIC_THRESHOLD_PERCENT = recommended

    with STATE_LOCK:
        DASHBOARD_STATE["analysis"] = {
            "rows_considered": analysis.rows_considered,
            "success_rate": analysis.success_rate,
            "average_net_percent": analysis.average_net_percent,
            "average_effective_percent": analysis.average_effective_percent,
            "recommended_threshold": recommended,
        }

    log_event(
        "analysis.updated",
        recommended_threshold=recommended,
        rows=analysis.rows_considered,
        success_rate=analysis.success_rate,
    )

FEE_REGISTRY: Dict[Tuple[str, str], float] = {}


COMMANDS_HELP: List[Tuple[str, str]] = [
    ("/ping", "Ping"),
    ("/status", "Estado"),
    ("/capital", "Capital"),
    ("/listapares", "Lista de pares"),
    ("/adherirpar", "Adherir par"),
    ("/eliminarpar", "Eliminar par"),
    ("/senalprueba", "Señal de prueba"),
]


def format_command_help() -> str:
    return (
        "📟 Para ver los comandos disponibles, tocá el botón "
        '"Menú" que aparece junto a la carita de emoticones en Telegram.\n'
        "Desde allí vas a encontrar accesos directos a todas las acciones del bot."
    )


def get_bot_token() -> str:
    return os.getenv(CONFIG["telegram"]["bot_token_env"], "").strip()


def tg_commands_reply_markup() -> Dict[str, Any]:
    """Construye un teclado con accesos directos a los comandos del bot."""

    keyboard: List[List[Dict[str, str]]] = []
    row: List[Dict[str, str]] = []
    for idx, (command, _description) in enumerate(COMMANDS_HELP, start=1):
        row.append({"text": command})
        if idx % 2 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
    }


def tg_command_menu_payload() -> List[Dict[str, str]]:
    payload: List[Dict[str, str]] = []
    for command, description in COMMANDS_HELP:
        payload.append({
            "command": command.lstrip("/"),
            "description": description[:256],
        })
    return payload


def set_pending_action(chat_id: str, action: Optional[str]) -> None:
    if action:
        PENDING_CHAT_ACTIONS[chat_id] = action
    else:
        PENDING_CHAT_ACTIONS.pop(chat_id, None)


def get_pending_action(chat_id: str) -> Optional[str]:
    return PENDING_CHAT_ACTIONS.get(chat_id)


def normalize_pair_input(raw_value: str) -> Optional[str]:
    cleaned = raw_value.strip().upper().replace(" ", "")
    if not cleaned:
        return None
    if "/" in cleaned:
        base, _, quote = cleaned.partition("/")
        base = base.strip()
        quote = quote.strip()
        if not base or not quote:
            return None
        return f"{base}/{quote}"
    return f"{cleaned}/{DEFAULT_QUOTE_ASSET}"


def normalize_pair_list(pairs: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    normalized: List[str] = []
    for raw_value in pairs:
        if raw_value is None:
            continue
        normalized_pair = normalize_pair_input(str(raw_value))
        if not normalized_pair:
            continue
        if normalized_pair not in seen:
            normalized.append(normalized_pair)
            seen.add(normalized_pair)
    return normalized


def build_pairs_reply_keyboard(pairs: Iterable[str]) -> Dict[str, Any]:
    keyboard: List[List[Dict[str, str]]] = []
    row: List[Dict[str, str]] = []
    for idx, pair in enumerate(sorted(pairs), start=1):
        row.append({"text": pair})
        if idx % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def tg_enable_menu_button(chat_id: Optional[str] = None) -> None:
    """Fuerza el botón de menú de comandos en el cliente de Telegram."""

    token = get_bot_token()
    if not token:
        log_event("telegram.menu_button.skip", reason="missing_token")
        return

    params = {"menu_button": json.dumps({"type": "commands"})}
    if chat_id:
        params["chat_id"] = str(chat_id)

    try:
        tg_api_request(
            "setChatMenuButton",
            params=params,
            http_method="post",
        )
    except Exception as exc:  # pragma: no cover - logging only
        log_event("telegram.menu_button.error", error=str(exc), chat_id=chat_id)
    else:
        log_event("telegram.menu_button.enabled", chat_id=chat_id)


def build_test_signal_message() -> str:
    capital = float(CONFIG.get("simulation_capital_quote", 0.0))

    sample_opportunity = Opportunity(
        pair="BTC/USDT",
        buy_venue="binance",
        sell_venue="bybit",
        buy_price=30050.0,
        sell_price=30290.0,
        gross_percent=((30290.0 - 30050.0) / 30050.0) * 100,
        net_percent=0.82,
    )

    base_qty = 0.0
    if sample_opportunity.buy_price > 0 and capital > 0:
        base_qty = capital / sample_opportunity.buy_price

    est_profit = (capital * sample_opportunity.net_percent) / 100.0 if capital else 0.0
    capital_used = base_qty * sample_opportunity.buy_price
    sample_opportunity.liquidity_score = 0.5
    sample_opportunity.volatility_score = 0.3
    sample_opportunity.priority_score = sample_opportunity.net_percent
    sample_opportunity.confidence_label = "media"
    link_items = build_trade_link_items(
        sample_opportunity.buy_venue, sample_opportunity.sell_venue, sample_opportunity.pair
    )

    alert_message = fmt_alert(
        sample_opportunity,
        est_profit=est_profit,
        est_percent=sample_opportunity.net_percent,
        base_qty=base_qty,
        capital_quote=capital,
        capital_used=capital_used,
        links=link_items,
    )

    return "Señal de prueba ✅\n\n" + alert_message


def tg_sync_command_menu(enabled: bool = True) -> None:
    if not enabled:
        return

    token = get_bot_token()
    if not token:
        log_event("telegram.commands.skip", reason="missing_token")
        return

    commands_payload = tg_command_menu_payload()
    try:
        tg_api_request(
            "setMyCommands",
            params={"commands": json.dumps(commands_payload)},
            http_method="post",
        )
    except Exception as exc:  # pragma: no cover - logging only
        log_event("telegram.commands.error", error=str(exc))
    else:
        log_event("telegram.commands.synced", commands=len(commands_payload))
        tg_enable_menu_button()
        for chat_id in get_registered_chat_ids():
            tg_enable_menu_button(chat_id=chat_id)


def _load_telegram_chat_ids_from_env() -> None:
    chat_ids_env = os.getenv(CONFIG["telegram"]["chat_ids_env"], "").strip()
    if not chat_ids_env:
        return
    for cid in chat_ids_env.split(","):
        cid = cid.strip()
        if cid:
            TELEGRAM_CHAT_IDS.add(cid)
    os.environ[CONFIG["telegram"]["chat_ids_env"]] = ",".join(sorted(TELEGRAM_CHAT_IDS))


_load_telegram_chat_ids_from_env()


def _load_telegram_admin_ids_from_env() -> None:
    admin_ids_env = os.getenv("TG_ADMIN_IDS", "").strip()
    if not admin_ids_env:
        return
    for cid in admin_ids_env.split(","):
        cid = cid.strip()
        if cid:
            TELEGRAM_ADMIN_IDS.add(cid)


_load_telegram_admin_ids_from_env()

refresh_config_snapshot()

# =========================
# HTTP / Dashboard
# =========================


def build_health_payload() -> Dict[str, Any]:
    now = time.time()
    metrics = metrics_snapshot()
    with STATE_LOCK:
        summary = DASHBOARD_STATE.get("last_run_summary") or {}
        latest_alerts = list(DASHBOARD_STATE.get("latest_alerts", []))[:5]
        latest_quotes = DASHBOARD_STATE.get("latest_quotes", {})
        quote_latency = DASHBOARD_STATE.get("last_quote_latency_ms")

    status = "ok"
    if not summary:
        status = "booting"
    elif any(stats.get("errors") for stats in metrics.values()):
        status = "degraded"

    telegram_enabled = bool(CONFIG.get("telegram", {}).get("enabled", False))
    if telegram_enabled and LAST_TELEGRAM_SEND_TS:
        seconds_since_last_send = now - LAST_TELEGRAM_SEND_TS
    else:
        seconds_since_last_send = None

    payload = {
        "status": status,
        "timestamp": int(now),
        "last_run_ts": summary.get("ts"),
        "last_run_iso": summary.get("ts_str"),
        "run_latency_ms": summary.get("run_latency_ms"),
        "quote_latency_ms": quote_latency or summary.get("quote_latency_ms"),
        "alerts_sent_last_run": summary.get("alerts_sent", 0),
        "triangular_alerts_last_run": summary.get("triangular_alerts", 0),
        "metrics": metrics,
        "latest_alerts": latest_alerts,
        "latest_quotes": latest_quotes,
        "telegram": {
            "enabled": telegram_enabled,
            "last_send_ts": LAST_TELEGRAM_SEND_TS or None,
            "seconds_since_last_send": seconds_since_last_send,
            "registered_chats": len(get_registered_chat_ids()),
        },
    }
    return payload


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang=\"es\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Arbitrage TeleBot</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
    header { padding: 1.5rem; background: #1e293b; display: flex; flex-wrap: wrap; gap: 1rem; align-items: baseline; }
    header h1 { margin: 0; font-size: 1.8rem; }
    main { padding: 1.5rem; }
    section { margin-bottom: 2rem; background: #1e293b; padding: 1.5rem; border-radius: 12px; box-shadow: 0 10px 30px rgba(15,23,42,0.4); }
    h2 { margin-top: 0; font-size: 1.4rem; color: #f8fafc; }
    table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
    th, td { padding: 0.6rem; text-align: left; border-bottom: 1px solid rgba(148, 163, 184, 0.25); }
    th { text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.08em; color: #94a3b8; }
    tr:last-child td { border-bottom: none; }
    tr.threshold-hit { background: rgba(34, 197, 94, 0.12); }
    .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-top: 1rem; }
    .stat-card { background: rgba(148, 163, 184, 0.08); padding: 1rem; border-radius: 10px; }
    .stat-card span { display: block; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: #cbd5f5; margin-bottom: 0.25rem; }
    .stat-card strong { font-size: 1.3rem; }
    button, input, textarea { font: inherit; border-radius: 8px; border: none; padding: 0.6rem 0.8rem; }
    button { background: #22d3ee; color: #0f172a; font-weight: 600; cursor: pointer; }
    button:hover { background: #0ea5e9; }
    label { display: block; margin-bottom: 0.6rem; }
    input, textarea { width: 100%; margin-top: 0.35rem; background: rgba(148, 163, 184, 0.12); color: #f8fafc; border: 1px solid rgba(148, 163, 184, 0.2); }
    textarea { min-height: 70px; }
    .alert-card { background: rgba(34, 211, 238, 0.1); border: 1px solid rgba(34, 211, 238, 0.4); border-radius: 10px; padding: 1rem; margin-top: 1rem; }
    .alert-card h3 { margin-top: 0; }
    .timestamp { color: #94a3b8; font-size: 0.9rem; }
    a { color: #38bdf8; }
    footer { text-align: center; padding: 1rem; color: #475569; }
  </style>
</head>
<body>
  <header>
    <h1>Arbitrage TeleBot Dashboard</h1>
    <p id=\"updatedAt\" class=\"timestamp\"></p>
  </header>
  <main>
    <section>
      <h2>Estado</h2>
      <div class=\"stat-grid\">
        <div class=\"stat-card\"><span>Threshold</span><strong id=\"threshold\">-</strong></div>
        <div class=\"stat-card\"><span>Capital simulado</span><strong id=\"capital\">-</strong></div>
        <div class=\"stat-card\"><span>Última ejecución</span><strong id=\"lastRun\">-</strong></div>
        <div class=\"stat-card\"><span>Alertas recientes</span><strong id=\"alertCount\">0</strong></div>
      </div>
    </section>
    <section>
      <h2>Oportunidades recientes</h2>
      <table>
        <thead>
          <tr>
            <th>Par</th>
            <th>Comprar</th>
            <th>Vender</th>
            <th>Spread Neto</th>
            <th>PnL estimado</th>
            <th>Confianza</th>
            <th>Liquidez</th>
            <th>Links</th>
          </tr>
        </thead>
        <tbody id=\"opportunities\">
          <tr><td colspan=\"6\">Sin datos todavía.</td></tr>
        </tbody>
      </table>
    </section>
    <section>
      <h2>Últimas alertas</h2>
      <div id=\"alerts\"></div>
    </section>
    <section>
      <h2>Configuración</h2>
      <form id=\"configForm\">
        <label>Threshold (%)
          <input type=\"number\" step=\"0.01\" name=\"threshold_percent\" required />
        </label>
        <label>Capital simulado (USDT)
          <input type=\"number\" step=\"0.01\" name=\"simulation_capital_quote\" required />
        </label>
        <label>Pares (uno por línea)
          <textarea name=\"pairs\"></textarea>
        </label>
        <button type=\"submit\">Guardar cambios</button>
        <p id=\"configStatus\" class=\"timestamp\"></p>
      </form>
    </section>
  </main>
  <footer>Panel autenticado · generado por Arbitrage TeleBot</footer>
  <script>
    async function fetchState() {
      try {
        const res = await fetch('/api/state', { cache: 'no-store', credentials: 'include' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        renderState(data);
      } catch (err) {
        document.getElementById('updatedAt').textContent = 'Error al cargar estado: ' + err;
      }
    }

    function formatNumber(value, decimals = 2) {
      if (typeof value === 'number' && Number.isFinite(value)) {
        return value.toFixed(decimals);
      }
      return value ?? '-';
    }

    function renderState(data) {
      const cfg = data.config_snapshot || {};
      const summary = data.last_run_summary || {};
      document.getElementById('threshold').textContent = formatNumber(cfg.threshold_percent, 3) + ' %';
      document.getElementById('capital').textContent = formatNumber(cfg.simulation_capital_quote, 2) + ' USDT';
      document.getElementById('lastRun').textContent = summary.ts_str || '-';
      document.getElementById('alertCount').textContent = summary.alerts_sent ?? 0;
      const tbody = document.getElementById('opportunities');
      tbody.innerHTML = '';
      const opps = (summary.opportunities || []);
      if (!opps.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 6;
        cell.textContent = 'Sin oportunidades en la última corrida.';
        row.appendChild(cell);
        tbody.appendChild(row);
      } else {
        opps.forEach((opp) => {
          const row = document.createElement('tr');
          if (opp.threshold_hit) {
            row.classList.add('threshold-hit');
          }
          row.innerHTML = `
            <td>${opp.pair}</td>
            <td>${opp.buy_venue} · ${formatNumber(opp.buy_price, 6)}</td>
            <td>${opp.sell_venue} · ${formatNumber(opp.sell_price, 6)}</td>
            <td>${formatNumber(opp.net_percent, 3)} %</td>
            <td>${formatNumber(opp.est_profit_quote, 2)} USDT</td>
            <td>${(opp.confidence || '').toUpperCase()}</td>
            <td>${formatNumber(opp.liquidity_score ?? 0, 2)}</td>
            <td>${renderLinks(opp.links)}</td>`;
          tbody.appendChild(row);
        });
      }

      const alertsRoot = document.getElementById('alerts');
      alertsRoot.innerHTML = '';
      (data.latest_alerts || []).forEach((alert) => {
        const card = document.createElement('div');
        card.className = 'alert-card';
        card.innerHTML = `
          <h3>${alert.pair} · ${formatNumber(alert.net_percent, 3)} %</h3>
          <p>${alert.buy_venue} ➜ ${alert.sell_venue}</p>
          <p>PnL estimado: ${formatNumber(alert.est_profit_quote, 2)} USDT (${formatNumber(alert.est_percent, 3)} %)</p>
          <p>${renderLinks(alert.links)}</p>
          <p>Confianza: ${(alert.confidence || '').toUpperCase()} · Liquidez: ${formatNumber(alert.liquidity_score ?? 0, 2)}</p>
          <p class='timestamp'>${alert.ts_str}</p>`;
        alertsRoot.appendChild(card);
      });

      const form = document.getElementById('configForm');
      form.threshold_percent.value = cfg.threshold_percent ?? '';
      form.simulation_capital_quote.value = cfg.simulation_capital_quote ?? '';
      form.pairs.value = (cfg.pairs || []).join('\n');
      document.getElementById('updatedAt').textContent = 'Última actualización: ' + (summary.ts_str || 'sin datos');
    }

    function renderLinks(links) {
      if (!links || !links.length) { return '—'; }
      return links.map((item) => `<a href="${item.url}" target="_blank" rel="noopener noreferrer">${item.label}</a>`).join(' · ');
    }

    document.getElementById('configForm').addEventListener('submit', async (evt) => {
      evt.preventDefault();
      const form = evt.target;
      const payload = {
        threshold_percent: parseFloat(form.threshold_percent.value),
        simulation_capital_quote: parseFloat(form.simulation_capital_quote.value),
        pairs: form.pairs.value.split('\n').map(v => v.trim()).filter(Boolean),
      };
      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        document.getElementById('configStatus').textContent = 'Configuración actualizada correctamente';
        fetchState();
      } catch (err) {
        document.getElementById('configStatus').textContent = 'Error al guardar: ' + err;
      }
    });

    fetchState();
    setInterval(fetchState, 5000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def _is_healthcheck(self) -> bool:
        return self.path in ("/health", "/live", "/ready")

    def _decode_auth_header(self, auth_header: str) -> Optional[str]:
        if not auth_header or " " not in auth_header:
            self._send_unauthorized()
            return None
        try:
            encoded_part = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded_part).decode("utf-8")
            return decoded
        except (IndexError, base64.binascii.Error, UnicodeDecodeError):
            self._send_unauthorized()
            return None

    def _parse_json(self, raw: bytes) -> Optional[Dict[str, Any]]:
        try:
            decoded = raw.decode("utf-8")
            data = json.loads(decoded or "{}")
            return data
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "JSON inválido"}, status=400)
            return None

    def _require_authentication(self) -> bool:
        if not WEB_AUTH_USER and not WEB_AUTH_PASS:
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            self._send_unauthorized()
            return False
        decoded = self._decode_auth_header(auth_header)
        if not decoded:
            return False
        if ":" not in decoded:
            self._send_unauthorized()
            return False
        user, password = decoded.split(":", 1)
        if user == WEB_AUTH_USER and password == WEB_AUTH_PASS:
            return True
        self._send_unauthorized()
        return False

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Arbitrage TeleBot"')
        self.end_headers()

    def do_HEAD(self):
        if self._is_healthcheck():
            self.send_response(200)
            self.end_headers()
            return
        if not self._require_authentication():
            return
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self._is_healthcheck():
            payload = build_health_payload()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/dashboard"):
            if not self._require_authentication():
                return
            self._send_html(DASHBOARD_HTML)
            return
        if self.path == "/metrics":
            body = generate_latest(PROM_REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            if not self._require_authentication():
                return
            with STATE_LOCK:
                payload = {
                    "last_run_summary": DASHBOARD_STATE.get("last_run_summary"),
                    "latest_alerts": DASHBOARD_STATE.get("latest_alerts", []),
                    "config_snapshot": DASHBOARD_STATE.get("config_snapshot", {}),
                    "exchange_metrics": DASHBOARD_STATE.get("exchange_metrics", {}),
                    "analysis": DASHBOARD_STATE.get("analysis"),
                }
            self._send_json(payload)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/config":
            global DYNAMIC_THRESHOLD_PERCENT
            if not self._require_authentication():
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = self._parse_json(raw)
            if data is None:
                return
            updated = {}
            errors: List[str] = []
            with CONFIG_LOCK:
                if "threshold_percent" in data:
                    try:
                        value = float(data["threshold_percent"])
                        CONFIG["threshold_percent"] = value
                        DYNAMIC_THRESHOLD_PERCENT = value
                        updated["threshold_percent"] = value
                    except (TypeError, ValueError):
                        errors.append("threshold_percent inválido")
                if "simulation_capital_quote" in data:
                    try:
                        value = float(data["simulation_capital_quote"])
                        if value <= 0:
                            raise ValueError
                        CONFIG["simulation_capital_quote"] = value
                        updated["simulation_capital_quote"] = value
                    except (TypeError, ValueError):
                        errors.append("simulation_capital_quote inválido")
                if "pairs" in data:
                    if isinstance(data["pairs"], list):
                        pairs = [str(p).upper() for p in data["pairs"] if str(p).strip()]
                        if pairs:
                            CONFIG["pairs"] = pairs
                            updated["pairs"] = pairs
                        else:
                            errors.append("pairs no puede quedar vacío")
                    else:
                        errors.append("pairs debe ser lista")
            refresh_config_snapshot()
            if not errors:
                capital = float(CONFIG.get("simulation_capital_quote", 0.0))
                log_path = str(CONFIG.get("log_csv_path", ""))
                update_analysis_state(capital, log_path)
            status = 200 if not errors else 400
            self._send_json({"updated": updated, "errors": errors, "config": DASHBOARD_STATE["config_snapshot"]}, status=status)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - reduce noise
        print(f"[WEB] {self.client_address[0]} {self.command} {self.path} -> {format % args}")


def serve_http(port: int):
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    log_event("web.listen_start", port=port)
    server.serve_forever()

def run_loop_forever(interval: int):
    while True:
        try:
            run_once()
        except Exception as e:
            log_event("loop.error", error=str(e))
        time.sleep(max(5, interval))

# =========================
# HTTP helpers
# =========================
class HttpError(Exception):
    """Error HTTP con código opcional."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code

def current_millis() -> int:
    return int(time.time() * 1000)


@dataclass
class HttpJsonResponse:
    data: Dict[str, Any]
    checksum: str
    received_ts: int


LAST_CHECKSUMS: Dict[str, Tuple[str, int]] = {}
MAX_CHECKSUM_STALENESS_MS = 60_000


NON_RETRYABLE_STATUS_CODES = {401, 403, 451}


def http_get_json(
    url: str,
    params: Optional[dict] = None,
    timeout: int = 8,
    retries: int = 3,
    integrity_key: Optional[str] = None,
    fallback_endpoints: Optional[List[Tuple[str, Optional[dict]]]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> HttpJsonResponse:
    last_exc: Optional[Exception] = None
    endpoints: List[Tuple[str, Optional[dict]]] = [(url, params)]
    if fallback_endpoints:
        endpoints.extend(fallback_endpoints)

    for endpoint_url, endpoint_params in endpoints:
        non_retryable_error = False
        for attempt in range(retries):
            try:
                r = requests.get(
                    endpoint_url,
                    params=endpoint_params,
                    timeout=timeout,
                    headers=headers,
                )
                if r.status_code != 200:
                    raise HttpError(
                        f"HTTP {r.status_code} {endpoint_url} params={endpoint_params}",
                        status_code=r.status_code,
                    )

                received_ts = current_millis()
                checksum = hashlib.sha256(r.content).hexdigest()
                payload = r.json()
                if not isinstance(payload, dict):
                    raise HttpError(f"Respuesta no es JSON objeto en {endpoint_url}")

                if integrity_key:
                    last_checksum, last_ts = LAST_CHECKSUMS.get(integrity_key, (None, 0))
                    if last_checksum == checksum and received_ts - last_ts > MAX_CHECKSUM_STALENESS_MS:
                        raise HttpError(
                            f"Checksum sin cambios por {received_ts - last_ts} ms para {integrity_key}"
                        )
                    LAST_CHECKSUMS[integrity_key] = (checksum, received_ts)

                return HttpJsonResponse(payload, checksum, received_ts)
            except Exception as e:
                last_exc = e
                if isinstance(e, HttpError) and e.status_code in NON_RETRYABLE_STATUS_CODES:
                    non_retryable_error = True
                    break
                backoff = min(0.5 * (2 ** attempt), 5.0)
                time.sleep(backoff + random.uniform(0, 0.25))
        if non_retryable_error:
            break
        if fallback_endpoints and last_exc is not None:
            print(f"[http] cambiando a endpoint alternativo {endpoint_url}: {last_exc}")

    raise last_exc or HttpError("GET failed")


MAX_ALLOWED_CLOCK_SKEW_MS = 5_000


def http_post_json(
    url: str,
    payload: Optional[dict] = None,
    timeout: int = 8,
    retries: int = 3,
    headers: Optional[Dict[str, str]] = None,
    fallback_endpoints: Optional[List[Tuple[str, Optional[dict]]]] = None,
) -> HttpJsonResponse:
    last_exc: Optional[Exception] = None
    effective_headers = {"Content-Type": "application/json"}
    if headers:
        effective_headers.update(headers)
    endpoints: List[Tuple[str, Optional[dict]]] = [(url, payload)]
    if fallback_endpoints:
        endpoints.extend(fallback_endpoints)

    for endpoint_url, endpoint_payload in endpoints:
        non_retryable_error = False
        for attempt in range(retries):
            try:
                r = requests.post(
                    endpoint_url,
                    json=endpoint_payload,
                    headers=effective_headers,
                    timeout=timeout,
                )
                if r.status_code != 200:
                    raise HttpError(
                        f"HTTP {r.status_code} {endpoint_url} payload={endpoint_payload}",
                        status_code=r.status_code,
                    )

                received_ts = current_millis()
                checksum = hashlib.sha256(r.content).hexdigest()
                payload_json = r.json()
                if not isinstance(payload_json, dict):
                    raise HttpError(f"Respuesta no es JSON objeto en {endpoint_url}")

                return HttpJsonResponse(payload_json, checksum, received_ts)
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, HttpError) and exc.status_code in NON_RETRYABLE_STATUS_CODES:
                    non_retryable_error = True
                    break
                backoff = min(0.5 * (2 ** attempt), 5.0)
                time.sleep(backoff + random.uniform(0, 0.25))
        if non_retryable_error:
            break
        if fallback_endpoints and last_exc is not None:
            print(f"[http] cambiando a endpoint alternativo {endpoint_url}: {last_exc}")

    raise last_exc or HttpError("POST failed")


def ensure_fresh_timestamp(ts_ms: int, received_ts: int, source: str) -> int:
    if ts_ms <= 0:
        raise HttpError(f"Timestamp inválido en {source}: {ts_ms}")
    if abs(received_ts - ts_ms) > MAX_ALLOWED_CLOCK_SKEW_MS:
        raise HttpError(
            f"Timestamp desfasado en {source}: diff={received_ts - ts_ms} ms"
        )
    return ts_ms


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _format_with_context(value: Any, context: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format(**context)
        except Exception:
            return value
    if isinstance(value, list):
        return [_format_with_context(item, context) for item in value]
    if isinstance(value, tuple):
        return tuple(_format_with_context(item, context) for item in value)
    if isinstance(value, dict):
        return {key: _format_with_context(val, context) for key, val in value.items()}
    return value


def _normalize_json_path(
    path: Any,
    context: Dict[str, Any],
) -> List[Union[str, int]]:
    if path is None:
        return []
    if isinstance(path, (str, int)):
        path_items: List[Any] = [path]
    else:
        path_items = list(path)
    normalized: List[Union[str, int]] = []
    for item in path_items:
        formatted = _format_with_context(item, context)
        if isinstance(formatted, str) and "." in formatted and not formatted.strip().isdigit():
            parts = [part for part in formatted.split(".") if part]
            normalized.extend(parts)
        else:
            normalized.append(formatted)
    result: List[Union[str, int]] = []
    for item in normalized:
        if isinstance(item, int):
            result.append(item)
        else:
            try:
                result.append(int(item))
            except Exception:
                result.append(str(item))
    return result


def _extract_json_path(data: Any, path: List[Union[str, int]]) -> Any:
    current = data
    for segment in path:
        if isinstance(segment, int):
            if isinstance(current, list) and 0 <= segment < len(current):
                current = current[segment]
            else:
                return None
        else:
            if isinstance(current, dict):
                current = current.get(segment)
            else:
                return None
    return current


@dataclass
class DepthInfo:
    best_bid: float
    best_ask: float
    bid_volume: float
    ask_volume: float
    levels: int
    ts: int
    checksum: str


@dataclass
class DepthCacheEntry:
    info: DepthInfo
    stored_ts: int


class DepthCache:
    def __init__(self, ttl_ms: int = 5_000):
        self.ttl_ms = ttl_ms
        self._lock = threading.Lock()
        self._data: Dict[Tuple[str, str], DepthCacheEntry] = {}

    def get(self, key: Tuple[str, str], now_ms: Optional[int] = None) -> Optional[DepthInfo]:
        now = now_ms or current_millis()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if now - entry.stored_ts > self.ttl_ms:
                return None
            return entry.info

    def set(self, key: Tuple[str, str], info: DepthInfo) -> None:
        with self._lock:
            self._data[key] = DepthCacheEntry(info=info, stored_ts=current_millis())


DEPTH_CACHE = DepthCache()

# =========================
# Telegram (HTTP API)
# =========================
def register_telegram_chat(chat_id) -> str:
    cid = str(chat_id)
    if cid not in TELEGRAM_CHAT_IDS:
        TELEGRAM_CHAT_IDS.add(cid)
        os.environ[CONFIG["telegram"]["chat_ids_env"]] = ",".join(sorted(TELEGRAM_CHAT_IDS))
        log_event("telegram.chat_registered", chat_id=cid)
        tg_enable_menu_button(chat_id=cid)
    return cid


def get_registered_chat_ids() -> List[str]:
    return sorted(TELEGRAM_CHAT_IDS)


def is_admin_chat(chat_id: str) -> bool:
    if not TELEGRAM_ADMIN_IDS:
        return True
    return str(chat_id) in TELEGRAM_ADMIN_IDS


def ensure_admin(chat_id: str, enabled: bool) -> bool:
    if is_admin_chat(chat_id):
        return True
    tg_send_message(
        "⚠️ Este comando requiere privilegios de administrador.",
        enabled=enabled,
        chat_id=chat_id,
    )
    return False


def tg_send_message(
    text: str,
    *,
    enabled: bool = True,
    chat_id: Optional[str] = None,
    preview: Optional[str] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> None:
    effective_preview = preview or (text if len(text) <= 400 else text[:400] + "…")
    if not enabled:
        log_event("telegram.send.skip", reason="disabled", preview=effective_preview)
        return

    token = get_bot_token()
    if not token:
        log_event("telegram.send.skip", reason="missing_token", preview=effective_preview)
        return

    targets: List[str]
    if chat_id is not None:
        targets = [str(chat_id)]
    else:
        targets = get_registered_chat_ids()

    if not targets:
        log_event("telegram.send.skip", reason="no_targets", preview=effective_preview)
        return

    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in targets:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}
            if reply_markup is not None:
                payload["reply_markup"] = json.dumps(reply_markup)
            r = requests.post(base, data=payload, timeout=8)
            if r.status_code != 200:
                log_event(
                    "telegram.send.error",
                    chat_id=cid,
                    status=r.status_code,
                    response=r.text[:200],
                )
            else:
                log_event("telegram.send.success", chat_id=cid)
                LAST_TELEGRAM_SEND_TS = time.time()
        except Exception as e:
            log_event("telegram.send.exception", chat_id=cid, error=str(e))


def tg_api_request(method: str, params: Optional[Dict] = None, http_method: str = "get") -> Dict:
    token = get_bot_token()
    if not token:
        raise HttpError("Falta TG_BOT_TOKEN")

    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if http_method.lower() == "post":
            r = requests.post(url, data=params or {}, timeout=8)
        else:
            r = requests.get(url, params=params or {}, timeout=8)
    except Exception as e:
        raise HttpError(f"Error al invocar {method}: {e}") from e

    if r.status_code != 200:
        raise HttpError(f"HTTP {r.status_code} -> {r.text}", status_code=r.status_code)

    data = r.json()
    if not data.get("ok"):
        raise HttpError(f"Respuesta no OK en {method}: {data}")
    return data


def tg_handle_pending_input(chat_id: str, text: str, enabled: bool) -> bool:
    action = get_pending_action(chat_id)
    if not action:
        return False

    raw_text = text.strip()
    value = normalize_pair_input(text)
    if not value:
        tg_send_message(
            "No pude interpretar ese valor. Probá nuevamente o enviá otro comando para cancelar.",
            enabled=enabled,
            chat_id=chat_id,
        )
        return True

    if action == "addpair":
        pair = value
        if pair in CONFIG["pairs"]:
            tg_send_message(
                (
                    f"{pair} ya está configurado. Ingresá otra cripto o "
                    "enviá cualquier comando para cancelar."
                ),
                enabled=enabled,
                chat_id=chat_id,
            )
            return True
        with CONFIG_LOCK:
            CONFIG["pairs"].append(pair)
        refresh_config_snapshot()
        set_pending_action(chat_id, None)
        tg_send_message(
            f"Par agregado: {pair}",
            enabled=enabled,
            chat_id=chat_id,
        )
        return True

    if action == "delpair":
        target = value
        if "/" not in raw_text:
            base = raw_text.strip().upper().replace(" ", "")
            if base:
                candidates = [p for p in CONFIG["pairs"] if p.startswith(f"{base}/")]
                if len(candidates) == 1:
                    target = candidates[0]
        if target not in CONFIG["pairs"]:
            tg_send_message(
                (
                    f"{target} no figura en la lista. Elegí otro de los botones "
                    "o ingresá una cripto válida."
                ),
                enabled=enabled,
                chat_id=chat_id,
            )
            return True
        with CONFIG_LOCK:
            CONFIG["pairs"] = [p for p in CONFIG["pairs"] if p != target]
        refresh_config_snapshot()
        set_pending_action(chat_id, None)
        tg_send_message(
            f"Par eliminado: {target}",
            enabled=enabled,
            chat_id=chat_id,
            reply_markup={"remove_keyboard": True},
        )
        return True

    return False


def tg_handle_command(command: str, argument: str, chat_id: str, enabled: bool) -> None:
    global DYNAMIC_THRESHOLD_PERCENT
    command = command.lower()
    register_telegram_chat(chat_id)

    if command == "/start":
        response = (
            "Hola! Ya estás registrado para recibir señales.\n"
            f"Threshold base: {CONFIG['threshold_percent']:.3f}% | dinámico: {DYNAMIC_THRESHOLD_PERCENT:.3f}%\n"
            f"{format_command_help()}"
        )
        tg_send_message(
            response,
            enabled=enabled,
            chat_id=chat_id,
            reply_markup=tg_commands_reply_markup(),
        )
        return

    if command == "/ping":
        tg_send_message("pong", enabled=enabled, chat_id=chat_id)
        return

    if command == "/status":
        pairs = CONFIG["pairs"]
        chats = get_registered_chat_ids()
        analysis_summary = "Sin historial"
        if LATEST_ANALYSIS and LATEST_ANALYSIS.rows_considered:
            analysis_summary = (
                f"SR: {LATEST_ANALYSIS.success_rate*100:.1f}%"
                f" ({LATEST_ANALYSIS.rows_considered} señales)"
            )
        response = (
            "Estado actual:\n"
            f"Threshold base: {CONFIG['threshold_percent']:.3f}% | dinámico: {DYNAMIC_THRESHOLD_PERCENT:.3f}%\n"
            f"Histórico: {analysis_summary}\n"
            f"Pares ({len(pairs)}): {', '.join(pairs) if pairs else 'sin pares'}\n"
            f"Chats registrados: {', '.join(chats) if chats else 'ninguno'}"
        )
        tg_send_message(response, enabled=enabled, chat_id=chat_id)
        return

    if command == "/capital":
        if not argument:
            capital = float(CONFIG.get("simulation_capital_quote", 0.0))
            tg_send_message(
                f"Capital simulado actual: {format_decimal_comma(capital, decimals=2)} USDT",
                enabled=enabled,
                chat_id=chat_id,
            )
            return
        if not ensure_admin(chat_id, enabled):
            return
        cleaned = argument.lower().replace("usdt", "").strip()
        cleaned = cleaned.replace(" ", "")
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif cleaned.count(",") == 1 and cleaned.count(".") == 0:
            cleaned = cleaned.replace(",", ".")
        try:
            value = float(cleaned)
        except ValueError:
            tg_send_message(
                "Valor inválido. Ej: /capital 2500 o /capital 2.500,50",
                enabled=enabled,
                chat_id=chat_id,
            )
            return
        if value <= 0:
            tg_send_message(
                "El capital debe ser mayor a 0.",
                enabled=enabled,
                chat_id=chat_id,
            )
            return
        with CONFIG_LOCK:
            CONFIG["simulation_capital_quote"] = value
        refresh_config_snapshot()
        tg_send_message(
            (
                "Nuevo capital simulado guardado: "
                f"{format_decimal_comma(value, decimals=2)} USDT"
            ),
            enabled=enabled,
            chat_id=chat_id,
        )
        return

    if command in ("/pairs", "/listapares"):
        pairs = CONFIG["pairs"]
        if not pairs:
            tg_send_message("No hay pares configurados.", enabled=enabled, chat_id=chat_id)
        else:
            formatted = "\n".join(f"- {p}" for p in pairs)
            tg_send_message(f"Pares actuales:\n{formatted}", enabled=enabled, chat_id=chat_id)
        return

    if command in ("/addpair", "/adherirpar"):
        if not ensure_admin(chat_id, enabled):
            return
        set_pending_action(chat_id, "addpair")
        default_quote = DEFAULT_QUOTE_ASSET
        prompt = (
            "Ingresá la cripto que querés adherir."
            f" Se agregará como BASE/{default_quote}."
        )
        tg_send_message(prompt, enabled=enabled, chat_id=chat_id)
        return

    if command in ("/delpair", "/eliminarpar"):
        if not ensure_admin(chat_id, enabled):
            return
        pairs = CONFIG["pairs"]
        if not pairs:
            tg_send_message("No hay pares configurados para eliminar.", enabled=enabled, chat_id=chat_id)
            return
        set_pending_action(chat_id, "delpair")
        keyboard = build_pairs_reply_keyboard(pairs)
        tg_send_message(
            (
                "Elegí el par a eliminar desde los botones o ingresá "
                "manual la cripto/par a remover."
            ),
            enabled=enabled,
            chat_id=chat_id,
            reply_markup=keyboard,
        )
        return

    if command in ("/test", "/senalprueba"):
        tg_send_message(build_test_signal_message(), enabled=enabled, chat_id=chat_id)
        return

    tg_send_message(
        "Comando no reconocido. Usá el botón de menú para ver las opciones disponibles.",
        enabled=enabled,
        chat_id=chat_id,
    )


def _reset_telegram_webhook_after_conflict(now: Optional[float] = None) -> None:
    """Intenta limpiar un webhook residual que impide la lectura por getUpdates."""

    global TELEGRAM_LAST_WEBHOOK_RESET_TS

    moment = now if now is not None else time.monotonic()
    if (
        TELEGRAM_LAST_WEBHOOK_RESET_TS
        and moment - TELEGRAM_LAST_WEBHOOK_RESET_TS < TELEGRAM_WEBHOOK_RESET_COOLDOWN_SECONDS
    ):
        log_event(
            "telegram.poll.reset_webhook.skip",
            reason="cooldown",
            cooldown_seconds=TELEGRAM_WEBHOOK_RESET_COOLDOWN_SECONDS,
        )
        return

    TELEGRAM_LAST_WEBHOOK_RESET_TS = moment
    try:
        tg_api_request("deleteWebhook")
    except HttpError as exc:
        log_event("telegram.poll.reset_webhook.error", error=str(exc))
    except Exception as exc:  # pragma: no cover - logging only
        log_event("telegram.poll.reset_webhook.exception", error=str(exc))
    else:
        log_event("telegram.poll.reset_webhook.success")


def tg_process_updates(enabled: bool = True) -> None:
    global TELEGRAM_LAST_UPDATE_ID, TELEGRAM_POLL_BACKOFF_UNTIL

    if not get_bot_token():
        return

    if TELEGRAM_POLL_BACKOFF_UNTIL:
        now = time.monotonic()
        if now < TELEGRAM_POLL_BACKOFF_UNTIL:
            return
        TELEGRAM_POLL_BACKOFF_UNTIL = 0.0

    params: Dict[str, int] = {}
    if TELEGRAM_LAST_UPDATE_ID:
        params["offset"] = TELEGRAM_LAST_UPDATE_ID + 1

    try:
        data = tg_api_request("getUpdates", params=params or None)
    except HttpError as e:
        if getattr(e, "status_code", None) == 409:
            TELEGRAM_POLL_BACKOFF_UNTIL = time.monotonic() + TELEGRAM_POLL_CONFLICT_BACKOFF_SECONDS
            log_event(
                "telegram.poll.conflict",
                error=str(e),
                backoff_seconds=TELEGRAM_POLL_CONFLICT_BACKOFF_SECONDS,
            )
            _reset_telegram_webhook_after_conflict()
        else:
            log_event("telegram.poll.error", error=str(e))
        return
    except Exception as e:
        log_event("telegram.poll.error", error=str(e))
        return

    for update in data.get("result", []):
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            TELEGRAM_LAST_UPDATE_ID = max(TELEGRAM_LAST_UPDATE_ID, update_id)
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            continue

        chat_id_str = register_telegram_chat(chat_id)
        if text.startswith("/"):
            set_pending_action(chat_id_str, None)
            parts = text.split(maxsplit=1)
            command = parts[0]
            argument = parts[1] if len(parts) > 1 else ""
            tg_handle_command(command, argument, chat_id_str, enabled)
            continue

        if tg_handle_pending_input(chat_id_str, text, enabled):
            continue



def ensure_telegram_polling_thread(enabled: bool, interval: float = 1.0) -> None:
    """Arranca un hilo dedicado a leer updates de Telegram frecuentemente."""
    global TELEGRAM_POLLING_THREAD

    if not enabled:
        return

    if TELEGRAM_POLLING_THREAD and TELEGRAM_POLLING_THREAD.is_alive():
        return

    def _loop():
        while True:
            try:
                tg_process_updates(enabled=True)
            except Exception as exc:  # pragma: no cover - logging only
                log_event("telegram.poll.exception", error=str(exc))
            time.sleep(max(0.5, interval))

    TELEGRAM_POLLING_THREAD = threading.Thread(
        target=_loop,
        name="telegram-polling",
        daemon=True,
    )
    TELEGRAM_POLLING_THREAD.start()

# =========================
# Modelo y Fees
# =========================
@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: int
    depth: Optional[DepthInfo] = None
    checksum: Optional[str] = None
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FeeSchedule:
    taker_fee_percent: float = 0.10
    maker_fee_percent: float = 0.0
    slippage_bps: float = 0.0
    native_token_discount_percent: float = 0.0

    @classmethod
    def from_config(cls, cfg: Dict, fallback: Optional["FeeSchedule"] = None) -> "FeeSchedule":
        fallback = fallback or FeeSchedule()
        taker = float(cfg.get("taker", cfg.get("taker_fee_percent", fallback.taker_fee_percent)))
        maker = float(cfg.get("maker", cfg.get("maker_fee_percent", fallback.maker_fee_percent)))
        slippage_bps = float(cfg.get("slippage_bps", fallback.slippage_bps))
        native_discount = float(cfg.get(
            "native_token_discount_percent",
            cfg.get("native_discount", fallback.native_token_discount_percent),
        ))
        return cls(
            taker_fee_percent=taker,
            maker_fee_percent=maker,
            slippage_bps=slippage_bps,
            native_token_discount_percent=native_discount,
        )


@dataclass
class VenueFees:
    venue: str
    default: FeeSchedule
    per_pair: Dict[str, FeeSchedule] = field(default_factory=dict)
    vip_level: str = "default"
    vip_multipliers: Dict[str, float] = field(default_factory=dict)
    native_token_discount_percent: float = 0.0
    last_updated: float = field(default_factory=lambda: time.time())

    @classmethod
    def from_config(cls, venue: str, cfg: Dict) -> "VenueFees":
        fees_cfg = cfg.get("fees") or {}
        base_default = FeeSchedule(taker_fee_percent=float(cfg.get("taker_fee_percent", 0.10)))
        if not fees_cfg:
            return cls(venue=venue, default=base_default)

        default_schedule = FeeSchedule.from_config(fees_cfg.get("default", {}), base_default)
        per_pair_cfg = fees_cfg.get("per_pair", {}) or {}
        per_pair: Dict[str, FeeSchedule] = {
            pair: FeeSchedule.from_config(data or {}, default_schedule)
            for pair, data in per_pair_cfg.items()
        }

        vip_multipliers = {str(k): float(v) for k, v in (fees_cfg.get("vip_multipliers", {}) or {}).items()}
        if "default" not in vip_multipliers:
            vip_multipliers["default"] = 1.0

        vip_level = str(fees_cfg.get("vip_level", "default"))
        native_discount = float(fees_cfg.get(
            "native_token_discount_percent",
            default_schedule.native_token_discount_percent,
        ))

        return cls(
            venue=venue,
            default=default_schedule,
            per_pair=per_pair,
            vip_level=vip_level,
            vip_multipliers=vip_multipliers,
            native_token_discount_percent=native_discount,
        )

    def _vip_multiplier(self) -> float:
        if not self.vip_multipliers:
            return 1.0
        if self.vip_level in self.vip_multipliers:
            return self.vip_multipliers[self.vip_level]
        return self.vip_multipliers.get("default", 1.0)

    def schedule_for_pair(self, pair: str) -> FeeSchedule:
        schedule = self.per_pair.get(pair, self.default)
        multiplier = self._vip_multiplier()
        taker = schedule.taker_fee_percent * multiplier
        maker = schedule.maker_fee_percent * multiplier
        native_discount = schedule.native_token_discount_percent or self.native_token_discount_percent
        if native_discount:
            taker = max(taker - native_discount, 0.0)
            maker = max(maker - native_discount, 0.0)
        return FeeSchedule(
            taker_fee_percent=taker,
            maker_fee_percent=maker,
            slippage_bps=schedule.slippage_bps,
            native_token_discount_percent=native_discount,
        )

    def register_pair_fee(self, pair: str, schedule: FeeSchedule) -> None:
        self.per_pair[pair] = schedule
        self.last_updated = time.time()

    @property
    def taker_fee_percent(self) -> float:
        """Expose the active taker fee for venues that require a flat rate."""

        return float(self.default.taker_fee_percent)


@dataclass
class TransferProfile:
    withdraw_fee: float = 0.0
    withdraw_percent: float = 0.0
    withdraw_minutes: float = 0.0
    deposit_fee: float = 0.0
    deposit_percent: float = 0.0
    deposit_minutes: float = 0.0

    @classmethod
    def from_config(cls, cfg: Dict) -> "TransferProfile":
        return cls(
            withdraw_fee=float(cfg.get("withdraw_fee", 0.0)),
            withdraw_percent=float(cfg.get("withdraw_percent", 0.0)),
            withdraw_minutes=float(cfg.get("withdraw_minutes", cfg.get("withdraw_eta_minutes", 0.0))),
            deposit_fee=float(cfg.get("deposit_fee", 0.0)),
            deposit_percent=float(cfg.get("deposit_percent", 0.0)),
            deposit_minutes=float(cfg.get("deposit_minutes", cfg.get("deposit_eta_minutes", 0.0))),
        )


@dataclass
class VenueTransfers:
    assets: Dict[str, TransferProfile] = field(default_factory=dict)

    def profile(self, asset: str) -> Optional[TransferProfile]:
        asset_key = asset.upper()
        if asset_key in self.assets:
            return self.assets[asset_key]
        return self.assets.get(asset)


@dataclass
class TransferEstimate:
    total_cost_quote: float = 0.0
    total_minutes: float = 0.0
    base_asset_loss: float = 0.0
    quote_asset_loss: float = 0.0


def apply_slippage(price: float, slippage_bps: float, side: str) -> float:
    if price <= 0:
        return 0.0
    if slippage_bps <= 0:
        return price
    factor = slippage_bps / 10_000.0
    side = side.lower()
    if side == "buy":
        return price * (1.0 + factor)
    return max(price * (1.0 - factor), 0.0)


def compute_base_quantity(capital_quote: float, buy_price: float, buy_slippage_bps: float) -> float:
    adjusted_buy = apply_slippage(buy_price, buy_slippage_bps, "buy")
    if adjusted_buy <= 0 or capital_quote <= 0:
        return 0.0
    return capital_quote / adjusted_buy


def update_fee_registry(venue_fees: VenueFees, pairs: List[str]) -> None:
    for pair in pairs:
        schedule = venue_fees.schedule_for_pair(pair)
        key = (venue_fees.venue, pair)
        current = round(schedule.taker_fee_percent, 8)
        previous = FEE_REGISTRY.get(key)
        if previous is None or not math.isclose(previous, current, rel_tol=1e-6):
            FEE_REGISTRY[key] = current
            prev_fmt = f"{previous:.4f}" if previous is not None else "n/a"
            print(f"[FEE] {venue_fees.venue} {pair} taker fee actualizado: {prev_fmt} -> {current:.4f}")


def build_fee_map(pairs: List[str]) -> Dict[str, VenueFees]:
    fee_map: Dict[str, VenueFees] = {}
    for vname, vcfg in CONFIG["venues"].items():
        if not vcfg.get("enabled", False):
            continue
        venue_fees = VenueFees.from_config(vname, vcfg)
        fee_map[vname] = venue_fees
        update_fee_registry(venue_fees, pairs)
    return fee_map


def build_transfer_profiles() -> Dict[str, VenueTransfers]:
    profiles: Dict[str, VenueTransfers] = {}
    for vname, vcfg in CONFIG["venues"].items():
        if not vcfg.get("enabled", False):
            continue
        transfers_cfg = vcfg.get("transfers") or {}
        assets: Dict[str, TransferProfile] = {}
        for asset, cfg in transfers_cfg.items():
            assets[asset.upper()] = TransferProfile.from_config(cfg or {})
        if assets:
            profiles[vname] = VenueTransfers(assets=assets)
    return profiles


def _asset_transfer_loss(
    amount: float,
    withdraw_profile: Optional[TransferProfile],
    deposit_profile: Optional[TransferProfile],
) -> Tuple[float, float]:
    if amount <= 0:
        return 0.0, 0.0
    loss_units = 0.0
    minutes = 0.0
    if withdraw_profile:
        loss_units += withdraw_profile.withdraw_fee
        loss_units += (withdraw_profile.withdraw_percent / 100.0) * amount
        minutes += withdraw_profile.withdraw_minutes
    if deposit_profile:
        loss_units += deposit_profile.deposit_fee
        loss_units += (deposit_profile.deposit_percent / 100.0) * amount
        minutes += deposit_profile.deposit_minutes
    return loss_units, minutes


def estimate_round_trip_transfer_cost(
    pair: str,
    buy_venue: str,
    sell_venue: str,
    base_qty: float,
    executed_sell_price: float,
    transfers: Dict[str, VenueTransfers],
) -> TransferEstimate:
    if base_qty <= 0 or executed_sell_price <= 0:
        return TransferEstimate()

    base_asset, quote_asset = pair.split("/")
    buy_profiles = transfers.get(buy_venue)
    sell_profiles = transfers.get(sell_venue)

    base_withdraw = buy_profiles.profile(base_asset) if buy_profiles else None
    base_deposit = sell_profiles.profile(base_asset) if sell_profiles else None
    base_loss_units, base_minutes = _asset_transfer_loss(base_qty, base_withdraw, base_deposit)

    quote_amount = base_qty * executed_sell_price
    quote_withdraw = sell_profiles.profile(quote_asset) if sell_profiles else None
    quote_deposit = buy_profiles.profile(quote_asset) if buy_profiles else None
    quote_loss_units, quote_minutes = _asset_transfer_loss(quote_amount, quote_withdraw, quote_deposit)

    total_cost_quote = base_loss_units * executed_sell_price + quote_loss_units
    total_minutes = base_minutes + quote_minutes
    return TransferEstimate(
        total_cost_quote=total_cost_quote,
        total_minutes=total_minutes,
        base_asset_loss=base_loss_units,
        quote_asset_loss=quote_loss_units,
    )


def simulate_inventory_rebalance(
    pair: str,
    buy_venue: str,
    sell_venue: str,
    base_qty: float,
    executed_sell_price: float,
    transfers: Dict[str, VenueTransfers],
) -> Tuple[float, float]:
    cfg = CONFIG.get("inventory_management", {})
    if not cfg or not cfg.get("enabled", False):
        return 0.0, 0.0
    frequency = max(1, int(cfg.get("rebalance_frequency_trades", 1)))
    reverse = estimate_round_trip_transfer_cost(
        pair,
        sell_venue,
        buy_venue,
        base_qty,
        executed_sell_price,
        transfers,
    )
    return reverse.total_cost_quote / frequency, reverse.total_minutes


def split_pair(pair: str) -> Tuple[str, str]:
    if "/" in pair:
        base, quote = pair.split("/", 1)
    elif "-" in pair:
        base, quote = pair.split("-", 1)
    else:
        midpoint = len(pair) // 2
        base, quote = pair[:midpoint], pair[midpoint:]
    return base.upper(), quote.upper()


def build_trade_link(venue: str, pair: str) -> Optional[str]:
    base, quote = split_pair(pair)
    venue_cfg = CONFIG.get("venues", {}).get(venue.lower())
    if venue_cfg:
        trade_links = venue_cfg.get("trade_links") or {}
        template = trade_links.get(pair.upper()) or trade_links.get("default")
        if template:
            try:
                return template.format(pair=pair.upper(), base=base, quote=quote)
            except Exception:
                pass
    venue = venue.lower()
    if venue == "binance":
        return f"https://www.binance.com/en/trade/{base}_{quote}?type=spot"
    if venue == "bybit":
        return f"https://www.bybit.com/en/trade/spot/{base}/{quote}"
    if venue == "kucoin":
        return f"https://www.kucoin.com/trade/{base}-{quote}"
    if venue == "okx":
        return f"https://www.okx.com/trade-spot/{base}-{quote}"
    return None


def build_trade_link_items(buy_venue: str, sell_venue: str, pair: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    buy_link = build_trade_link(buy_venue, pair)
    if buy_link:
        items.append({"label": f"Comprar en {buy_venue.title()}", "url": buy_link})
    sell_link = build_trade_link(sell_venue, pair)
    if sell_link:
        items.append({"label": f"Vender en {sell_venue.title()}", "url": sell_link})
    return items


def is_strategy_enabled(name: str) -> bool:
    strategies = CONFIG.get("strategies") or {}
    return bool(strategies.get(name, False))


def configured_p2p_pairs() -> Dict[str, Dict[str, Dict[str, Any]]]:
    venues_cfg = CONFIG.get("venues", {}) or {}
    configured: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for venue, cfg in venues_cfg.items():
        p2p_cfg = cfg.get("p2p") or {}
        if not p2p_cfg.get("enabled", False):
            continue
        pairs_cfg = p2p_cfg.get("pairs") or {}
        venue_pairs: Dict[str, Dict[str, Any]] = {}
        for pair, pcfg in pairs_cfg.items():
            if not pair:
                continue
            venue_pairs[pair.strip().upper()] = pcfg or {}
        if venue_pairs:
            configured[venue] = venue_pairs
    return configured


def market_rules_for(venue: str, pair: str) -> Dict[str, float]:
    rules_cfg = CONFIG.get("market_rules") or {}
    venue_rules = rules_cfg.get(venue) if isinstance(rules_cfg, dict) else None
    if not isinstance(venue_rules, dict):
        return {}
    data = venue_rules.get(pair)
    return data if isinstance(data, dict) else {}


def validate_market_trade(
    venue: str,
    pair: str,
    base_qty: float,
    price: float,
    tolerance: float = 1e-9,
) -> Tuple[bool, str]:
    rules = market_rules_for(venue, pair)
    if not rules:
        return True, ""

    min_qty = float(rules.get("min_qty", 0.0) or 0.0)
    if min_qty > 0 and base_qty + tolerance < min_qty:
        return False, "min_notional"

    min_notional = float(rules.get("min_notional", 0.0) or 0.0)
    notional = base_qty * price
    if min_notional > 0 and notional + tolerance < min_notional:
        return False, "min_notional"

    step_size = float(rules.get("step_size", 0.0) or 0.0)
    if step_size > 0:
        steps = round(base_qty / step_size)
        aligned = steps * step_size
        if abs(aligned - base_qty) > max(step_size * 1e-3, tolerance):
            return False, "min_notional"

    return True, ""


def get_p2p_fee_percent(venue: str, asset: str) -> float:
    p2p_cfg = CONFIG.get("venues", {}).get(venue, {}).get("p2p") or {}
    fees_cfg = p2p_cfg.get("fees") or {}
    default = float(fees_cfg.get("default_percent", fees_cfg.get("fee_percent", 0.0)) or 0.0)
    per_asset = fees_cfg.get("per_asset_percent") or {}
    try:
        return float(per_asset.get(asset.upper(), default))
    except (TypeError, ValueError):
        return default


def get_p2p_min_notional(venue: str, asset: str) -> float:
    p2p_cfg = CONFIG.get("venues", {}).get(venue, {}).get("p2p") or {}
    min_cfg = p2p_cfg.get("min_notional_usdt") or {}
    try:
        return float(min_cfg.get(asset.upper(), 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_p2p_payment_filters(venue: str) -> List[str]:
    p2p_cfg = CONFIG.get("venues", {}).get(venue, {}).get("p2p") or {}
    filters = p2p_cfg.get("payment_methods") or []
    return [f for f in filters if isinstance(f, str) and f]


def validate_p2p_notional(venue: str, asset: str, quote_amount: float) -> Tuple[bool, str]:
    min_notional = get_p2p_min_notional(venue, asset)
    if min_notional > 0 and quote_amount < min_notional:
        return False, "min_notional"
    return True, ""


def emit_p2p_log(
    venue: str,
    asset: str,
    fiat: str,
    side: str,
    quote: Quote,
    offers: int,
    filters: Iterable[str],
) -> None:
    filters_label = ",".join(sorted(set(filters))) or "any"
    price = quote.ask if side.upper() == "BUY" else quote.bid
    print(
        "[P2P] "
        f"{asset}: venue={venue} fiat={fiat} offers={offers} side={side.upper()} "
        f"filtros={filters_label} elegido={price:.6f}"
    )


def build_p2p_quote_index(
    pair_quotes: Dict[str, Dict[str, Quote]],
) -> Dict[str, Dict[str, Dict[str, Quote]]]:
    index: Dict[str, Dict[str, Dict[str, Quote]]] = {}
    for pair, venues in pair_quotes.items():
        base, quote = split_pair(pair)
        for venue, q in venues.items():
            if str(getattr(q, "source", "")).lower() != "p2p":
                continue
            venue_entry = index.setdefault(venue, {})
            fiat_entry = venue_entry.setdefault(quote, {})
            fiat_entry[base] = q
    return index


def build_effective_p2p_quotes(
    p2p_index: Dict[str, Dict[str, Dict[str, Quote]]],
    stable_asset: str = DEFAULT_QUOTE_ASSET,
) -> Dict[str, Dict[str, Quote]]:
    effective: Dict[str, Dict[str, Quote]] = {}
    for venue, fiat_map in p2p_index.items():
        payment_filters = get_p2p_payment_filters(venue)
        for fiat, assets in fiat_map.items():
            stable_quote = assets.get(stable_asset)
            if not stable_quote:
                continue
            stable_bid = max(stable_quote.bid, 1e-12)
            stable_ask = max(stable_quote.ask, 1e-12)
            offers_meta = stable_quote.metadata.get("offers", {})
            offers_buy = int(offers_meta.get("BUY", 0)) if isinstance(offers_meta, dict) else 0
            offers_sell = int(offers_meta.get("SELL", 0)) if isinstance(offers_meta, dict) else 0
            emit_p2p_log(venue, stable_asset, fiat, "BUY", stable_quote, offers_buy, payment_filters)
            emit_p2p_log(venue, stable_asset, fiat, "SELL", stable_quote, offers_sell, payment_filters)
            for asset, quote in assets.items():
                if asset == stable_asset:
                    continue
                bid = quote.bid / stable_ask if stable_ask > 0 else 0.0
                ask = quote.ask / stable_bid if stable_bid > 0 else 0.0
                ts = min(int(stable_quote.ts), int(quote.ts))
                metadata = dict(quote.metadata)
                metadata.update({"fiat": fiat, "source_pair": quote.symbol})
                asset_quote = Quote(
                    f"{asset}/{stable_asset}",
                    bid,
                    ask,
                    ts,
                    source="p2p_effective",
                    metadata=metadata,
                )
                offers_meta = quote.metadata.get("offers", {}) if isinstance(quote.metadata, dict) else {}
                offers_buy = int(offers_meta.get("BUY", 0)) if isinstance(offers_meta, dict) else 0
                offers_sell = int(offers_meta.get("SELL", 0)) if isinstance(offers_meta, dict) else 0
                emit_p2p_log(venue, asset, fiat, "BUY", quote, offers_buy, payment_filters)
                emit_p2p_log(venue, asset, fiat, "SELL", quote, offers_sell, payment_filters)
                effective.setdefault(asset, {})[venue] = asset_quote
    return effective

# =========================
# Adapters de Exchanges
# =========================
class ExchangeAdapter:
    name: str
    depth_supported: bool = False

    def normalize_symbol(self, pair: str) -> str:
        raise NotImplementedError

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        raise NotImplementedError

    def fetch_depth_snapshot(self, pair: str) -> Optional[DepthInfo]:
        return None

    def _test_mode_config(self) -> Dict[str, Any]:
        cfg = CONFIG.get("test_mode") or {}
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def _is_test_mode_enabled(self) -> bool:
        return bool(self._test_mode_config().get("enabled", False))

    def _test_mode_paused(self) -> bool:
        cfg = self._test_mode_config()
        return bool(cfg.get("pause_live_requests", False))

    def _test_mode_quote(self, pair: str) -> Optional[Quote]:
        if not self._is_test_mode_enabled():
            return None
        cfg = self._test_mode_config()
        venues_cfg = cfg.get("venues") or {}
        if not isinstance(venues_cfg, dict):
            return None
        venue_cfg = venues_cfg.get(self.name, {})
        pairs_cfg = venue_cfg.get("pairs") or {}
        if not isinstance(pairs_cfg, dict):
            return None
        data = pairs_cfg.get(pair)
        if not isinstance(data, dict):
            return None
        bid = safe_float(data.get("bid"))
        ask = safe_float(data.get("ask"))
        if bid <= 0 or ask <= 0 or bid >= ask:
            return None
        ts_raw = data.get("ts")
        ts = int(ts_raw) if ts_raw is not None else current_millis()
        source = str(data.get("source") or "test")
        return Quote(
            self.normalize_symbol(pair),
            bid,
            ask,
            ts,
            source=source,
        )

    def _endpoint_config(self, endpoint: str, default: str) -> Tuple[str, List[str]]:
        venue_cfg = CONFIG.get("venues", {}).get(self.name, {})
        endpoints_cfg = venue_cfg.get("endpoints", {})
        endpoint_cfg = endpoints_cfg.get(endpoint, {}) if isinstance(endpoints_cfg, dict) else {}
        primary = str(endpoint_cfg.get("primary") or default)
        fallbacks = [
            str(url)
            for url in endpoint_cfg.get("fallbacks", [])
            if url
        ]
        return primary, fallbacks

    def _p2p_config(self) -> Dict[str, Any]:
        venue_cfg = CONFIG.get("venues", {}).get(self.name, {})
        p2p_cfg = venue_cfg.get("p2p") or {}
        return p2p_cfg if isinstance(p2p_cfg, dict) else {}

    def _p2p_pair_config(self, pair: str) -> Optional[Dict[str, Any]]:
        p2p_cfg = self._p2p_config()
        if not p2p_cfg.get("enabled", False):
            return None
        pairs_cfg = p2p_cfg.get("pairs") or {}
        if not isinstance(pairs_cfg, dict):
            return None
        return pairs_cfg.get(pair)

    def _integrity_key(self, symbol: str, endpoint: str) -> str:
        return f"{self.name}:{symbol}:{endpoint}"

    def _offline_quote(self, pair: str, reason: Optional[str] = None) -> Optional[Quote]:
        offline_cfg = CONFIG.get("offline_quotes") or {}
        data = offline_cfg.get(pair)
        if not data:
            return None
        bid = safe_float(data.get("bid"))
        ask = safe_float(data.get("ask"))
        if bid <= 0 or ask <= 0 or bid >= ask:
            return None
        if reason:
            print(f"[{self.name}] usando cotización offline para {pair}: {reason}")
        return Quote(
            self.normalize_symbol(pair),
            bid,
            ask,
            current_millis(),
            source=str(data.get("source") or "offline"),
        )

    def get_depth(self, pair: str) -> Optional[DepthInfo]:
        if not self.depth_supported:
            return None
        if self._is_test_mode_enabled() and self._test_mode_paused():
            return None
        symbol = self.normalize_symbol(pair)
        cache_key = (self.name, symbol)
        cached = DEPTH_CACHE.get(cache_key)
        if cached:
            return cached
        depth = self.fetch_depth_snapshot(pair)
        if depth:
            DEPTH_CACHE.set(cache_key, depth)
        return depth

    def _attach_depth(self, pair: str, quote: Optional[Quote]) -> Optional[Quote]:
        depth = self.get_depth(pair)
        if not depth:
            return quote
        symbol = self.normalize_symbol(pair)
        if quote is None:
            return Quote(
                symbol,
                depth.best_bid,
                depth.best_ask,
                depth.ts,
                depth=depth,
                checksum=depth.checksum,
                source="depth",
            )
        quote.depth = depth
        if depth.best_bid > 0:
            quote.bid = max(quote.bid, depth.best_bid)
        if depth.best_ask > 0:
            quote.ask = min(quote.ask, depth.best_ask)
        quote.ts = max(quote.ts, depth.ts)
        return quote


class Binance(ExchangeAdapter):
    name = "binance"
    depth_supported = True

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        test_quote = self._test_mode_quote(pair)
        if test_quote is not None:
            return self._attach_depth(pair, test_quote)
        if self._is_test_mode_enabled() and self._test_mode_paused():
            return self._attach_depth(pair, self._offline_quote(pair, reason="test_mode_paused"))
        p2p_cfg = self._p2p_pair_config(pair)
        if p2p_cfg:
            p2p_quote = self._fetch_p2p_quote(pair, p2p_cfg)
            if p2p_quote:
                return p2p_quote
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "ticker", "https://api.binance.com/api/v3/ticker/bookTicker"
        )
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"symbol": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
                fallback_endpoints=[(fallback, {"symbol": sym}) for fallback in fallbacks],
            )
            data = response.data
            bid = safe_float(data.get("bidPrice"))
            ask = safe_float(data.get("askPrice"))
            if bid <= 0 or ask <= 0 or bid >= ask:
                raise HttpError("Precios inválidos en ticker")
            ts_ms = safe_float(data.get("time"))
            if ts_ms > 0:
                ts_val = ensure_fresh_timestamp(int(ts_ms), response.received_ts, "binance:ticker")
            else:
                ts_val = response.received_ts
            quote = Quote(sym, bid, ask, int(ts_val), checksum=response.checksum, source="bookTicker")
        except Exception as exc:
            print(f"[binance] ticker fallback {pair}: {exc}")
            quote = self._offline_quote(pair, reason=str(exc))
        quote = self._attach_depth(pair, quote)
        if quote and quote.bid >= quote.ask:
            return None
        return quote

    def fetch_depth_snapshot(self, pair: str) -> Optional[DepthInfo]:
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "depth", "https://api.binance.com/api/v3/depth"
        )
        try:
            response = http_get_json(
                url,
                params={"symbol": sym, "limit": 20},
                integrity_key=self._integrity_key(sym, "depth"),
                fallback_endpoints=[(fallback, {"symbol": sym, "limit": 20}) for fallback in fallbacks],
            )
            bids = response.data.get("bids") or []
            asks = response.data.get("asks") or []
            if not bids or not asks:
                raise HttpError("Depth vacío")
            best_bid = safe_float(bids[0][0])
            best_ask = safe_float(asks[0][0])
            bid_volume = sum(safe_float(b[1]) for b in bids)
            ask_volume = sum(safe_float(a[1]) for a in asks)
            levels = min(len(bids), len(asks))
            ts_val = response.received_ts
            return DepthInfo(best_bid, best_ask, bid_volume, ask_volume, levels, ts_val, response.checksum)
        except Exception as exc:
            print(f"[binance] depth error {pair}: {exc}")
            return None

    def _fetch_p2p_quote(self, pair: str, cfg: Dict[str, Any]) -> Optional[Quote]:
        p2p_cfg = self._p2p_config()
        endpoint = str(p2p_cfg.get("endpoint") or "").strip()
        if not endpoint:
            return None
        asset = str(cfg.get("asset") or pair.split("/")[0]).upper()
        fiat = str(cfg.get("fiat") or pair.split("/")[1]).upper()
        pay_types_raw = cfg.get("pay_types") or []
        pay_types = [pt for pt in pay_types_raw if isinstance(pt, str) and pt]
        rows = max(1, int(p2p_cfg.get("rows", 10)))
        merchant_types = p2p_cfg.get("merchant_types") or []

        offers_info: Dict[str, int] = {}

        def _fetch_side(trade_type: str) -> Optional[float]:
            payload: Dict[str, Any] = {
                "page": 1,
                "rows": rows,
                "payTypes": pay_types,
                "asset": asset,
                "tradeType": trade_type,
                "fiat": fiat,
                "publisherType": None,
            }
            if merchant_types:
                payload["merchantCheck"] = True
                payload["publisherType"] = "MERCHANT"

            fallbacks: List[Tuple[str, Optional[dict]]] = []
            for fb in p2p_cfg.get("fallbacks", []) or []:
                fb_url = str(fb).strip()
                if fb_url:
                    fallbacks.append((fb_url, payload))

            try:
                response = http_post_json(endpoint, payload, fallback_endpoints=fallbacks)
            except Exception as exc:
                print(f"[binance] p2p {pair} {trade_type} error: {exc}")
                return None

            data = response.data.get("data") or []
            if not isinstance(data, list) or not data:
                return None

            prices: List[float] = []
            for item in data:
                adv = item.get("adv") if isinstance(item, dict) else None
                price_str = adv.get("price") if isinstance(adv, dict) else None
                price = safe_float(price_str)
                if price > 0:
                    prices.append(price)
            offers_info[trade_type.upper()] = len(data)
            if not prices:
                return None
            if trade_type.upper() == "BUY":
                return min(prices)
            return max(prices)

        ask_price = _fetch_side("BUY")
        bid_price = _fetch_side("SELL")
        if ask_price is None or bid_price is None:
            return None
        if bid_price <= 0 or ask_price <= 0 or bid_price >= ask_price:
            return None

        symbol = self.normalize_symbol(pair)
        metadata = {
            "asset": asset,
            "fiat": fiat,
            "offers": offers_info,
            "pay_types": pay_types,
        }
        return Quote(symbol, bid_price, ask_price, current_millis(), source="p2p", metadata=metadata)

class Bybit(ExchangeAdapter):
    name = "bybit"
    depth_supported = True

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        test_quote = self._test_mode_quote(pair)
        if test_quote is not None:
            return self._attach_depth(pair, test_quote)
        if self._is_test_mode_enabled() and self._test_mode_paused():
            return self._attach_depth(pair, self._offline_quote(pair, reason="test_mode_paused"))
        p2p_cfg = self._p2p_pair_config(pair)
        if p2p_cfg:
            p2p_quote = self._fetch_p2p_quote(pair, p2p_cfg)
            if p2p_quote:
                return p2p_quote
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "ticker", "https://api.bybit.com/v5/market/tickers"
        )
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"category": "spot", "symbol": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
                fallback_endpoints=[
                    (fallback, {"category": "spot", "symbol": sym}) for fallback in fallbacks
                ],
            )
            result = response.data.get("result") or {}
            items = result.get("list") or []
            if not items:
                raise HttpError("Ticker vacío")
            item = items[0]
            bid = safe_float(item.get("bid1Price"))
            ask = safe_float(item.get("ask1Price"))
            if bid <= 0 or ask <= 0 or bid >= ask:
                raise HttpError("Precios inválidos en ticker")
            ts_field = safe_float(response.data.get("time") or item.get("time") or item.get("t"))
            if ts_field > 0:
                ts_val = ensure_fresh_timestamp(int(ts_field), response.received_ts, "bybit:ticker")
            else:
                ts_val = response.received_ts
            quote = Quote(sym, bid, ask, int(ts_val), checksum=response.checksum, source="ticker")
        except Exception as exc:
            print(f"[bybit] ticker fallback {pair}: {exc}")
            quote = self._offline_quote(pair, reason=str(exc))
        quote = self._attach_depth(pair, quote)
        if quote and quote.bid >= quote.ask:
            return None
        return quote

    def fetch_depth_snapshot(self, pair: str) -> Optional[DepthInfo]:
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "depth", "https://api.bybit.com/v5/market/orderbook"
        )
        try:
            response = http_get_json(
                url,
                params={"category": "spot", "symbol": sym, "limit": 25},
                integrity_key=self._integrity_key(sym, "depth"),
                fallback_endpoints=[
                    (
                        fallback,
                        {"category": "spot", "symbol": sym, "limit": 25},
                    )
                    for fallback in fallbacks
                ],
            )
            result = response.data.get("result") or {}
            bids = result.get("b") or []
            asks = result.get("a") or []
            if not bids or not asks:
                raise HttpError("Depth vacío")
            best_bid = safe_float(bids[0][0])
            best_ask = safe_float(asks[0][0])
            bid_volume = sum(safe_float(entry[1]) for entry in bids)
            ask_volume = sum(safe_float(entry[1]) for entry in asks)
            levels = min(len(bids), len(asks))
            ts_field = safe_float(result.get("ts") or response.data.get("time"))
            if ts_field > 0:
                ts_val = ensure_fresh_timestamp(int(ts_field), response.received_ts, "bybit:depth")
            else:
                ts_val = response.received_ts
            return DepthInfo(best_bid, best_ask, bid_volume, ask_volume, levels, int(ts_val), response.checksum)
        except Exception as exc:
            print(f"[bybit] depth error {pair}: {exc}")
            return None

    def _fetch_p2p_quote(self, pair: str, cfg: Dict[str, Any]) -> Optional[Quote]:
        p2p_cfg = self._p2p_config()
        endpoint = str(p2p_cfg.get("endpoint") or "").strip()
        if not endpoint:
            return None
        asset = str(cfg.get("asset") or pair.split("/")[0]).upper()
        fiat = str(cfg.get("fiat") or pair.split("/")[1]).upper()
        rows = max(1, int(p2p_cfg.get("rows", 10)))
        ask_side = str(cfg.get("ask_side", "1"))
        bid_side = str(cfg.get("bid_side", "0"))

        offers_info: Dict[str, int] = {}

        def _fetch_side(side: str, prefer_min: bool) -> Optional[float]:
            payload: Dict[str, Any] = {
                "userId": "",
                "tokenId": asset,
                "currencyId": fiat,
                "payment": [],
                "side": side,
                "size": str(rows),
                "page": "1",
                "amount": "",
            }
            try:
                response = http_post_json(endpoint, payload)
            except Exception as exc:
                print(f"[bybit] p2p {pair} side={side} error: {exc}")
                return None

            data = response.data.get("result") or {}
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                return None

            prices: List[float] = []
            for item in items:
                price = safe_float(item.get("price") if isinstance(item, dict) else None)
                if price > 0:
                    prices.append(price)
            offers_info[side.upper()] = len(items)
            if not prices:
                return None
            return min(prices) if prefer_min else max(prices)

        ask_price = _fetch_side(ask_side, True)
        bid_price = _fetch_side(bid_side, False)
        if ask_price is None or bid_price is None:
            return None
        if bid_price <= 0 or ask_price <= 0 or bid_price >= ask_price:
            return None

        symbol = self.normalize_symbol(pair)
        metadata = {
            "asset": asset,
            "fiat": fiat,
            "offers": offers_info,
        }
        return Quote(symbol, bid_price, ask_price, current_millis(), source="p2p", metadata=metadata)


class GenericP2PMarketplace(ExchangeAdapter):
    depth_supported = False

    def __init__(self, venue_name: str):
        self.name = venue_name

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "_")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        test_quote = self._test_mode_quote(pair)
        if test_quote is not None:
            return test_quote
        if self._is_test_mode_enabled() and self._test_mode_paused():
            return self._offline_quote(pair, reason="test_mode_paused")

        cfg = self._p2p_pair_config(pair)
        if not cfg:
            return None
        quote = self._fetch_gateway_quote(pair, cfg)
        if quote and quote.bid >= quote.ask:
            return None
        return quote

    def _fetch_gateway_quote(self, pair: str, cfg: Dict[str, Any]) -> Optional[Quote]:
        base_asset, quote_asset = split_pair(pair)
        asset = str(cfg.get("asset") or base_asset).upper()
        fiat = str(cfg.get("fiat") or quote_asset).upper()
        context = {
            "pair": pair.upper(),
            "base": base_asset,
            "quote": quote_asset,
            "asset": asset,
            "fiat": fiat,
        }

        p2p_cfg = self._p2p_config()
        method = str(cfg.get("method") or p2p_cfg.get("method") or "GET").upper()
        endpoint = _format_with_context(
            cfg.get("endpoint") or p2p_cfg.get("endpoint"),
            context,
        )
        headers_cfg: Dict[str, str] = {}
        headers_cfg.update(_format_with_context(p2p_cfg.get("headers") or {}, context))
        headers_cfg.update(_format_with_context(cfg.get("headers") or {}, context))
        params: Dict[str, Any] = {}
        params.update(_format_with_context(p2p_cfg.get("params") or {}, context))
        params.update(_format_with_context(cfg.get("params") or {}, context))
        payload: Dict[str, Any] = {}
        payload.update(_format_with_context(p2p_cfg.get("payload") or {}, context))
        payload.update(_format_with_context(cfg.get("payload") or {}, context))
        fallbacks_cfg = cfg.get("fallbacks") or p2p_cfg.get("fallbacks") or []
        timeout = int(cfg.get("timeout", p2p_cfg.get("timeout", 8)))
        retries = int(cfg.get("retries", p2p_cfg.get("retries", 3)))

        response: Optional[HttpJsonResponse] = None
        symbol = self.normalize_symbol(pair)
        integrity_key = self._integrity_key(symbol, "p2p_gateway")

        def _format_fallbacks() -> List[Tuple[str, Optional[dict]]]:
            formatted: List[Tuple[str, Optional[dict]]] = []
            for fb in fallbacks_cfg:
                if isinstance(fb, str):
                    formatted.append((
                        _format_with_context(fb, context),
                        params if method == "GET" else payload,
                    ))
                    continue
                if isinstance(fb, dict):
                    fb_url = _format_with_context(fb.get("url"), context)
                    if not fb_url:
                        continue
                    if method == "GET":
                        fb_params = dict(params)
                        fb_params.update(
                            _format_with_context(fb.get("params") or {}, context)
                        )
                        formatted.append((fb_url, fb_params))
                    else:
                        fb_payload = dict(payload)
                        fb_payload.update(
                            _format_with_context(fb.get("payload") or {}, context)
                        )
                        formatted.append((fb_url, fb_payload))
            return formatted

        try:
            if endpoint:
                if method == "GET":
                    response = http_get_json(
                        endpoint,
                        params=params or None,
                        timeout=timeout,
                        retries=retries,
                        integrity_key=integrity_key,
                        fallback_endpoints=_format_fallbacks(),
                        headers=headers_cfg or None,
                    )
                else:
                    response = http_post_json(
                        endpoint,
                        payload=payload or None,
                        timeout=timeout,
                        retries=retries,
                        headers=headers_cfg or None,
                        fallback_endpoints=_format_fallbacks(),
                    )
        except Exception as exc:
            print(f"[{self.name}] p2p {pair} error: {exc}")
            response = None

        data = response.data if response else None
        base_path = _normalize_json_path(
            cfg.get("data_path") or p2p_cfg.get("data_path"),
            context,
        )
        if base_path and data is not None:
            data = _extract_json_path(data, base_path)

        invert_sides = bool(cfg.get("invert_sides") or p2p_cfg.get("invert_sides", False))

        raw_bid = None
        raw_ask = None
        if data is not None:
            raw_bid = _extract_json_path(
                data,
                _normalize_json_path(
                    cfg.get("bid_path") or p2p_cfg.get("bid_path"),
                    context,
                ),
            )
            raw_ask = _extract_json_path(
                data,
                _normalize_json_path(
                    cfg.get("ask_path") or p2p_cfg.get("ask_path"),
                    context,
                ),
            )

        if invert_sides:
            raw_bid, raw_ask = raw_ask, raw_bid

        bid = safe_float(raw_bid)
        ask = safe_float(raw_ask)

        static_quote = cfg.get("static_quote") or {}
        if bid <= 0:
            bid = safe_float(static_quote.get("bid"))
        if ask <= 0:
            ask = safe_float(static_quote.get("ask"))

        if bid <= 0 or ask <= 0:
            return None

        invert_price = bool(cfg.get("invert_price") or p2p_cfg.get("invert_price", False))
        if invert_price:
            bid = (1.0 / bid) if bid > 0 else 0.0
            ask = (1.0 / ask) if ask > 0 else 0.0

        scale = safe_float(cfg.get("price_scale") or p2p_cfg.get("price_scale") or 1.0, 1.0)
        if scale != 1.0:
            bid *= scale
            ask *= scale

        if bid <= 0 or ask <= 0:
            return None

        if bid >= ask:
            spread_bps = safe_float(cfg.get("spread_adjust_bps") or p2p_cfg.get("spread_adjust_bps"))
            if spread_bps > 0:
                spread_factor = spread_bps / 10_000.0
                ask = ask * (1.0 + spread_factor)
                bid = bid * (1.0 - spread_factor)
            if bid >= ask:
                return None

        ts_path = _normalize_json_path(
            cfg.get("timestamp_path") or p2p_cfg.get("timestamp_path"),
            context,
        )
        ts_value = 0.0
        if ts_path and response is not None:
            ts_value = safe_float(_extract_json_path(response.data, ts_path))
        timestamp_ms = current_millis()
        if ts_value > 0:
            if ts_value > 1_000_000_000_000:
                timestamp_ms = int(ts_value)
            else:
                timestamp_ms = int(ts_value * 1000)

        offers_meta: Dict[str, Any] = {}
        offers_cfg = cfg.get("offers_path") or p2p_cfg.get("offers_path")
        if isinstance(offers_cfg, dict) and data is not None:
            buy_path = _normalize_json_path(offers_cfg.get("buy"), context)
            sell_path = _normalize_json_path(offers_cfg.get("sell"), context)
            if buy_path:
                buy_val = _extract_json_path(data, buy_path)
                try:
                    offers_meta["BUY"] = int(buy_val)
                except Exception:
                    pass
            if sell_path:
                sell_val = _extract_json_path(data, sell_path)
                try:
                    offers_meta["SELL"] = int(sell_val)
                except Exception:
                    pass

        metadata = {
            "asset": asset,
            "fiat": fiat,
            "offers": offers_meta,
            "source_pair": pair.upper(),
        }
        extra_meta = cfg.get("metadata") or {}
        extra_meta = _format_with_context(extra_meta, context)
        if isinstance(extra_meta, dict):
            metadata.update(extra_meta)

        return Quote(
            self.normalize_symbol(pair),
            bid,
            ask,
            timestamp_ms,
            source="p2p",
            metadata=metadata,
        )


class KuCoin(ExchangeAdapter):
    name = "kucoin"
    depth_supported = True

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "-")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        test_quote = self._test_mode_quote(pair)
        if test_quote is not None:
            return self._attach_depth(pair, test_quote)
        if self._is_test_mode_enabled() and self._test_mode_paused():
            return self._attach_depth(pair, self._offline_quote(pair, reason="test_mode_paused"))
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "ticker", "https://api.kucoin.com/api/v1/market/orderbook/level1"
        )
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"symbol": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
                fallback_endpoints=[(fallback, {"symbol": sym}) for fallback in fallbacks],
            )
            data = response.data.get("data") or {}
            bid = safe_float(data.get("bestBid"))
            ask = safe_float(data.get("bestAsk"))
            if bid <= 0 or ask <= 0 or bid >= ask:
                raise HttpError("Precios inválidos en ticker")
            ts_field = safe_float(data.get("time"))
            if ts_field > 0:
                ts_val = ensure_fresh_timestamp(int(ts_field), response.received_ts, "kucoin:ticker")
            else:
                ts_val = response.received_ts
            quote = Quote(sym, bid, ask, int(ts_val), checksum=response.checksum, source="level1")
        except Exception as exc:
            print(f"[kucoin] ticker fallback {pair}: {exc}")
            quote = self._offline_quote(pair, reason=str(exc))
        quote = self._attach_depth(pair, quote)
        if quote and quote.bid >= quote.ask:
            return None
        return quote

    def fetch_depth_snapshot(self, pair: str) -> Optional[DepthInfo]:
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "depth", "https://api.kucoin.com/api/v1/market/orderbook/level2_20"
        )
        try:
            response = http_get_json(
                url,
                params={"symbol": sym},
                integrity_key=self._integrity_key(sym, "depth"),
                fallback_endpoints=[(fallback, {"symbol": sym}) for fallback in fallbacks],
            )
            data = response.data.get("data") or {}
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            if not bids or not asks:
                raise HttpError("Depth vacío")
            best_bid = safe_float(bids[0][0])
            best_ask = safe_float(asks[0][0])
            bid_volume = sum(safe_float(entry[1]) for entry in bids)
            ask_volume = sum(safe_float(entry[1]) for entry in asks)
            levels = min(len(bids), len(asks))
            ts_field = safe_float(data.get("time"))
            if ts_field > 0:
                ts_val = ensure_fresh_timestamp(int(ts_field), response.received_ts, "kucoin:depth")
            else:
                ts_val = response.received_ts
            return DepthInfo(best_bid, best_ask, bid_volume, ask_volume, levels, int(ts_val), response.checksum)
        except Exception as exc:
            print(f"[kucoin] depth error {pair}: {exc}")
            return None


class OKX(ExchangeAdapter):
    name = "okx"
    depth_supported = True

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "-")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        test_quote = self._test_mode_quote(pair)
        if test_quote is not None:
            return self._attach_depth(pair, test_quote)
        if self._is_test_mode_enabled() and self._test_mode_paused():
            return self._attach_depth(pair, self._offline_quote(pair, reason="test_mode_paused"))
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "ticker", "https://www.okx.com/api/v5/market/ticker"
        )
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"instId": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
                fallback_endpoints=[(fallback, {"instId": sym}) for fallback in fallbacks],
            )
            items = response.data.get("data") or []
            if not items:
                raise HttpError("Ticker vacío")
            item = items[0]
            bid = safe_float(item.get("bidPx"))
            ask = safe_float(item.get("askPx"))
            if bid <= 0 or ask <= 0 or bid >= ask:
                raise HttpError("Precios inválidos en ticker")
            ts_field = safe_float(item.get("ts") or response.data.get("ts"))
            if ts_field > 0:
                ts_val = ensure_fresh_timestamp(int(ts_field), response.received_ts, "okx:ticker")
            else:
                ts_val = response.received_ts
            quote = Quote(sym, bid, ask, int(ts_val), checksum=response.checksum, source="ticker")
        except Exception as exc:
            print(f"[okx] ticker fallback {pair}: {exc}")
            quote = self._offline_quote(pair, reason=str(exc))
        quote = self._attach_depth(pair, quote)
        if quote and quote.bid >= quote.ask:
            return None
        return quote

    def fetch_depth_snapshot(self, pair: str) -> Optional[DepthInfo]:
        sym = self.normalize_symbol(pair)
        url, fallbacks = self._endpoint_config(
            "depth", "https://www.okx.com/api/v5/market/books"
        )
        try:
            response = http_get_json(
                url,
                params={"instId": sym, "sz": "20"},
                integrity_key=self._integrity_key(sym, "depth"),
                fallback_endpoints=[(fallback, {"instId": sym, "sz": "20"}) for fallback in fallbacks],
            )
            items = response.data.get("data") or []
            if not items:
                raise HttpError("Depth vacío")
            item = items[0]
            bids = item.get("bids") or []
            asks = item.get("asks") or []
            if not bids or not asks:
                raise HttpError("Depth vacío")
            best_bid = safe_float(bids[0][0])
            best_ask = safe_float(asks[0][0])
            bid_volume = sum(safe_float(entry[1]) for entry in bids)
            ask_volume = sum(safe_float(entry[1]) for entry in asks)
            levels = min(len(bids), len(asks))
            ts_field = safe_float(item.get("ts") or response.data.get("ts"))
            if ts_field > 0:
                ts_val = ensure_fresh_timestamp(int(ts_field), response.received_ts, "okx:depth")
            else:
                ts_val = response.received_ts
            return DepthInfo(best_bid, best_ask, bid_volume, ask_volume, levels, int(ts_val), response.checksum)
        except Exception as exc:
            print(f"[okx] depth error {pair}: {exc}")
            return None


ADAPTER_REGISTRY: Dict[str, Type[ExchangeAdapter]] = {
    "binance": Binance,
    "bybit": Bybit,
    "kucoin": KuCoin,
    "okx": OKX,
    "generic_p2p": GenericP2PMarketplace,
}


def build_adapters() -> Dict[str, ExchangeAdapter]:
    adapters: Dict[str, ExchangeAdapter] = {}
    for venue_name, cfg in CONFIG.get("venues", {}).items():
        if not cfg or not cfg.get("enabled", False):
            continue
        adapter_key = str(cfg.get("adapter") or venue_name).lower()
        adapter_cls = ADAPTER_REGISTRY.get(adapter_key)
        if adapter_cls is None:
            adapter_cls = ADAPTER_REGISTRY.get(venue_name.lower())
        if adapter_cls is None:
            continue
        if adapter_cls is GenericP2PMarketplace:
            adapters[venue_name] = adapter_cls(venue_name)
        else:
            adapters[venue_name] = adapter_cls()
    return adapters


def collect_pair_quotes(pairs: List[str], adapters: Dict[str, ExchangeAdapter]) -> Dict[str, Dict[str, Quote]]:
    pair_quotes: Dict[str, Dict[str, Quote]] = {pair: {} for pair in pairs}
    if not pairs or not adapters:
        return pair_quotes

    p2p_pairs_cfg = configured_p2p_pairs()
    futures_map: Dict[Any, Tuple[str, str]] = {}

    def _task(adapter: ExchangeAdapter, pair: str, venue: str) -> Optional[Quote]:
        record_exchange_attempt(venue, pair)
        try:
            quote = adapter.fetch_quote(pair)
        except Exception as exc:
            record_exchange_error(venue, str(exc), pair)
            return None

        if quote:
            record_exchange_success(venue, pair)
            log_event(
                "exchange.quote",
                exchange=venue,
                pair=pair,
                bid=float(quote.bid),
                ask=float(quote.ask),
            )
            return quote

        record_exchange_no_data(venue, pair)
        return None

    max_workers = min(
        max(1, DEFAULT_QUOTE_WORKERS),
        max(1, len(pairs) * max(1, len(adapters))),
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for pair in pairs:
            for venue, adapter in adapters.items():
                if is_circuit_open(venue):
                    record_exchange_skip(venue, "circuit_open", pair)
                    continue
                venue_p2p_pairs = p2p_pairs_cfg.get(venue, {})
                pair_key = pair.upper()
                is_p2p_pair = pair_key in venue_p2p_pairs
                if venue == "bybit" and pair_key.endswith("/ARS") and not is_p2p_pair:
                    print("[bybit] ARS no está en spot; usar Convert/P2P (no implementado).")
                    record_exchange_skip(venue, "ars_not_spot", pair)
                    continue
                futures_map[executor.submit(_task, adapter, pair, venue)] = (pair, venue)

        for future in as_completed(futures_map):
            pair, venue = futures_map[future]
            try:
                quote = future.result()
            except Exception as exc:
                print(f"[{venue}] error fetch {pair}: {exc}")
                continue
            if quote:
                source = str(getattr(quote, "source", "")).lower()
                if source == "offline":
                    log_event(
                        "exchange.quote.skip",
                        exchange=venue,
                        pair=pair,
                        reason="offline_source",
                    )
                    record_exchange_no_data(venue, pair)
                    continue

                max_age_seconds = float(CONFIG.get("max_quote_age_seconds", 0.0))
                if max_age_seconds > 0:
                    try:
                        age_ms = max(0, current_millis() - int(quote.ts))
                    except Exception:
                        age_ms = None
                    if age_ms is None or age_ms > max_age_seconds * 1000:
                        log_event(
                            "exchange.quote.skip",
                            exchange=venue,
                            pair=pair,
                            reason="stale_quote",
                            age_ms=age_ms,
                        )
                        record_exchange_no_data(venue, pair)
                        continue

                pair_quotes[pair][venue] = quote

    return pair_quotes


def diagnose_exchange_pairs(
    pairs: Iterable[str],
    adapters: Dict[str, ExchangeAdapter],
    max_workers: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Ejecuta fetch_quote para cada par y exchange, devolviendo diagnósticos detallados."""

    pairs_list = [pair.strip().upper() for pair in pairs if pair]
    if not pairs_list or not adapters:
        return []

    futures: Dict[Any, Tuple[str, str]] = {}
    results: List[Dict[str, Any]] = []
    workers = max_workers or DEFAULT_QUOTE_WORKERS
    workers = max(1, workers)

    def _task(venue: str, adapter: ExchangeAdapter, pair: str) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            quote = adapter.fetch_quote(pair)
        except Exception as exc:  # pragma: no cover - logging handled by caller
            latency_ms = (time.perf_counter() - started) * 1000.0
            return {
                "venue": venue,
                "pair": pair,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": latency_ms,
            }

        latency_ms = (time.perf_counter() - started) * 1000.0
        if quote:
            try:
                bid = float(quote.bid)
                ask = float(quote.ask)
            except (TypeError, ValueError):
                bid = math.nan
                ask = math.nan

            if (
                not math.isfinite(bid)
                or not math.isfinite(ask)
                or bid <= 0
                or ask <= 0
                or bid >= ask
            ):
                return {
                    "venue": venue,
                    "pair": pair,
                    "status": "error",
                    "error": "InvalidQuote: bid/ask inválidos",
                    "latency_ms": latency_ms,
                }

            source = str(quote.source or "")
            offline = source.lower() == "offline"
            try:
                ts_value = int(quote.ts)
            except (TypeError, ValueError):
                ts_value = int(time.time() * 1000)

            return {
                "venue": venue,
                "pair": pair,
                "status": "ok",
                "bid": bid,
                "ask": ask,
                "latency_ms": latency_ms,
                "source": source,
                "offline_source": offline,
                "timestamp": ts_value,
            }

        return {
            "venue": venue,
            "pair": pair,
            "status": "no_data",
            "latency_ms": latency_ms,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for venue, adapter in adapters.items():
            for pair in pairs_list:
                future = executor.submit(_task, venue, adapter, pair)
                futures[future] = (venue, pair)

        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # pragma: no cover - defensive
                venue, pair = futures[future]
                results.append(
                    {
                        "venue": venue,
                        "pair": pair,
                        "status": "error",
                        "error": f"UnexpectedError: {exc}",
                        "latency_ms": 0.0,
                    }
                )

    return results


def build_quote_snapshot(
    pair_quotes: Dict[str, Dict[str, Quote]], limit: int = 10
) -> Dict[str, Dict[str, Dict[str, float]]]:
    snapshot: Dict[str, Dict[str, Dict[str, float]]] = {}
    for pair in sorted(pair_quotes.keys())[:limit]:
        venues = pair_quotes.get(pair, {})
        if not venues:
            continue
        snapshot[pair] = {
            venue: {
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "ts": int(quote.ts),
            }
            for venue, quote in venues.items()
        }
    return snapshot


def update_prometheus_metrics(
    metrics: Dict[str, Dict[str, Any]], summary: Dict[str, Any], tri_alerts: int
) -> None:
    if summary:
        PROM_LAST_RUN_TS.set(float(summary.get("ts", 0)))
        PROM_LAST_RUN_LATENCY_MS.set(float(summary.get("run_latency_ms", 0)))
        PROM_ALERTS_SENT.set(float(summary.get("alerts_sent", 0)))
        PROM_TRIANGULAR_ALERTS.set(float(tri_alerts))

    for exchange, stats in metrics.items():
        PROM_EXCHANGE_ATTEMPTS.labels(exchange=exchange).set(float(stats.get("attempts", 0)))
        PROM_EXCHANGE_ERRORS.labels(exchange=exchange).set(float(stats.get("errors", 0)))

# =========================
# Engine
# =========================
@dataclass
class Opportunity:
    pair: str
    buy_venue: str
    sell_venue: str
    buy_price: float
    sell_price: float
    gross_percent: float
    net_percent: float
    buy_depth: Optional[DepthInfo] = None
    sell_depth: Optional[DepthInfo] = None
    liquidity_score: float = 0.0
    volatility_score: float = 0.0
    priority_score: float = 0.0
    confidence_label: str = "media"
    strategy: str = "spot_spot"
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestParams:
    capital_quote: float
    slippage_bps: float
    rebalance_bps: float
    latency_seconds: float
    latency_penalty_multiplier: float


@dataclass
class BacktestReport:
    total_trades: int = 0
    profitable_trades: int = 0
    cumulative_pnl: float = 0.0
    average_pnl: float = 0.0
    success_rate: float = 0.0
    average_effective_percent: float = 0.0


@dataclass
class HistoricalAnalysis:
    rows_considered: int
    success_rate: float
    average_net_percent: float
    average_effective_percent: float
    recommended_threshold: float
    pair_volatility: Dict[str, float]
    max_volatility: float
    backtest: BacktestReport
def load_historical_rows(path: str, lookback_hours: int) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []

    ensure_log_header(path)

    cutoff_ts: Optional[int] = None
    if lookback_hours > 0:
        cutoff_ts = int(time.time() - lookback_hours * 3600)

    rows: List[Dict[str, str]] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(float(row.get("ts", 0)))
            except (TypeError, ValueError):
                continue
            if cutoff_ts is not None and ts < cutoff_ts:
                continue
            rows.append(row)
    return rows


def compute_pair_volatility(rows: Iterable[Dict[str, str]]) -> Tuple[Dict[str, float], float]:
    per_pair: Dict[str, List[float]] = {}
    for row in rows:
        pair = row.get("pair")
        if not pair:
            continue
        per_pair.setdefault(pair, []).append(safe_float(row.get("net_%"), 0.0))

    volatility: Dict[str, float] = {}
    max_volatility = 0.0
    for pair, values in per_pair.items():
        if len(values) > 1:
            try:
                vol = pstdev(values)
            except StatisticsError:
                vol = 0.0
        else:
            vol = 0.0
        volatility[pair] = vol
        max_volatility = max(max_volatility, vol)
    return volatility, max_volatility


def build_backtest_params(capital: float, cfg: Dict[str, float]) -> BacktestParams:
    return BacktestParams(
        capital_quote=capital,
        slippage_bps=float(cfg.get("slippage_bps", 0.0)),
        rebalance_bps=float(cfg.get("rebalance_bps", 0.0)),
        latency_seconds=float(cfg.get("latency_seconds", 0.0)),
        latency_penalty_multiplier=float(cfg.get("latency_penalty_multiplier", 0.0)),
    )


def compute_effective_net_percent(net_percent: float, pair_volatility: float, params: BacktestParams) -> float:
    penalty = (params.slippage_bps + params.rebalance_bps) / 100.0
    if pair_volatility > 0 and params.latency_seconds > 0 and params.latency_penalty_multiplier > 0:
        penalty += pair_volatility * (params.latency_seconds / 60.0) * params.latency_penalty_multiplier
    return net_percent - penalty


def run_backtest(rows: Iterable[Dict[str, str]], params: BacktestParams, pair_volatility: Dict[str, float]) -> Tuple[BacktestReport, List[float], List[float]]:
    net_values: List[float] = []
    effective_values: List[float] = []
    cumulative_pnl = 0.0
    profitable = 0

    for row in rows:
        pair = row.get("pair")
        if not pair:
            continue
        net_percent = safe_float(row.get("net_%"), 0.0)
        effective_net = compute_effective_net_percent(net_percent, pair_volatility.get(pair, 0.0), params)
        net_values.append(net_percent)
        effective_values.append(effective_net)
        pnl = params.capital_quote * (effective_net / 100.0)
        cumulative_pnl += pnl
        if pnl > 0:
            profitable += 1

    total = len(effective_values)
    average_pnl = cumulative_pnl / total if total else 0.0
    average_effective = mean(effective_values) if effective_values else 0.0
    success_rate = (profitable / total) if total else 0.0

    report = BacktestReport(
        total_trades=total,
        profitable_trades=profitable,
        cumulative_pnl=cumulative_pnl,
        average_pnl=average_pnl,
        success_rate=success_rate,
        average_effective_percent=average_effective,
    )

    return report, net_values, effective_values


def compute_dynamic_threshold(
    net_values: List[float],
    effective_values: List[float],
    success_rate: float,
    current_threshold: float,
    cfg: Dict[str, float],
) -> float:
    if not net_values:
        return current_threshold

    target = float(cfg.get("target_success_rate", 0.6))
    min_thr = float(cfg.get("min_threshold_percent", 0.1))
    max_thr = float(cfg.get("max_threshold_percent", 5.0))
    adjust_multiplier = float(cfg.get("adjust_multiplier", 0.4))

    sorted_net = sorted(net_values)
    # índice asociado al percentil que deja target% de señales por encima
    idx = max(0, min(len(sorted_net) - 1, int(math.floor((1 - target) * len(sorted_net)))))
    quantile_net = sorted_net[idx]

    penalties: List[float] = []
    for net, eff in zip(net_values, effective_values):
        penalties.append(net - eff)
    avg_penalty = mean(penalties) if penalties else 0.0

    candidate = quantile_net + max(0.0, avg_penalty)

    diff = success_rate - target
    adjusted = current_threshold - diff * adjust_multiplier

    blended = 0.5 * adjusted + 0.5 * candidate
    return max(min_thr, min(max_thr, blended))


def analyze_historical_performance(path: str, capital: float) -> HistoricalAnalysis:
    analysis_cfg = CONFIG.get("analysis", {})
    lookback_hours = int(analysis_cfg.get("lookback_hours", 0))
    rows = load_historical_rows(path, lookback_hours)

    params = build_backtest_params(capital, CONFIG.get("execution_costs", {}))

    if not rows:
        backtest = BacktestReport()
        return HistoricalAnalysis(
            rows_considered=0,
            success_rate=backtest.success_rate,
            average_net_percent=0.0,
            average_effective_percent=backtest.average_effective_percent,
            recommended_threshold=float(CONFIG["threshold_percent"]),
            pair_volatility={},
            max_volatility=0.0,
            backtest=backtest,
        )

    volatility, max_volatility = compute_pair_volatility(rows)
    backtest, net_values, effective_values = run_backtest(rows, params, volatility)

    average_net = mean(net_values) if net_values else 0.0
    recommended_threshold = compute_dynamic_threshold(
        net_values,
        effective_values,
        backtest.success_rate,
        float(CONFIG["threshold_percent"]),
        analysis_cfg,
    )

    return HistoricalAnalysis(
        rows_considered=len(net_values),
        success_rate=backtest.success_rate,
        average_net_percent=average_net,
        average_effective_percent=backtest.average_effective_percent,
        recommended_threshold=recommended_threshold,
        pair_volatility=volatility,
        max_volatility=max_volatility,
        backtest=backtest,
    )

@dataclass
class TriangleLeg:
    pair: str
    action: str  # BUY_BASE o SELL_BASE

    def normalized_action(self) -> str:
        return self.action.strip().upper()


@dataclass
class TriangularRoute:
    name: str
    venue: str
    start_asset: str
    legs: List[TriangleLeg]

    @property
    def identifier(self) -> str:
        return f"{self.venue}::{self.name}"


@dataclass
class TriangularOpportunity:
    route: TriangularRoute
    start_capital: float
    final_capital_gross: float
    final_capital_net: float
    gross_percent: float
    net_percent: float
    leg_prices: List[Tuple[TriangleLeg, float]]

    @property
    def net_profit(self) -> float:
        return self.final_capital_net - self.start_capital

def compute_opportunities_for_pair(
    pair: str,
    quotes: Dict[str, Quote],
    fees: Dict[str, VenueFees],
) -> List[Opportunity]:
    venues = list(quotes.keys())
    opportunities: List[Opportunity] = []
    for buy_v, sell_v in itertools.permutations(venues, 2):
        buy_quote = quotes.get(buy_v)
        sell_quote = quotes.get(sell_v)
        if not buy_quote or not sell_quote:
            continue
        if str(getattr(buy_quote, "source", "")).lower() == "offline":
            continue
        if str(getattr(sell_quote, "source", "")).lower() == "offline":
            continue

        buy_fee_cfg = fees.get(buy_v)
        sell_fee_cfg = fees.get(sell_v)
        if not buy_fee_cfg or not sell_fee_cfg:
            continue

        buy_price = float(buy_quote.ask)
        sell_price = float(sell_quote.bid)
        if buy_price <= 0 or sell_price <= 0:
            continue

        buy_schedule = buy_fee_cfg.schedule_for_pair(pair)
        sell_schedule = sell_fee_cfg.schedule_for_pair(pair)

        buy_price = apply_slippage(buy_quote.ask, buy_schedule.slippage_bps, "buy")
        sell_price = apply_slippage(sell_quote.bid, sell_schedule.slippage_bps, "sell")
        if buy_price <= 0 or sell_price <= 0:
            continue

        gross_percent = (sell_price - buy_price) / buy_price * 100.0
        total_fee = buy_schedule.taker_fee_percent + sell_schedule.taker_fee_percent
        net_percent = gross_percent - total_fee

        opportunities.append(
            Opportunity(
                pair=pair,
                buy_venue=buy_v,
                sell_venue=sell_v,
                buy_price=buy_price,
                sell_price=sell_price,
                gross_percent=gross_percent,
                net_percent=net_percent,
                buy_depth=getattr(buy_quote, "depth", None),
                sell_depth=getattr(sell_quote, "depth", None),
                strategy="spot_spot",
            )
        )

    return sorted(opportunities, key=lambda o: o.net_percent, reverse=True)


def compute_spot_p2p_opportunities(
    pair: str,
    spot_quotes: Dict[str, Quote],
    p2p_quotes: Dict[str, Quote],
    fees: Dict[str, VenueFees],
) -> List[Opportunity]:
    opportunities: List[Opportunity] = []
    base, _ = split_pair(pair)
    asset = base.upper()
    for spot_venue, spot_quote in spot_quotes.items():
        fee_cfg = fees.get(spot_venue)
        if not fee_cfg:
            continue
        buy_schedule = fee_cfg.schedule_for_pair(pair)
        sell_schedule = fee_cfg.schedule_for_pair(pair)
        spot_buy = apply_slippage(spot_quote.ask, buy_schedule.slippage_bps, "buy")
        spot_sell = apply_slippage(spot_quote.bid, sell_schedule.slippage_bps, "sell")
        if spot_buy <= 0 or spot_sell <= 0:
            continue
        for p2p_venue, p2p_quote in p2p_quotes.items():
            buy_fee = buy_schedule.taker_fee_percent
            sell_fee = sell_schedule.taker_fee_percent
            p2p_fee = get_p2p_fee_percent(p2p_venue, asset)
            p2p_bid = p2p_quote.bid
            p2p_ask = p2p_quote.ask
            if p2p_bid > 0:
                gross = (p2p_bid - spot_buy) / spot_buy * 100.0
                net = gross - buy_fee - p2p_fee
                opportunities.append(
                    Opportunity(
                        pair=pair,
                        buy_venue=spot_venue,
                        sell_venue=f"{p2p_venue}_p2p",
                        buy_price=spot_buy,
                        sell_price=p2p_bid,
                        gross_percent=gross,
                        net_percent=net,
                        buy_depth=getattr(spot_quote, "depth", None),
                        sell_depth=None,
                        strategy="spot_p2p",
                        notes={
                            "side": "spot_to_p2p",
                            "p2p_fee_percent": p2p_fee,
                            "p2p_venue": p2p_venue,
                            "fiat": p2p_quote.metadata.get("fiat"),
                        },
                    )
                )
            if p2p_ask > 0:
                gross = (spot_sell - p2p_ask) / p2p_ask * 100.0
                net = gross - sell_fee - p2p_fee
                opportunities.append(
                    Opportunity(
                        pair=pair,
                        buy_venue=f"{p2p_venue}_p2p",
                        sell_venue=spot_venue,
                        buy_price=p2p_ask,
                        sell_price=spot_sell,
                        gross_percent=gross,
                        net_percent=net,
                        buy_depth=None,
                        sell_depth=getattr(spot_quote, "depth", None),
                        strategy="spot_p2p",
                        notes={
                            "side": "p2p_to_spot",
                            "p2p_fee_percent": p2p_fee,
                            "p2p_venue": p2p_venue,
                            "fiat": p2p_quote.metadata.get("fiat"),
                        },
                    )
                )
    return sorted(opportunities, key=lambda o: o.net_percent, reverse=True)


def compute_p2p_cross_opportunities(
    pair: str,
    quotes: Dict[str, Quote],
) -> List[Opportunity]:
    base, _ = split_pair(pair)
    opportunities: List[Opportunity] = []
    venues = list(quotes.keys())
    for buy_v, sell_v in itertools.permutations(venues, 2):
        buy_quote = quotes.get(buy_v)
        sell_quote = quotes.get(sell_v)
        if not buy_quote or not sell_quote:
            continue
        buy_price = buy_quote.ask
        sell_price = sell_quote.bid
        if buy_price <= 0 or sell_price <= 0:
            continue
        buy_fee = get_p2p_fee_percent(buy_v, base)
        sell_fee = get_p2p_fee_percent(sell_v, base)
        gross_percent = (sell_price - buy_price) / buy_price * 100.0
        net_percent = gross_percent - buy_fee - sell_fee
        opportunities.append(
            Opportunity(
                pair=pair,
                buy_venue=f"{buy_v}_p2p",
                sell_venue=f"{sell_v}_p2p",
                buy_price=buy_price,
                sell_price=sell_price,
                gross_percent=gross_percent,
                net_percent=net_percent,
                strategy="p2p_p2p",
                notes={
                    "p2p_buy_fee_percent": buy_fee,
                    "p2p_sell_fee_percent": sell_fee,
                },
            )
        )
    return sorted(opportunities, key=lambda o: o.net_percent, reverse=True)


def get_weighted_capital(base_capital: float, weights_cfg: Dict[str, float], key: str) -> float:
    if base_capital <= 0:
        return 0.0
    default = float(weights_cfg.get("default", 1.0)) if weights_cfg else 1.0
    weight = float(weights_cfg.get(key, default)) if weights_cfg else default
    return base_capital * weight


def load_triangular_routes() -> List[TriangularRoute]:
    routes_cfg = CONFIG.get("triangular_routes", []) or []
    routes: List[TriangularRoute] = []
    for rcfg in routes_cfg:
        legs_cfg = rcfg.get("legs", []) or []
        legs = [
            TriangleLeg(
                pair=str(leg_cfg.get("pair", "")).upper(),
                action=str(leg_cfg.get("action", "BUY_BASE")),
            )
            for leg_cfg in legs_cfg
            if leg_cfg.get("pair")
        ]
        if not legs:
            continue
        name = str(rcfg.get("name", "triangle")).strip() or "triangle"
        venue = str(rcfg.get("venue", "")).strip().lower()
        if not venue:
            continue
        start_asset = str(rcfg.get("start_asset", "USDT")).upper() or "USDT"
        routes.append(TriangularRoute(name=name, venue=venue, start_asset=start_asset, legs=legs))
    return routes


def compute_triangular_opportunity(route: TriangularRoute,
                                   quotes_by_pair: Dict[str, Dict[str, Quote]],
                                   fees: Dict[str, VenueFees],
                                   start_capital: float) -> Optional[TriangularOpportunity]:
    if start_capital <= 0:
        return None

    fee_cfg = fees.get(route.venue)
    fee_rate = (fee_cfg.taker_fee_percent / 100.0) if fee_cfg else 0.0

    gross_amount = start_capital
    net_amount = start_capital
    legs_with_prices: List[Tuple[TriangleLeg, float]] = []

    for leg in route.legs:
        quotes_for_pair = quotes_by_pair.get(leg.pair, {})
        quote = quotes_for_pair.get(route.venue)
        if not quote:
            return None

        action = leg.normalized_action()
        if action == "BUY_BASE":
            price = quote.ask
            if price <= 0:
                return None
            gross_amount = gross_amount / price
            net_amount = (net_amount / price) * (1 - fee_rate)
        elif action == "SELL_BASE":
            price = quote.bid
            if price <= 0:
                return None
            gross_amount = gross_amount * price
            net_amount = (net_amount * price) * (1 - fee_rate)
        else:
            return None

        legs_with_prices.append((leg, price))

    gross_percent = (gross_amount - start_capital) / start_capital * 100.0
    net_percent = (net_amount - start_capital) / start_capital * 100.0

    return TriangularOpportunity(
        route=route,
        start_capital=start_capital,
        final_capital_gross=gross_amount,
        final_capital_net=net_amount,
        gross_percent=gross_percent,
        net_percent=net_percent,
        leg_prices=legs_with_prices,
    )

# =========================
# Simulación PnL (avanzada)
# =========================


def _available_depth_qty(depth: Optional[DepthInfo], side: str) -> float:
    if not depth:
        return 0.0
    if side == "buy":
        return float(depth.ask_volume)
    return float(depth.bid_volume)


def compute_liquidity_score(opp: Opportunity, required_base_qty: float) -> float:
    if required_base_qty <= 0:
        return 0.0

    buy_available = _available_depth_qty(opp.buy_depth, "buy")
    sell_available = _available_depth_qty(opp.sell_depth, "sell")
    if buy_available <= 0 and sell_available <= 0:
        return 0.0

    coverage_buy = min(1.0, buy_available / required_base_qty) if buy_available > 0 else 0.0
    coverage_sell = min(1.0, sell_available / required_base_qty) if sell_available > 0 else 0.0
    depth_factor = 0.0
    if opp.buy_depth and opp.sell_depth:
        depth_factor = min(1.0, (opp.buy_depth.levels + opp.sell_depth.levels) / 40.0)

    raw_score = (coverage_buy + coverage_sell) / 2.0
    blended = 0.7 * raw_score + 0.3 * depth_factor
    return round(min(1.0, blended), 4)


def compute_volatility_score(pair: str) -> float:
    if not LATEST_ANALYSIS or not getattr(LATEST_ANALYSIS, "max_volatility", 0):
        return 0.0
    max_vol = max(1e-9, float(LATEST_ANALYSIS.max_volatility))
    pair_vol = float(LATEST_ANALYSIS.pair_volatility.get(pair, 0.0))
    return round(min(1.0, pair_vol / max_vol), 4)


def compute_priority_score(net_percent: float, liquidity_score: float, volatility_score: float) -> float:
    net = float(net_percent)
    liquidity_bonus = net * 0.5 * liquidity_score
    volatility_penalty = abs(net) * 0.4 * volatility_score
    return round(net + liquidity_bonus - volatility_penalty, 6)


def classify_confidence(
    net_percent: float,
    threshold: float,
    liquidity_score: float,
    volatility_score: float,
    priority_score: float,
) -> str:
    if net_percent >= threshold * 1.5 and liquidity_score >= 0.65 and volatility_score <= 0.3:
        return "alta"
    if net_percent >= threshold and liquidity_score >= 0.4 and priority_score >= threshold * 1.1:
        return "media"
    return "baja"


def estimate_profit(
    capital_quote: float,
    buy_price: float,
    sell_price: float,
    total_percent_fee: float,
    max_base_qty: Optional[float] = None,
) -> Tuple[float, float, float, float]:
    if buy_price <= 0 or sell_price <= 0 or capital_quote <= 0:
        return 0.0, 0.0, 0.0, 0.0

    base_qty = capital_quote / buy_price
    if max_base_qty is not None:
        base_qty = min(base_qty, max_base_qty)

    if base_qty <= 0:
        return 0.0, 0.0, 0.0, 0.0

    effective_capital = base_qty * buy_price
    gross_proceeds = base_qty * sell_price
    fee_loss = (total_percent_fee / 100.0) * effective_capital
    profit = gross_proceeds - effective_capital - fee_loss
    net_pct = (profit / effective_capital) * 100.0 if effective_capital > 0 else 0.0
    return profit, net_pct, base_qty, effective_capital

# =========================
# Logging CSV
# =========================


def ensure_log_header(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(LOG_HEADER)


def append_csv(
    path: str,
    opp: Opportunity,
    est_profit: float,
    base_qty: float,
    capital_used: float,
    buy_depth: Optional[DepthInfo],
    sell_depth: Optional[DepthInfo],
) -> None:
    ensure_log_header(path)
    buy_depth_qty = _available_depth_qty(buy_depth, "buy")
    sell_depth_qty = _available_depth_qty(sell_depth, "sell")
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                int(time.time()),
                opp.pair,
                opp.buy_venue,
                opp.sell_venue,
                f"{opp.buy_price:.8f}",
                f"{opp.sell_price:.8f}",
                f"{opp.gross_percent:.4f}",
                f"{opp.net_percent:.4f}",
                f"{est_profit:.4f}",
                f"{base_qty:.8f}",
                f"{capital_used:.8f}",
                f"{buy_depth_qty:.8f}",
                f"{sell_depth_qty:.8f}",
                f"{opp.liquidity_score:.4f}",
                f"{opp.volatility_score:.4f}",
                f"{opp.priority_score:.6f}",
                opp.confidence_label,
            ]
        )


def ensure_log_backups(paths: Iterable[str]) -> None:
    backup_dir = Path(LOG_BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    for raw_path in paths:
        if not raw_path:
            continue
        file_path = Path(raw_path)
        if not file_path.exists() or file_path.is_dir():
            continue
        target = backup_dir / f"{file_path.stem}-{timestamp}{file_path.suffix}"
        shutil.copy2(file_path, target)

    backups = sorted(
        backup_dir.glob("*.csv"), key=lambda item: item.stat().st_mtime, reverse=True
    )
    for obsolete in backups[20:]:
        try:
            obsolete.unlink()
        except OSError:
            pass


def append_triangular_csv(path: str, opp: TriangularOpportunity) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow([
                "ts",
                "route",
                "venue",
                "start_asset",
                "start_capital",
                "final_capital_net",
                "gross_%",
                "net_%",
                "legs",
            ])
        leg_summary = " | ".join(
            f"{leg.pair}:{leg.normalized_action()}@{price:.8f}"
            for leg, price in opp.leg_prices
        )
        w.writerow([
            int(time.time()),
            opp.route.name,
            opp.route.venue,
            opp.route.start_asset,
            f"{opp.start_capital:.8f}",
            f"{opp.final_capital_net:.8f}",
            f"{opp.gross_percent:.4f}",
            f"{opp.net_percent:.4f}",
            leg_summary,
        ])

# =========================
# Formato de alerta
# =========================
def format_decimal_comma(value: float, decimals: int = 2, min_int_digits: int = 2) -> str:
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    formatted = f"{abs_value:.{decimals}f}"
    integer_part = str(int(abs_value))
    if len(integer_part) < min_int_digits:
        total_length = min_int_digits + decimals + 1
        formatted = formatted.zfill(total_length)
    return f"{sign}{formatted.replace('.', ',')}"


def format_percent_comma(value: float) -> str:
    return f"{format_decimal_comma(value, decimals=2)}%"


def format_venue_label(venue: str) -> str:
    normalized = venue.strip()
    if normalized.lower().endswith("_p2p"):
        base = normalized[:-4]
        return f"{base.upper()} P2P"
    return normalized.upper()


def fmt_alert(
    opp: Opportunity,
    est_profit: float,
    est_percent: float,
    base_qty: float,
    capital_quote: float,
    capital_used: float,
    links: Optional[List[Dict[str, str]]] = None,
) -> str:
    strategy = getattr(opp, "strategy", "spot_spot")
    if strategy == "spot_p2p":
        title = "🚨 *Arbitraje spot↔P2P*"
    elif strategy == "p2p_p2p":
        title = "🚨 *Arbitraje P2P↔P2P*"
    else:
        title = "🚨 *Arbitraje spot detectado*"

    buy_label = format_venue_label(opp.buy_venue)
    sell_label = format_venue_label(opp.sell_venue)

    lines = [
        title,
        f"*Par:* `{opp.pair}`",
        f"*Ruta:* Comprar en *{buy_label}* a `{opp.buy_price:.6f}` · Vender en *{sell_label}* a `{opp.sell_price:.6f}`",
        f"*Spreads:* bruto `{format_percent_comma(opp.gross_percent)}` · neto `{format_percent_comma(opp.net_percent)}`",
        (
            "*PnL estimado:* `~"
            f"{format_decimal_comma(est_profit, decimals=2)} USDT` "
            f"(`{format_percent_comma(est_percent)}`) sobre {format_decimal_comma(capital_quote, decimals=2)} USDT"
        ),
        f"*Cantidad base:* `{base_qty:.6f}` ({format_decimal_comma(capital_used, decimals=2)} USDT usados)",
    ]
    fiat = opp.notes.get("fiat") if isinstance(opp.notes, dict) else None
    if fiat:
        lines.append(f"*Fiat P2P:* `{fiat}`")
    transfer_cost = opp.notes.get("transfer_cost_quote") if isinstance(opp.notes, dict) else None
    transfer_minutes = opp.notes.get("transfer_minutes") if isinstance(opp.notes, dict) else None
    if transfer_cost:
        eta = f" · ETA `{transfer_minutes:.1f}m`" if transfer_minutes is not None else ""
        lines.append(
            "*Transferencia estimada:* `"
            f"{format_decimal_comma(float(transfer_cost), decimals=2)} USDT`{eta}"
        )
    lines.append(time.strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)


def fmt_triangular_alert(opp: TriangularOpportunity, fee_percent: float) -> str:
    legs_lines = []
    for leg, price in opp.leg_prices:
        action = leg.normalized_action()
        legs_lines.append(f"- {leg.pair} [{action}] @ {price:.8f}")
    legs_block = "\n".join(legs_lines)
    return (
        "ARBITRAJE TRIANGULAR\n"
        f"Ruta: {opp.route.name} ({opp.route.venue})\n"
        f"Asset inicial: {opp.route.start_asset}\n"
        f"Capital simulado: {opp.start_capital:.4f} {opp.route.start_asset}\n"
        f"Resultado neto: {opp.final_capital_net:.4f} {opp.route.start_asset} (PnL {opp.net_profit:.4f}, {opp.net_percent:.3f}%)\n"
        f"Spread bruto: {opp.gross_percent:.3f}% | Fees considerados: {fee_percent:.3f}% por trade\n"
        f"Legs:\n{legs_block}\n"
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


def build_degradation_alerts(snapshot: Dict[str, Dict]) -> List[str]:
    alerts: List[str] = []
    for exchange, stats in snapshot.items():
        attempts = int(stats.get("attempts", 0))
        successes = int(stats.get("successes", 0))
        errors = int(stats.get("errors", 0))
        no_data = int(stats.get("no_data", 0))

        if attempts == 0:
            if register_degradation_alert(exchange, "no_attempts"):
                alerts.append(
                    f"⚠️ {exchange}: sin intentos de consulta recientes. Revisar configuración o circuito abierto."
                )
            continue

        if successes == 0:
            if register_degradation_alert(exchange, "no_data"):
                alerts.append(
                    f"⚠️ {exchange}: sin datos recibidos en la última corrida (intentos={attempts}, sin_datos={no_data})."
                )
            continue

        error_rate = errors / float(attempts)
        if errors and error_rate >= ERROR_RATE_ALERT_THRESHOLD:
            if register_degradation_alert(exchange, "high_error_rate"):
                alerts.append(
                    f"⚠️ {exchange}: tasa de errores {error_rate:.0%} (errores={errors}, intentos={attempts})."
                )

    return alerts

# =========================
# Run (una vez)
# =========================
def run_once() -> None:
    global DYNAMIC_THRESHOLD_PERCENT
    adapters = build_adapters()
    if not adapters:
        log_event("run.skip", reason="no_venues")
        return

    run_start = time.time()
    reset_metrics(adapters.keys())
    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    polling_active = TELEGRAM_POLLING_THREAD and TELEGRAM_POLLING_THREAD.is_alive()
    if tg_enabled and not polling_active:
        tg_process_updates(enabled=tg_enabled)

    routes = load_triangular_routes()
    pairs = normalize_pair_list(CONFIG["pairs"])
    extra_pairs = {leg.pair for route in routes for leg in route.legs}
    p2p_pairs_cfg = configured_p2p_pairs()
    p2p_pairs = sorted({pair for venue_pairs in p2p_pairs_cfg.values() for pair in venue_pairs})
    all_pairs = sorted(set(pairs) | extra_pairs | set(p2p_pairs))
    base_threshold = float(CONFIG.get("threshold_percent", 0.0))
    capital = float(CONFIG["simulation_capital_quote"])
    log_csv = CONFIG["log_csv_path"]
    tri_log_csv = CONFIG.get("triangular_log_csv_path")
    update_analysis_state(capital, log_csv)
    threshold = float(DYNAMIC_THRESHOLD_PERCENT or base_threshold)
    pair_weight_cfg = CONFIG.get("capital_weights", {}).get("pairs", {})
    triangle_weight_cfg = CONFIG.get("capital_weights", {}).get("triangles", {})
    fee_map = build_fee_map(all_pairs)
    transfers = build_transfer_profiles()
    summary_opps: List[Dict[str, Any]] = []
    alert_records: List[Dict[str, Any]] = []
    run_ts = int(time.time())

    fetch_started = time.time()
    pair_quotes = collect_pair_quotes(all_pairs, adapters)
    quote_latency_ms = int((time.time() - fetch_started) * 1000)
    log_event(
        "run.quotes_collected",
        pairs=len(all_pairs),
        venues=len(adapters),
        latency_ms=quote_latency_ms,
    )

    with STATE_LOCK:
        DASHBOARD_STATE["last_quote_latency_ms"] = quote_latency_ms
        DASHBOARD_STATE["last_quote_count"] = sum(len(v) for v in pair_quotes.values())
        DASHBOARD_STATE["latest_quotes"] = build_quote_snapshot(pair_quotes)

    for pair in all_pairs:
        venues_available = sorted(pair_quotes.get(pair, {}).keys())
        emit_pair_coverage(pair, venues_available)

    p2p_index = build_p2p_quote_index(pair_quotes)
    effective_p2p_quotes: Dict[str, Dict[str, Quote]] = {}
    if (is_strategy_enabled("spot_p2p") or is_strategy_enabled("p2p_p2p")) and p2p_index:
        effective_p2p_quotes = build_effective_p2p_quotes(p2p_index)

    spot_alerts = 0
    if is_strategy_enabled("spot_spot"):
        for pair in pairs:
            quotes = pair_quotes.get(pair, {})
            if len(quotes) < 2:
                available = sorted(quotes.keys())
                print(
                    "[SKIP] "
                    f"{pair}: solo {available} tiene spot; se necesitan 2 venues. "
                    "Sugerencia: BTC/USDT, ETH/USDT, XRP/USDT."
                )
                continue
            capital_for_pair = get_weighted_capital(capital, pair_weight_cfg, pair)
            if capital_for_pair <= 0:
                continue
            opps = compute_opportunities_for_pair(pair, quotes, fee_map)
            for opp in opps[:5]:
                fee_buy = fee_map.get(opp.buy_venue)
                fee_sell = fee_map.get(opp.sell_venue)
                if not fee_buy or not fee_sell:
                    continue
                buy_schedule = fee_buy.schedule_for_pair(pair)
                sell_schedule = fee_sell.schedule_for_pair(pair)
                total_fee_pct = buy_schedule.taker_fee_percent + sell_schedule.taker_fee_percent
                depth_volumes = [
                    v
                    for v in (
                        _available_depth_qty(opp.buy_depth, "buy"),
                        _available_depth_qty(opp.sell_depth, "sell"),
                    )
                    if v > 0
                ]
                max_depth_qty = min(depth_volumes) if len(depth_volumes) == 2 else None
                est_profit, est_percent, base_qty, capital_used = estimate_profit(
                    capital_for_pair,
                    opp.buy_price,
                    opp.sell_price,
                    total_fee_pct,
                    max_base_qty=max_depth_qty,
                )
                if base_qty <= 0 or capital_used <= 0:
                    continue
                valid_buy, reason_buy = validate_market_trade(
                    opp.buy_venue, opp.pair, base_qty, opp.buy_price
                )
                if not valid_buy:
                    print(
                        f"[SKIP] {opp.pair} {opp.buy_venue}->{opp.sell_venue} {reason_buy}"
                    )
                    continue
                valid_sell, reason_sell = validate_market_trade(
                    opp.sell_venue, opp.pair, base_qty, opp.sell_price
                )
                if not valid_sell:
                    print(
                        f"[SKIP] {opp.pair} {opp.buy_venue}->{opp.sell_venue} {reason_sell}"
                    )
                    continue
                transfer_est = estimate_round_trip_transfer_cost(
                    opp.pair,
                    opp.buy_venue,
                    opp.sell_venue,
                    base_qty,
                    opp.sell_price,
                    transfers,
                )
                est_profit_net = est_profit - transfer_est.total_cost_quote
                if est_profit_net <= 0:
                    print(
                        f"[SKIP] {opp.pair} {opp.buy_venue}->{opp.sell_venue} transfer_fee/ETA"
                    )
                    continue
                effective_net_percent = (
                    (est_profit_net / capital_used) * 100.0 if capital_used > 0 else 0.0
                )
                if effective_net_percent < threshold:
                    print(
                        f"[SKIP] {opp.pair} {opp.buy_venue}->{opp.sell_venue} transfer_fee/ETA"
                    )
                    continue
                opp.net_percent = effective_net_percent
                opp.notes.update(
                    {
                        "transfer_cost_quote": transfer_est.total_cost_quote,
                        "transfer_minutes": transfer_est.total_minutes,
                    }
                )
                est_profit = est_profit_net
                est_percent = effective_net_percent

            liquidity_score = compute_liquidity_score(opp, base_qty)
            volatility_score = compute_volatility_score(pair)
            priority_score = compute_priority_score(
                opp.net_percent, liquidity_score, volatility_score
            )
            confidence_label = classify_confidence(
                opp.net_percent,
                threshold,
                liquidity_score,
                volatility_score,
                priority_score,
            )

            opp.liquidity_score = liquidity_score
            opp.volatility_score = volatility_score
            opp.priority_score = priority_score
            opp.confidence_label = confidence_label

            link_items = build_trade_link_items(opp.buy_venue, opp.sell_venue, opp.pair)
            entry = {
                "pair": opp.pair,
                "buy_venue": opp.buy_venue,
                "sell_venue": opp.sell_venue,
                "buy_price": opp.buy_price,
                "sell_price": opp.sell_price,
                "gross_percent": opp.gross_percent,
                "net_percent": opp.net_percent,
                "est_profit_quote": est_profit,
                "est_percent": est_percent,
                "base_qty": base_qty,
                "capital_used_quote": capital_used,
                "links": link_items,
                "liquidity_score": liquidity_score,
                "volatility_score": volatility_score,
                "priority_score": priority_score,
                "confidence": confidence_label,
                "threshold_hit": est_percent >= threshold,
                "transfer_cost_quote": transfer_est.total_cost_quote,
                "transfer_minutes": transfer_est.total_minutes,
                "strategy": opp.strategy,
                "notes": opp.notes,
            }
            summary_opps.append(entry)
            if est_percent >= threshold:
                append_csv(
                    log_csv,
                    opp,
                    est_profit,
                    base_qty,
                    capital_used,
                    opp.buy_depth,
                    opp.sell_depth,
                )
                msg = fmt_alert(
                    opp,
                    est_profit,
                    est_percent,
                    base_qty,
                    capital_for_pair,
                    capital_used,
                    link_items,
                )
                tg_send_message(msg, enabled=tg_enabled)
                log_event(
                    "opportunity.alert",
                    pair=opp.pair,
                    buy_venue=opp.buy_venue,
                    sell_venue=opp.sell_venue,
                    net_percent=opp.net_percent,
                    est_profit=est_profit,
                )
                spot_alerts += 1
                alert_entry = dict(entry)
                alert_entry["ts"] = int(time.time())
                alert_entry["ts_str"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert_entry["ts"]))
                alert_records.append(alert_entry)

    spot_p2p_alerts = 0
    if is_strategy_enabled("spot_p2p"):
        for pair in pairs:
            asset, _ = split_pair(pair)
            p2p_asset_quotes = effective_p2p_quotes.get(asset)
            if not p2p_asset_quotes:
                print(f"[SKIP] {pair}: p2p_sin_ofertas")
                continue
            spot_quotes = {
                venue: quote
                for venue, quote in pair_quotes.get(pair, {}).items()
                if str(getattr(quote, "source", "")).lower() != "p2p"
            }
            if not spot_quotes:
                continue
            capital_for_pair = get_weighted_capital(capital, pair_weight_cfg, pair)
            if capital_for_pair <= 0:
                continue
            opps = compute_spot_p2p_opportunities(pair, spot_quotes, p2p_asset_quotes, fee_map)
            for opp in opps[:5]:
                side = opp.notes.get("side")
                p2p_venue = str(opp.notes.get("p2p_venue") or "")
                p2p_fee = float(opp.notes.get("p2p_fee_percent", 0.0) or 0.0)
                fiat = opp.notes.get("fiat")
                if side == "spot_to_p2p":
                    spot_venue = opp.buy_venue
                    spot_fee_cfg = fee_map.get(spot_venue)
                    if not spot_fee_cfg:
                        continue
                    buy_schedule = spot_fee_cfg.schedule_for_pair(pair)
                    adjusted_sell = opp.sell_price * (1 - p2p_fee / 100.0)
                    est_profit, est_percent, base_qty, capital_used = estimate_profit(
                        capital_for_pair,
                        opp.buy_price,
                        adjusted_sell,
                        buy_schedule.taker_fee_percent,
                    )
                    if base_qty <= 0 or capital_used <= 0:
                        continue
                    valid_spot, reason = validate_market_trade(spot_venue, pair, base_qty, opp.buy_price)
                    if not valid_spot:
                        print(f"[SKIP] {pair} {opp.buy_venue}->{opp.sell_venue} {reason}")
                        continue
                    notional = base_qty * opp.sell_price
                    valid_p2p, reason_p2p = validate_p2p_notional(p2p_venue, asset, notional)
                    if not valid_p2p:
                        print(f"[SKIP] {pair} {opp.buy_venue}->{opp.sell_venue} {reason_p2p}")
                        continue
                else:
                    spot_venue = opp.sell_venue
                    spot_fee_cfg = fee_map.get(spot_venue)
                    if not spot_fee_cfg:
                        continue
                    sell_schedule = spot_fee_cfg.schedule_for_pair(pair)
                    adjusted_buy = opp.buy_price * (1 + p2p_fee / 100.0)
                    est_profit, est_percent, base_qty, capital_used = estimate_profit(
                        capital_for_pair,
                        adjusted_buy,
                        opp.sell_price,
                        sell_schedule.taker_fee_percent,
                    )
                    if base_qty <= 0 or capital_used <= 0:
                        continue
                    valid_spot, reason = validate_market_trade(spot_venue, pair, base_qty, opp.sell_price)
                    if not valid_spot:
                        print(f"[SKIP] {pair} {opp.buy_venue}->{opp.sell_venue} {reason}")
                        continue
                    notional = base_qty * opp.buy_price
                    valid_p2p, reason_p2p = validate_p2p_notional(p2p_venue, asset, notional)
                    if not valid_p2p:
                        print(f"[SKIP] {pair} {opp.buy_venue}->{opp.sell_venue} {reason_p2p}")
                        continue
                if est_percent < threshold:
                    continue
                opp.net_percent = est_percent
                opp.notes.setdefault("fiat", fiat)
                liquidity_score = compute_liquidity_score(opp, base_qty)
                volatility_score = compute_volatility_score(pair)
                priority_score = compute_priority_score(est_percent, liquidity_score, volatility_score)
                confidence_label = classify_confidence(
                    est_percent, threshold, liquidity_score, volatility_score, priority_score
                )
                opp.liquidity_score = liquidity_score
                opp.volatility_score = volatility_score
                opp.priority_score = priority_score
                opp.confidence_label = confidence_label
                link_items = build_trade_link_items(opp.buy_venue, opp.sell_venue, opp.pair)
                entry = {
                    "pair": opp.pair,
                    "buy_venue": opp.buy_venue,
                    "sell_venue": opp.sell_venue,
                    "buy_price": opp.buy_price,
                    "sell_price": opp.sell_price,
                    "gross_percent": opp.gross_percent,
                    "net_percent": est_percent,
                    "est_profit_quote": est_profit,
                    "est_percent": est_percent,
                    "base_qty": base_qty,
                    "capital_used_quote": capital_used,
                    "links": link_items,
                    "liquidity_score": liquidity_score,
                    "volatility_score": volatility_score,
                    "priority_score": priority_score,
                    "confidence": confidence_label,
                    "threshold_hit": True,
                    "strategy": opp.strategy,
                    "notes": opp.notes,
                }
                summary_opps.append(entry)
                append_csv(
                    log_csv,
                    opp,
                    est_profit,
                    base_qty,
                    capital_used,
                    opp.buy_depth,
                    opp.sell_depth,
                )
                msg = fmt_alert(
                    opp,
                    est_profit,
                    est_percent,
                    base_qty,
                    capital_for_pair,
                    capital_used,
                    link_items,
                )
                tg_send_message(msg, enabled=tg_enabled)
                log_event(
                    "opportunity.alert",
                    pair=opp.pair,
                    buy_venue=opp.buy_venue,
                    sell_venue=opp.sell_venue,
                    net_percent=est_percent,
                    est_profit=est_profit,
                )
                spot_p2p_alerts += 1
                alert_entry = dict(entry)
                alert_entry["ts"] = int(time.time())
                alert_entry["ts_str"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert_entry["ts"]))
                alert_records.append(alert_entry)

    p2p_cross_alerts = 0
    if is_strategy_enabled("p2p_p2p"):
        for pair in p2p_pairs:
            quotes = {
                venue: quote
                for venue, quote in pair_quotes.get(pair, {}).items()
                if str(getattr(quote, "source", "")).lower() == "p2p"
            }
            if len(quotes) < 2:
                print(f"[SKIP] {pair}: p2p_sin_ofertas")
                continue
            capital_for_pair = get_weighted_capital(capital, pair_weight_cfg, pair)
            if capital_for_pair <= 0:
                continue
            opps = compute_p2p_cross_opportunities(pair, quotes)
            asset, _ = split_pair(pair)
            for opp in opps[:5]:
                buy_fee = float(opp.notes.get("p2p_buy_fee_percent", 0.0) or 0.0)
                sell_fee = float(opp.notes.get("p2p_sell_fee_percent", 0.0) or 0.0)
                adjusted_buy = opp.buy_price * (1 + buy_fee / 100.0)
                adjusted_sell = opp.sell_price * (1 - sell_fee / 100.0)
                est_profit, est_percent, base_qty, capital_used = estimate_profit(
                    capital_for_pair,
                    adjusted_buy,
                    adjusted_sell,
                    0.0,
                )
                if base_qty <= 0 or capital_used <= 0:
                    continue
                notional_buy = base_qty * opp.buy_price
                valid_buy, reason_buy = validate_p2p_notional(opp.buy_venue.replace("_p2p", ""), asset, notional_buy)
                if not valid_buy:
                    print(f"[SKIP] {pair} {opp.buy_venue}->{opp.sell_venue} {reason_buy}")
                    continue
                notional_sell = base_qty * opp.sell_price
                valid_sell, reason_sell = validate_p2p_notional(opp.sell_venue.replace("_p2p", ""), asset, notional_sell)
                if not valid_sell:
                    print(f"[SKIP] {pair} {opp.buy_venue}->{opp.sell_venue} {reason_sell}")
                    continue
                if est_percent < threshold:
                    continue
                opp.net_percent = est_percent
                liquidity_score = 0.0
                volatility_score = compute_volatility_score(pair)
                priority_score = compute_priority_score(est_percent, liquidity_score, volatility_score)
                confidence_label = classify_confidence(
                    est_percent, threshold, liquidity_score, volatility_score, priority_score
                )
                opp.liquidity_score = liquidity_score
                opp.volatility_score = volatility_score
                opp.priority_score = priority_score
                opp.confidence_label = confidence_label
                link_items = build_trade_link_items(opp.buy_venue, opp.sell_venue, opp.pair)
                entry = {
                    "pair": opp.pair,
                    "buy_venue": opp.buy_venue,
                    "sell_venue": opp.sell_venue,
                    "buy_price": opp.buy_price,
                    "sell_price": opp.sell_price,
                    "gross_percent": opp.gross_percent,
                    "net_percent": est_percent,
                    "est_profit_quote": est_profit,
                    "est_percent": est_percent,
                    "base_qty": base_qty,
                    "capital_used_quote": capital_used,
                    "links": link_items,
                    "liquidity_score": liquidity_score,
                    "volatility_score": volatility_score,
                    "priority_score": priority_score,
                    "confidence": confidence_label,
                    "threshold_hit": True,
                    "strategy": opp.strategy,
                    "notes": opp.notes,
                }
                summary_opps.append(entry)
                append_csv(
                    log_csv,
                    opp,
                    est_profit,
                    base_qty,
                    capital_used,
                    None,
                    None,
                )
                msg = fmt_alert(
                    opp,
                    est_profit,
                    est_percent,
                    base_qty,
                    capital_for_pair,
                    capital_used,
                    link_items,
                )
                tg_send_message(msg, enabled=tg_enabled)
                log_event(
                    "opportunity.alert",
                    pair=opp.pair,
                    buy_venue=opp.buy_venue,
                    sell_venue=opp.sell_venue,
                    net_percent=est_percent,
                    est_profit=est_profit,
                )
                p2p_cross_alerts += 1
                alert_entry = dict(entry)
                alert_entry["ts"] = int(time.time())
                alert_entry["ts_str"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert_entry["ts"]))
                alert_records.append(alert_entry)

    summary_opps.sort(key=lambda item: item.get("priority_score", item["net_percent"]), reverse=True)
    if len(summary_opps) > 20:
        summary_opps = summary_opps[:20]

    tri_alerts = 0
    for route in routes:
        route_capital = get_weighted_capital(capital, triangle_weight_cfg, route.identifier)
        if route_capital <= 0:
            continue
        opp = compute_triangular_opportunity(route, pair_quotes, fee_map, route_capital)
        if not opp or opp.net_percent < threshold:
            continue

        if tri_log_csv:
            append_triangular_csv(tri_log_csv, opp)
        fee_cfg = fee_map.get(route.venue)
        fee_pct = fee_cfg.default.taker_fee_percent if fee_cfg else 0.0
        msg = fmt_triangular_alert(opp, fee_pct)
        tg_send_message(msg, enabled=tg_enabled)
        tri_alerts += 1

    total_latency_ms = int((time.time() - run_start) * 1000)
    metrics_data = metrics_snapshot()

    total_alerts = spot_alerts + spot_p2p_alerts + p2p_cross_alerts

    summary = {
        "ts": run_ts,
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(run_ts)),
        "threshold": threshold,
        "base_threshold": base_threshold,
        "dynamic_threshold": DYNAMIC_THRESHOLD_PERCENT,
        "capital": capital,
        "pairs": pairs,
        "opportunities": summary_opps,
        "alerts_sent": total_alerts,
        "alerts_spot": spot_alerts,
        "alerts_p2p": spot_p2p_alerts + p2p_cross_alerts,
        "triangular_alerts": tri_alerts,
        "quote_latency_ms": quote_latency_ms,
        "run_latency_ms": total_latency_ms,
        "metrics": metrics_data,
    }

    if alert_records:
        alert_records.sort(key=lambda item: item["ts"], reverse=True)

    with STATE_LOCK:
        DASHBOARD_STATE["last_run_summary"] = summary
        DASHBOARD_STATE["exchange_metrics"] = metrics_data
        if alert_records:
            history = DASHBOARD_STATE.get("latest_alerts", [])
            history.extend(alert_records)
            history.sort(key=lambda item: item.get("ts", 0), reverse=True)
            DASHBOARD_STATE["latest_alerts"] = history[:MAX_ALERT_HISTORY]

    degradation_alerts = build_degradation_alerts(metrics_data)
    for alert_msg in degradation_alerts:
        tg_send_message(f"🚨 {alert_msg}", enabled=tg_enabled)

    update_prometheus_metrics(metrics_data, summary, tri_alerts)

    backup_targets = [log_csv]
    if tri_log_csv:
        backup_targets.append(tri_log_csv)
    ensure_log_backups(backup_targets)

    print(
        "Run complete. Oportunidades enviadas: "
        f"{spot_alerts} (spot) / {spot_p2p_alerts + p2p_cross_alerts} (p2p) / {tri_alerts} (triangulares)"
        f" · latencia total {total_latency_ms} ms"
    )

# =========================
# CLI
# =========================
def main():
    ap = argparse.ArgumentParser(description="Arbitrage TeleBot (spot, inventario) - web-ready")
    ap.add_argument("--once", action="store_true", help="Ejecuta una vez y termina")
    ap.add_argument("--loop", action="store_true", help="Ejecuta en loop continuo")
    ap.add_argument("--interval", type=int, default=int(os.getenv("INTERVAL_SECONDS", "30")), help="Segundos entre corridas en modo loop")
    ap.add_argument("--web", action="store_true", help="Expone /health y corre el loop en background")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "10000")), help="Puerto HTTP para /health (Render usa $PORT)")
    ap.add_argument("--diagnose-exchanges", action="store_true", help="Verifica conectividad de cada exchange y par configurado")
    ap.add_argument("--diagnose-pair", action="append", dest="diagnose_pairs", help="Limita el diagnóstico a uno o más pares (puede repetirse)")
    ap.add_argument("--diagnose-venue", action="append", dest="diagnose_venues", help="Limita el diagnóstico a uno o más exchanges (puede repetirse)")

    args = ap.parse_args()

    if args.diagnose_exchanges:
        selected_pairs = normalize_pair_list(args.diagnose_pairs or CONFIG["pairs"])
        adapters = build_adapters()
        if args.diagnose_venues:
            venues_filter = {venue.strip().lower() for venue in args.diagnose_venues if venue}
            adapters = {name: adapter for name, adapter in adapters.items() if name in venues_filter}
            missing = sorted(venues_filter.difference(adapters.keys()))
            if missing:
                print("Exchanges no habilitados:", ", ".join(missing))
        if not selected_pairs:
            print("Sin pares para diagnosticar")
            return
        if not adapters:
            print("Sin exchanges habilitados para diagnosticar")
            return
        start_ts = time.time()
        results = diagnose_exchange_pairs(selected_pairs, adapters)
        results.sort(key=lambda item: (item["venue"], item["pair"]))
        status_totals: Dict[str, int] = {}
        for result in results:
            status_totals[result["status"]] = status_totals.get(result["status"], 0) + 1
            venue = result["venue"]
            pair = result["pair"]
            latency = f"{result['latency_ms']:.1f} ms"
            if result["status"] == "ok":
                source = result.get("source") or ""
                offline_flag = " (offline)" if result.get("offline_source") else ""
                bid = result.get("bid")
                ask = result.get("ask")
                print(
                    f"[{venue}] {pair}: OK bid={bid:.8f} ask={ask:.8f} · origen={source or 'desconocido'} · latencia {latency}{offline_flag}"
                )
            elif result["status"] == "no_data":
                print(f"[{venue}] {pair}: SIN DATOS · latencia {latency}")
            else:
                error = result.get("error") or "desconocido"
                print(f"[{venue}] {pair}: ERROR ({error}) · latencia {latency}")
        elapsed = time.time() - start_ts
        total = sum(status_totals.values())
        summary_parts = [f"total={total}"]
        for status, count in sorted(status_totals.items()):
            summary_parts.append(f"{status}={count}")
        summary = " · ".join(summary_parts)
        print(f"Diagnóstico completado en {elapsed:.2f} s · {summary}")
        return

    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    if tg_enabled:
        tg_sync_command_menu(enabled=True)
        tg_send_message(
            "🤖 Bot reiniciado.\n\n" + format_command_help(),
            enabled=True,
        )
    if tg_enabled and (args.loop or args.web):
        ensure_telegram_polling_thread(enabled=True, interval=1.0)

    if args.web:
        t = threading.Thread(target=run_loop_forever, args=(args.interval,), daemon=True)
        t.start()
        serve_http(args.port)
        return

    if args.once and args.loop:
        log_event("cli.invalid_args", once=args.once, loop=args.loop)
        return

    if args.once or not args.loop:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception as e:
            log_event("loop.error", error=str(e))
        time.sleep(max(5, args.interval))

if __name__ == "__main__":
    main()
