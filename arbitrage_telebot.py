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
from collections import deque
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
    ],
    "simulation_capital_quote": 10_000,  # capital (USDT) para estimar PnL en alerta
    "venues": {
        "binance": {"enabled": True,  "taker_fee_percent": 0.10},
        "bybit":   {"enabled": True,  "taker_fee_percent": 0.10},
        "kucoin":  {"enabled": True,  "taker_fee_percent": 0.10},
        "okx":     {"enabled": True,  "taker_fee_percent": 0.10},
        # add more venues aquí
    },
    "telegram": {
        "enabled": True,                 # poner False para pruebas sin enviar
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_ids_env": "TG_CHAT_IDS",   # coma-separado: "-100123...,123456..."
    },
    "log_csv_path": "logs/opportunities.csv",
}

_log_path_override = os.getenv("LOG_CSV_PATH")
if _log_path_override:
    CONFIG["log_csv_path"] = _log_path_override

TELEGRAM_CHAT_IDS: Set[str] = set()
TELEGRAM_LAST_UPDATE_ID = 0
TELEGRAM_POLLING_THREAD: Optional[threading.Thread] = None

# =========================
# Runtime metrics
# =========================
METRICS = {
    "fetch_latencies_ms": deque(maxlen=200),
    "last_run_ts": None,
    "last_run_alerts": 0,
    "quotes": {},
    "last_quotes_update_ts": None,
    "last_telegram_send_ts": None,
    "last_telegram_send_text": None,
    "last_telegram_send_ok": None,
    "last_telegram_error": None,
}


def record_fetch_latency(duration_ms: float) -> None:
    METRICS["fetch_latencies_ms"].append(float(duration_ms))


