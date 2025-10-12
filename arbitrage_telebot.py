#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import csv
import time
import math
import json
import argparse
import itertools
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple, Set

import requests

# =========================
# CONFIG
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
        "SOL/USDT",
        "MATIC/USDT",
        "BTC/USDC",
        "ETH/BTC",
        "BTC/EUR",
        "ETH/EUR",
        "USDC/USDT",
        "BUSD/USDT",
        "BUSD/USDC",
    ],
    "simulation_capital_quote": 10_000,  # capital (USDT) para estimar PnL en alerta
    "capital_weights": {
        "pairs": {
            "default": 1.0,
            "BTC/USDT": 1.5,
            "ETH/USDT": 1.2,
            "ETH/BTC": 0.8,
        },
        "triangles": {
            "default": 0.6,
            "binance::USDT-USDC-BUSD": 1.0,
            "bybit::USDT-BTC-USDC": 0.8,
        },
    },
    "venues": {
        "binance": {"enabled": True,  "taker_fee_percent": 0.10},
        "bybit":   {"enabled": True,  "taker_fee_percent": 0.10},
        "kucoin":  {"enabled": True,  "taker_fee_percent": 0.10},
        "okx":     {"enabled": True,  "taker_fee_percent": 0.10},
        # add more venues aquí
    },
    "triangular_routes": [
        {
            "name": "USDT-USDC-BUSD",
            "venue": "binance",
            "start_asset": "USDT",
            "legs": [
                {"pair": "USDC/USDT", "action": "BUY_BASE"},
                {"pair": "BUSD/USDC", "action": "BUY_BASE"},
                {"pair": "BUSD/USDT", "action": "SELL_BASE"},
            ],
        },
        {
            "name": "USDT-BTC-USDC",
            "venue": "bybit",
            "start_asset": "USDT",
            "legs": [
                {"pair": "BTC/USDT", "action": "BUY_BASE"},
                {"pair": "BTC/USDC", "action": "SELL_BASE"},
                {"pair": "USDC/USDT", "action": "SELL_BASE"},
            ],
        },
    ],
    "telegram": {
        "enabled": True,                 # poner False para pruebas sin enviar
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_ids_env": "TG_CHAT_IDS",   # coma-separado: "-100123...,123456..."
    },
    "log_csv_path": "logs/opportunities.csv",
    "triangular_log_csv_path": "logs/triangular_opportunities.csv",
}

TELEGRAM_CHAT_IDS: Set[str] = set()
TELEGRAM_LAST_UPDATE_ID = 0
TELEGRAM_POLLING_THREAD: Optional[threading.Thread] = None


COMMANDS_HELP: List[Tuple[str, str]] = [
    ("/help", "Muestra este listado de comandos"),
    ("/ping", "Responde con 'pong' para verificar conectividad"),
    ("/status", "Resume configuración actual y chats registrados"),
    ("/threshold <valor>", "Consulta o actualiza el umbral de alerta (%)"),
    ("/pairs", "Lista los pares configurados"),
    ("/addpair <PAR>", "Agrega un par nuevo al monitoreo"),
    ("/delpair <PAR>", "Elimina un par del monitoreo"),
    ("/test", "Envía una señal de prueba"),
]


def format_command_help() -> str:
    lines = ["Comandos disponibles:"]
    for cmd, desc in COMMANDS_HELP:
        lines.append(f"{cmd} — {desc}")
    return "\n".join(lines)


def get_bot_token() -> str:
    return os.getenv(CONFIG["telegram"]["bot_token_env"], "").strip()


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

# =========================
# HTTP / Health
# =========================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/live", "/ready"):
            self.send_response(200); self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_HEAD(self):
        if self.path in ("/", "/health", "/live", "/ready"):
            self.send_response(200)
        else:
            self.send_response(404)
        self.end_headers()

def serve_http(port: int):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[WEB] listening on 0.0.0.0:{port}")
    server.serve_forever()

def run_loop_forever(interval: int):
    while True:
        try:
            run_once()
        except Exception as e:
            print("[ERROR loop]", e)
        time.sleep(max(5, interval))

# =========================
# HTTP helpers
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
def register_telegram_chat(chat_id) -> str:
    cid = str(chat_id)
    if cid not in TELEGRAM_CHAT_IDS:
        TELEGRAM_CHAT_IDS.add(cid)
        os.environ[CONFIG["telegram"]["chat_ids_env"]] = ",".join(sorted(TELEGRAM_CHAT_IDS))
        print(f"[TELEGRAM] Nuevo chat registrado: {cid}")
    return cid


def get_registered_chat_ids() -> List[str]:
    return sorted(TELEGRAM_CHAT_IDS)


