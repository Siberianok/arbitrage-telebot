#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arbitrage TeleBot (v1, single-file)
-----------------------------------
- Spot cross-exchange arbitrage (inventario) con alertas por Telegram
- CEX incluidos (por ahora): Binance, Bybit, KuCoin, OKX
- Pairs configurables (BTC/USDT, ETH/USDT, etc.)
- Spread neto = spread bruto - (fee taker buy + fee taker sell)
- Umbral configurable (0.8% por defecto) + simulación de PnL sobre capital
- Logs CSV de oportunidades

Requisitos: Python 3.9+ y 'requests'
    pip install requests

Variables de entorno (Telegram):
    export TG_BOT_TOKEN="123456:ABCDEF..."
    export TG_CHAT_IDS="-1001234567890,123456789"   # canal,usuario (coma-separado)

Uso:
    python arbitrage_telebot.py --once         # una corrida
    python arbitrage_telebot.py --loop         # loop continuo
    python arbitrage_telebot.py --interval 30  # segundos entre corridas (loop)
"""

import os
import csv
import time
import math
import json
import argparse
import itertools
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple

import requests

# =========================
# CONFIG BÁSICA (editá acá)
# =========================
CONFIG = {
    "threshold_percent": 0.8,      # alerta si neto >= 0.80%
    "pairs": [
        "BTC/USDT",
        "ETH/USDT",
        "XRP/USDT",
        "ADA/USDT",
        "ALGO/USDT",
        "SHIB/USDT",
    ],
    "simulation_capital_quote": 10_000,  # capital (USDT) para estimar PnL en alerta
    "venues": {
        "binance": {"enabled": True,  "taker_fee_percent": 0.10},
        "bybit":   {"enabled": True,  "taker_fee_percent": 0.10},
        "kucoin":  {"enabled": True,  "taker_fee_percent": 0.10},
        "okx":     {"enabled": True,  "taker_fee_percent": 0.10},

        # TODO: activar e implementar más venues (Bitget, BingX, HTX, AscendEX, etc.)
        # "bitget": {"enabled": False, "taker_fee_percent": 0.10},
        # "bingx":  {"enabled": False, "taker_fee_percent": 0.10},
        # "htx":    {"enabled": False, "taker_fee_percent": 0.10},
    },
    "telegram": {
        "enabled": True,                 # poner False para modo silencioso
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_ids_env": "TG_CHAT_IDS",   # coma-separado: "-100123...,123456..."
    },
    "log_csv_path": "logs/opportunities.csv",
}

# =========================
# Helpers HTTP
# =========================
class HttpError(Exception):
    pass

def http_get_json(url: str, params: Optional[dict] = None, timeout: int = 8, retries: int = 3) -> dict:
    last_exc = None
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code != 200:
                raise HttpError(f"HTTP {r.status_code} {url} params={params}")
            return r.json()
        except Exception as e:
            last_exc = e
            time.sleep(0.5)
    raise last_exc or HttpError("GET failed")

# =========================
# Telegram (HTTP API)
# =========================
def tg_send_message(text: str, enabled: bool = True) -> None:
    if not enabled:
        print("[TELEGRAM DISABLED] Would send:\n" + text)
        return

    token = os.getenv(CONFIG["telegram"]["bot_token_env"], "").strip()
    chat_ids_env = os.getenv(CONFIG["telegram"]["chat_ids_env"], "").strip()

    if not token or not chat_ids_env:
        print("[TELEGRAM] Faltan variables de entorno (TG_BOT_TOKEN / TG_CHAT_IDS). Mensaje no enviado.")
        print("Mensaje:\n" + text)
        return

    chat_ids = [cid.strip() for cid in chat_ids_env.split(",") if cid.strip()]
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in chat_ids:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}
            r = requests.post(base, data=payload, timeout=8)
            if r.status_code != 200:
                print(f"[TELEGRAM] Error HTTP {r.status_code} chat_id={cid} -> {r.text}")
        except Exception as e:
            print(f"[TELEGRAM] Error enviando a {cid}: {e}")

# =========================
# Modelo y Fees
# =========================
@dataclass
class Quote:
    symbol: str  # símbolo normalizado por exchange (ej: BTCUSDT, BTC-USDT)
    bid: float
    ask: float
    ts: int

@dataclass
class VenueFees:
    taker_fee_percent: float  # ej: 0.10 = 0.10%

def total_percent_fee(buy_fees: VenueFees, sell_fees: VenueFees) -> float:
    return buy_fees.taker_fee_percent + sell_fees.taker_fee_percent

# =========================
# Adapters de Exchanges
# =========================
class ExchangeAdapter:
    name: str
    def normalize_symbol(self, pair: str) -> str:
        raise NotImplementedError
    def fetch_quote(self, pair: str) -> Optional[Quote]:
        raise NotImplementedError

class Binance(ExchangeAdapter):
    name = "binance"
    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "")
    def fetch_quote(self, pair: str) -> Optional[Quote]:
        sym = self.normalize_symbol(pair)
        url = "https://api.binance.com/api/v3/ticker/bookTicker"
        data = http_get_json(url, params={"symbol": sym})
        bid = float(data["bidPrice"]); ask = float(data["askPrice"])
        return Quote(sym, bid, ask, int(time.time()*1000))

class Bybit(ExchangeAdapter):
    name = "bybit"
    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "")
    def fetch_quote(self, pair: str) -> Optional[Quote]:
        sym = self.normalize_symbol(pair)
        url = "https://api.bybit.com/v5/market/tickers"
        data = http_get_json(url, params={"category":"spot", "symbol": sym})
        try:
            item = data["result"]["list"][0]
            bid = float(item["bid1Price"]); ask = float(item["ask1Price"])
            return Quote(sym, bid, ask, int(time.time()*1000))
        except Exception:
            return None

class KuCoin(ExchangeAdapter):
    name = "kucoin"
    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "-")
    def fetch_quote(self, pair: str) -> Optional[Quote]:
        sym = self.normalize_symbol(pair)
        url = "https://api.kucoin.com/api/v1/market/orderbook/level1"
        data = http_get_json(url, params={"symbol": sym})
        try:
            d = data["data"]
            bid = float(d["bestBid"]); ask = float(d["bestAsk"])
            return Quote(sym, bid, ask, int(time.time()*1000))
        except Exception:
            return None

class OKX(ExchangeAdapter):
    name = "okx"
    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "-")
    def fetch_quote(self, pair: str) -> Optional[Quote]:
        sym = self.normalize_symbol(pair)
        url = "https://www.okx.com/api/v5/market/ticker"
        data = http_get_json(url, params={"instId": sym})
        try:
            item = data["data"][0]
            bid = float(item["bidPx"]); ask = float(item["askPx"])
            return Quote(sym, bid, ask, int(time.time()*1000))
        except Exception:
            return None

def build_adapters() -> Dict[str, ExchangeAdapter]:
    adapters: Dict[str, ExchangeAdapter] = {}
    vcfg = CONFIG["venues"]
    if vcfg.get("binance", {}).get("enabled", False): adapters["binance"] = Binance()
    if vcfg.get("bybit",   {}).get("enabled", False): adapters["bybit"]   = Bybit()
    if vcfg.get("kucoin",  {}).get("enabled", False): adapters["kucoin"]  = KuCoin()
    if vcfg.get("okx",     {}).get("enabled", False): adapters["okx"]     = OKX()
    # TODO: agregar más adapters aquí (Bitget, BingX, HTX, AscendEX, etc.)
    return adapters

def build_fee_map() -> Dict[str, VenueFees]:
    fee_map: Dict[str, VenueFees] = {}
    for vname, v in CONFIG["venues"].items():
        if not v.get("enabled", False): 
            continue
        fee_map[vname] = VenueFees(taker_fee_percent=float(v.get("taker_fee_percent", 0.10)))
    return fee_map

# =========================
# Engine de oportunidades
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

def compute_opportunities_for_pair(pair: str,
                                   quotes: Dict[str, Quote],
                                   fees: Dict[str, VenueFees]) -> List[Opportunity]:
    venues = list(quotes.keys())
    out: List[Opportunity] = []
    for i, j in itertools.permutations(range(len(venues)), 2):
        buy_v = venues[i]; sell_v = venues[j]
        qb = quotes[buy_v]; qs = quotes[sell_v]
        if qb is None or qs is None: 
            continue

        buy_price = qb.ask; sell_price = qs.bid
        if buy_price <= 0 or sell_price <= 0:
            continue

        gross = (sell_price - buy_price) / buy_price * 100.0
        f_buy = fees.get(buy_v); f_sell = fees.get(sell_v)
        if not f_buy or not f_sell:
            continue
        net = gross - total_percent_fee(f_buy, f_sell)

        out.append(Opportunity(pair, buy_v, sell_v, buy_price, sell_price, gross, net))

    return sorted(out, key=lambda o: o.net_percent, reverse=True)

# =========================
# Simulación PnL (simple)
# =========================
def estimate_profit(capital_quote: float, buy_price: float, sell_price: float, total_percent_fee: float) -> Tuple[float, float, float]:
    """
    Estimación simple: compra base = capital/buy_price; vende al sell_price
    Aplica fee total porcentual sobre el capital efectivo.
    Retorna (profit_quote, net_percent, base_qty)
    """
    if buy_price <= 0 or sell_price <= 0 or capital_quote <= 0:
        return 0.0, 0.0, 0.0
    base_qty = capital_quote / buy_price
    gross_proceeds = base_qty * sell_price
    fee_loss = (total_percent_fee / 100.0) * capital_quote
    net_proceeds = gross_proceeds - fee_loss
    profit = net_proceeds - capital_quote
    net_pct = (profit / capital_quote) * 100.0
    return profit, net_pct, base_qty

# =========================
# Logging CSV
# =========================
def append_csv(path: str, opp: Opportunity, est_profit: float, base_qty: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["ts","pair","buy_venue","sell_venue","buy_price","sell_price","gross_%","net_%","est_profit_quote","base_qty"])
        w.writerow([
            int(time.time()), opp.pair, opp.buy_venue, opp.sell_venue,
            f"{opp.buy_price:.8f}", f"{opp.sell_price:.8f}",
            f"{opp.gross_percent:.4f}", f"{opp.net_percent:.4f}",
            f"{est_profit:.4f}", f"{base_qty:.8f}"
        ])

# =========================
# Formato de alerta
# =========================
def fmt_alert(opp: Opportunity, est_profit: float, est_percent: float, base_qty: float, capital_quote: float) -> str:
    return (
        "ARBITRAJE SPOT (inventario)\n"
        f"Par: {opp.pair}\n"
        f"Comprar en {opp.buy_venue}: {opp.buy_price:.6f}\n"
        f"Vender en {opp.sell_venue}: {opp.sell_price:.6f}\n"
        f"Spread bruto: {opp.gross_percent:.3f}%  |  Neto: {opp.net_percent:.3f}%\n"
        f"Simulacion sobre {capital_quote:.0f} USDT: PnL ~{est_profit:.2f} USDT  (~{est_percent:.3f}%)\n"
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

# =========================
# Run (una vez)
# =========================
def run_once() -> None:
    adapters = build_adapters()
    fee_map = build_fee_map()
    if not adapters:
        print("No hay venues habilitados en CONFIG['venues'].")
        return

    pairs = CONFIG["pairs"]
    threshold = float(CONFIG["threshold_percent"])
    capital = float(CONFIG["simulation_capital_quote"])
    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    log_csv = CONFIG["log_csv_path"]

    # Fetch quotes por par/venue
    pair_quotes: Dict[str, Dict[str, Quote]] = {p: {} for p in pairs}
    for pair in pairs:
        for vname, adapter in adapters.items():
            try:
                q = adapter.fetch_quote(pair)
            except Exception as e:
                q = None
                print(f"[{vname}] error fetch {pair}: {e}")
            if q:
                pair_quotes[pair][vname] = q

    # Oportunidades y alertas
    alerts = 0
    for pair, quotes in pair_quotes.items():
        if len(quotes) < 2:
            continue
        opps = compute_opportunities_for_pair(pair, quotes, fee_map)
        for opp in opps:
            if opp.net_percent >= threshold:
                total_fee_pct = fee_map[opp.buy_venue].taker_fee_percent + fee_map[opp.sell_venue].taker_fee_percent
                est_profit, est_percent, base_qty = estimate_profit(capital, opp.buy_price, opp.sell_price, total_fee_pct)

                append_csv(log_csv, opp, est_profit, base_qty)
                msg = fmt_alert(opp, est_profit, est_percent, base_qty, capital)
                tg_send_message(msg, enabled=tg_enabled)
                alerts += 1

    print(f"Run complete. Oportunidades enviadas: {alerts}")

# =========================
# CLI
# =========================
def main():
    ap = argparse.ArgumentParser(description="Arbitrage TeleBot (spot, inventario)")
    ap.add_argument("--once", action="store_true", help="Ejecuta una vez y termina")
    ap.add_argument("--loop", action="store_true", help="Ejecuta en loop continuo")
    ap.add_argument("--interval", type=int, default=30, help="Segundos entre corridas en modo loop")
    args = ap.parse_args()

    if args.once and args.loop:
        print("Elegí --once o --loop, no ambos.")
        return

    if args.once or not args.loop:
        run_once()
        return

    # loop
    while True:
        try:
            run_once()
        except Exception as e:
            print("[ERROR]", e)
        time.sleep(max(5, args.interval))

if __name__ == "__main__":
    main()
