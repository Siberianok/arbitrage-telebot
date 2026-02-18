"""Adapters de mercados centralizados y P2P.

Capa inicial de refactor: reexporta implementaciones existentes para
mantener compatibilidad durante la migración incremental.
"""

from .base import ExchangeAdapter
from .binance import Binance
from .bybit import Bybit
from .kucoin import KuCoin
from .okx import OKX
from .p2p import GenericP2PMarketplace

__all__ = [
    "ExchangeAdapter",
    "Binance",
    "Bybit",
    "KuCoin",
    "OKX",
    "GenericP2PMarketplace",
]