def tg_send_message(text: str, enabled: bool = True, chat_id: Optional[str] = None) -> None:
    if not enabled:
        print("[TELEGRAM DISABLED] Would send:\n" + text)
        return

    token = get_bot_token()
    if not token:
        print("[TELEGRAM] Falta TG_BOT_TOKEN. No se envía.")
        print("Mensaje:\n" + text)
        return

    targets: List[str]
    if chat_id is not None:
        targets = [str(chat_id)]
    else:
        targets = get_registered_chat_ids()

    if not targets:
        print("[TELEGRAM] No hay chats registrados. No se envía.")
        print("Mensaje:\n" + text)
        return

    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in targets:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}
            r = requests.post(base, data=payload, timeout=8)
            if r.status_code != 200:
                print(f"[TELEGRAM] HTTP {r.status_code} chat_id={cid} -> {r.text}")
        except Exception as e:
            print(f"[TELEGRAM] Error enviando a {cid}: {e}")


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
        raise HttpError(f"HTTP {r.status_code} -> {r.text}")

    data = r.json()
    if not data.get("ok"):
        raise HttpError(f"Respuesta no OK en {method}: {data}")
    return data


def tg_handle_command(command: str, argument: str, chat_id: str, enabled: bool) -> None:
    command = command.lower()
    register_telegram_chat(chat_id)

    if command == "/start":
        response = (
            "Hola! Ya estás registrado para recibir señales.\n"
            f"Threshold actual: {CONFIG['threshold_percent']:.3f}%\n"
            f"{format_command_help()}"
        )
        tg_send_message(response, enabled=enabled, chat_id=chat_id)
        return

    if command == "/help":
        tg_send_message(format_command_help(), enabled=enabled, chat_id=chat_id)
        return

    if command == "/ping":
        tg_send_message("pong", enabled=enabled, chat_id=chat_id)
        return

    if command == "/status":
        pairs = CONFIG["pairs"]
        chats = get_registered_chat_ids()
        response = (
            "Estado actual:\n"
            f"Threshold: {CONFIG['threshold_percent']:.3f}%\n"
            f"Pares ({len(pairs)}): {', '.join(pairs) if pairs else 'sin pares'}\n"
            f"Chats registrados: {', '.join(chats) if chats else 'ninguno'}"
        )
        tg_send_message(response, enabled=enabled, chat_id=chat_id)
        return

    if command == "/threshold":
        if not argument:
            tg_send_message(
                f"Threshold actual: {CONFIG['threshold_percent']:.3f}%",
                enabled=enabled,
                chat_id=chat_id,
            )
            return
        try:
            value = float(argument.replace("%", "").strip())
        except ValueError:
            tg_send_message("Valor inválido. Ej: /threshold 0.8", enabled=enabled, chat_id=chat_id)
            return
        CONFIG["threshold_percent"] = value
        tg_send_message(
            f"Nuevo threshold guardado: {CONFIG['threshold_percent']:.3f}%",
            enabled=enabled,
            chat_id=chat_id,
        )
        return

    if command == "/pairs":
        pairs = CONFIG["pairs"]
        if not pairs:
            tg_send_message("No hay pares configurados.", enabled=enabled, chat_id=chat_id)
        else:
            formatted = "\n".join(f"- {p}" for p in pairs)
            tg_send_message(f"Pares actuales:\n{formatted}", enabled=enabled, chat_id=chat_id)
        return

    if command == "/addpair":
        if not argument:
            tg_send_message("Uso: /addpair BTC/USDT", enabled=enabled, chat_id=chat_id)
            return
        pair = argument.upper().strip()
        if pair in CONFIG["pairs"]:
            tg_send_message(f"{pair} ya estaba en la lista.", enabled=enabled, chat_id=chat_id)
        else:
            CONFIG["pairs"].append(pair)
            tg_send_message(f"Par agregado: {pair}", enabled=enabled, chat_id=chat_id)
        return

    if command == "/delpair":
        if not argument:
            tg_send_message("Uso: /delpair BTC/USDT", enabled=enabled, chat_id=chat_id)
            return
        pair = argument.upper().strip()
        if pair not in CONFIG["pairs"]:
            tg_send_message(f"{pair} no está en la lista.", enabled=enabled, chat_id=chat_id)
        else:
            CONFIG["pairs"] = [p for p in CONFIG["pairs"] if p != pair]
            tg_send_message(f"Par eliminado: {pair}", enabled=enabled, chat_id=chat_id)
        return

    if command == "/test":
        tg_send_message("Señal de prueba ✅", enabled=enabled, chat_id=chat_id)
        return

    tg_send_message("Comando no reconocido. Probá /help para ver el listado.", enabled=enabled, chat_id=chat_id)