def avg_fetch_latency_ms() -> Optional[float]:
    latencies = METRICS["fetch_latencies_ms"]
    if not latencies:
        return None
    return sum(latencies) / len(latencies)


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
    """Expose JSON health information with latency and freshness metrics."""

    def _build_payload(self) -> dict:
        now = int(time.time())
        average_latency = avg_fetch_latency_ms()
        quotes_summary = {}
        for pair, venues in METRICS.get("quotes", {}).items():
            quotes_summary[pair] = {}
            for venue, data in venues.items():
                quotes_summary[pair][venue] = {
                    "bid": data.get("bid"),
                    "ask": data.get("ask"),
                    "ts": data.get("ts"),
                    "age_seconds": max(0, now - int(data.get("ts", now))),
                }

        status = "ok"
        last_run_ts = METRICS.get("last_run_ts")
        if last_run_ts and now - int(last_run_ts) > max(120, int(os.getenv("INTERVAL_SECONDS", "30")) * 4):
            status = "stale"

        payload = {
            "status": status,
            "timestamp": now,
            "last_run_ts": last_run_ts,
            "last_run_alerts": METRICS.get("last_run_alerts"),
            "average_fetch_latency_ms": average_latency,
            "last_quotes_update_ts": METRICS.get("last_quotes_update_ts"),
            "last_telegram_send_ts": METRICS.get("last_telegram_send_ts"),
            "last_telegram_send_ok": METRICS.get("last_telegram_send_ok"),
            "last_telegram_error": METRICS.get("last_telegram_error"),
            "last_pairs_with_data": METRICS.get("last_pairs_with_data", {}),
            "quotes": quotes_summary,
        }
        if METRICS.get("last_telegram_send_text"):
            payload["last_telegram_preview"] = METRICS["last_telegram_send_text"]
        if average_latency is not None:
            payload["average_fetch_latency_ms"] = round(average_latency, 3)
        return payload

    def _build_prometheus_metrics(self) -> bytes:
        payload = self._build_payload()
        lines = [
            "# HELP arbitrage_bot_status Overall health status (0=ok,1=stale)",
            "# TYPE arbitrage_bot_status gauge",
        ]
        status_value = 0 if payload.get("status") == "ok" else 1
        lines.append(f"arbitrage_bot_status {status_value}")

        for key in ("last_run_ts", "last_quotes_update_ts", "last_telegram_send_ts"):
            value = payload.get(key)
            if value is not None:
                lines.append(f"# TYPE arbitrage_bot_{key} gauge")
                lines.append(f"arbitrage_bot_{key} {int(value)}")

        latency = payload.get("average_fetch_latency_ms")
        if latency is not None:
            lines.append("# TYPE arbitrage_bot_average_fetch_latency_ms gauge")
            lines.append(f"arbitrage_bot_average_fetch_latency_ms {float(latency)}")

        alerts = payload.get("last_run_alerts")
        if alerts is not None:
            lines.append("# TYPE arbitrage_bot_last_run_alerts gauge")
            lines.append(f"arbitrage_bot_last_run_alerts {int(alerts)}")

        telegram_ok = payload.get("last_telegram_send_ok")
        if telegram_ok is not None:
            lines.append("# TYPE arbitrage_bot_last_telegram_send_ok gauge")
            lines.append(f"arbitrage_bot_last_telegram_send_ok {1 if telegram_ok else 0}")

        return ("\n".join(lines) + "\n").encode("utf-8")

    def _send_json(self, status_code: int) -> None:
        payload = self._build_payload()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/health", "/live", "/ready", "/metrics"):
            self._send_json(200)
        elif self.path in ("/metrics/prom", "/metrics/prometheus"):
            body = self._build_prometheus_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path in ("/", "/health", "/live", "/ready", "/metrics"):
            self._send_json(200)
        elif self.path in ("/metrics/prom", "/metrics/prometheus"):
            body = self._build_prometheus_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
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
    preview = text if len(text) <= 200 else text[:197] + "..."
    now = int(time.time())
    METRICS["last_telegram_send_ts"] = now
    METRICS["last_telegram_send_text"] = preview
    METRICS["last_telegram_error"] = None

    if not enabled:
        print("[TELEGRAM DISABLED] Would send:\n" + text)
        METRICS["last_telegram_send_ok"] = False
        METRICS["last_telegram_error"] = "telegram_disabled"
        return

    token = get_bot_token()
    if not token:
        print("[TELEGRAM] Falta TG_BOT_TOKEN. No se envía.")
        print("Mensaje:\n" + text)
        METRICS["last_telegram_send_ok"] = False
        METRICS["last_telegram_error"] = "missing_token"
        return

    targets: List[str]
    if chat_id is not None:
        targets = [str(chat_id)]
    else:
        targets = get_registered_chat_ids()

    if not targets:
        print("[TELEGRAM] No hay chats registrados. No se envía.")
        print("Mensaje:\n" + text)
        METRICS["last_telegram_send_ok"] = False
        METRICS["last_telegram_error"] = "no_targets"
        return

    base = f"https://api.telegram.org/bot{token}/sendMessage"
    errors = []
    for cid in targets:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}
            r = requests.post(base, data=payload, timeout=8)
            if r.status_code != 200:
                print(f"[TELEGRAM] HTTP {r.status_code} chat_id={cid} -> {r.text}")
                errors.append(f"{cid}:{r.status_code}")
        except Exception as e:
            print(f"[TELEGRAM] Error enviando a {cid}: {e}")
            errors.append(f"{cid}:{e}")

    if errors:
        METRICS["last_telegram_send_ok"] = False
        METRICS["last_telegram_error"] = "; ".join(str(err) for err in errors[:3])
    else:
        METRICS["last_telegram_send_ok"] = True


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

    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    polling_active = TELEGRAM_POLLING_THREAD and TELEGRAM_POLLING_THREAD.is_alive()
    if tg_enabled and not polling_active:
        tg_process_updates(enabled=tg_enabled)

    pairs = list(CONFIG["pairs"])
    threshold = float(CONFIG["threshold_percent"])
    capital = float(CONFIG["simulation_capital_quote"])
    log_csv = CONFIG["log_csv_path"]

    pair_quotes: Dict[str, Dict[str, Quote]] = {p: {} for p in pairs}
    for pair in pairs:
        for vname, adapter in adapters.items():
            started_at = time.perf_counter()
            try:
                q = adapter.fetch_quote(pair)
            except Exception as e:
                q = None
                print(f"[{vname}] error fetch {pair}: {e}")
            finally:
                duration_ms = (time.perf_counter() - started_at) * 1000.0
                record_fetch_latency(duration_ms)
            if q:
                pair_quotes[pair][vname] = q
                METRICS.setdefault("quotes", {}).setdefault(pair, {})[vname] = {
                    "bid": q.bid,
                    "ask": q.ask,
                    "ts": int(q.ts),
                }
                METRICS["last_quotes_update_ts"] = int(time.time())

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

    METRICS["last_run_ts"] = int(time.time())
    METRICS["last_run_alerts"] = alerts
    METRICS["last_pairs_with_data"] = {
        pair: sorted(quotes.keys()) for pair, quotes in pair_quotes.items() if quotes
    }

    print(f"Run complete. Oportunidades enviadas: {alerts}")

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