def tg_process_updates(enabled: bool = True) -> None:
    global TELEGRAM_LAST_UPDATE_ID

    if not get_bot_token():
        return

    params: Dict[str, int] = {}
    if TELEGRAM_LAST_UPDATE_ID:
        params["offset"] = TELEGRAM_LAST_UPDATE_ID + 1

    try:
        data = tg_api_request("getUpdates", params=params or None)
    except Exception as e:
        print(f"[TELEGRAM] Error leyendo updates: {e}")
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

        register_telegram_chat(chat_id)
        if not text.startswith("/"):
            continue

        parts = text.split(maxsplit=1)
        command = parts[0]
        argument = parts[1] if len(parts) > 1 else ""
        tg_handle_command(command, argument, str(chat_id), enabled)


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
                print(f"[TELEGRAM] Error en polling: {exc}")
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
    return adapters

def build_fee_map() -> Dict[str, VenueFees]:
    fee_map: Dict[str, VenueFees] = {}
    for vname, v in CONFIG["venues"].items():
        if not v.get("enabled", False): 
            continue
        fee_map[vname] = VenueFees(taker_fee_percent=float(v.get("taker_fee_percent", 0.10)))
    return fee_map

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
# Simulación PnL (simple)
# =========================
def estimate_profit(capital_quote: float, buy_price: float, sell_price: float, total_percent_fee: float) -> Tuple[float, float, float]:
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

# =========================
# Run (una vez)
# =========================
def run_once() -> None:
    adapters = build_adapters()
    fee_map = build_fee_map()
    if not adapters:
        print("No hay venues habilitados en CONFIG['venues'].")
        return

    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    polling_active = TELEGRAM_POLLING_THREAD and TELEGRAM_POLLING_THREAD.is_alive()
    if tg_enabled and not polling_active:
        tg_process_updates(enabled=tg_enabled)

    routes = load_triangular_routes()
    pairs = list(dict.fromkeys(CONFIG["pairs"]))
    extra_pairs = {leg.pair for route in routes for leg in route.legs}
    all_pairs = sorted(set(pairs) | extra_pairs)
    threshold = float(CONFIG["threshold_percent"])
    capital = float(CONFIG["simulation_capital_quote"])
    log_csv = CONFIG["log_csv_path"]
    tri_log_csv = CONFIG.get("triangular_log_csv_path")
    pair_weight_cfg = CONFIG.get("capital_weights", {}).get("pairs", {})
    triangle_weight_cfg = CONFIG.get("capital_weights", {}).get("triangles", {})

    pair_quotes: Dict[str, Dict[str, Quote]] = {p: {} for p in all_pairs}
    for pair in all_pairs:
        for vname, adapter in adapters.items():
            try:
                q = adapter.fetch_quote(pair)
            except Exception as e:
                q = None
                print(f"[{vname}] error fetch {pair}: {e}")
            if q:
                pair_quotes[pair][vname] = q

    alerts = 0
    for pair in pairs:
        quotes = pair_quotes.get(pair, {})
        if len(quotes) < 2:
            continue
        capital_for_pair = get_weighted_capital(capital, pair_weight_cfg, pair)
        if capital_for_pair <= 0:
            continue
        opps = compute_opportunities_for_pair(pair, quotes, fee_map)
        for opp in opps:
            if opp.net_percent >= threshold:
                total_fee_pct = fee_map[opp.buy_venue].taker_fee_percent + fee_map[opp.sell_venue].taker_fee_percent
                est_profit, est_percent, base_qty = estimate_profit(capital_for_pair, opp.buy_price, opp.sell_price, total_fee_pct)

                append_csv(log_csv, opp, est_profit, base_qty)
                msg = fmt_alert(opp, est_profit, est_percent, base_qty, capital_for_pair)
                tg_send_message(msg, enabled=tg_enabled)
                alerts += 1

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
        fee_pct = fee_cfg.taker_fee_percent if fee_cfg else 0.0
        msg = fmt_triangular_alert(opp, fee_pct)
        tg_send_message(msg, enabled=tg_enabled)
        tri_alerts += 1

    print(f"Run complete. Oportunidades enviadas: {alerts} (cross) / {tri_alerts} (triangulares)")

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

    args = ap.parse_args()

    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    if tg_enabled and (args.loop or args.web):
        ensure_telegram_polling_thread(enabled=True, interval=1.0)

    if args.web:
        t = threading.Thread(target=run_loop_forever, args=(args.interval,), daemon=True)
        t.start()
        serve_http(args.port)
        return

    if args.once and args.loop:
        print("Elegí --once o --loop, no ambos.")
        return

    if args.once or not args.loop:
        run_once(); return

    while True:
        try:
            run_once()
        except Exception as e:
            print("[ERROR]", e)
        time.sleep(max(5, args.interval))

if __name__ == "__main__":
    main()
