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
from statistics import mean, pstdev, StatisticsError
from typing import Dict, Optional, List, Tuple, Set, Iterable

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
    "analysis": {
        "lookback_hours": 72,
        "target_success_rate": 0.65,
        "min_threshold_percent": 0.3,
        "max_threshold_percent": 2.5,
        "adjust_multiplier": 0.6,
    },
    "execution_costs": {
        "slippage_bps": 8.0,        # coste extra (ida+vuelta)
        "rebalance_bps": 5.0,       # coste de rebalanceo eventual
        "latency_seconds": 6.0,
        "latency_penalty_multiplier": 0.45,
    },
}

TELEGRAM_CHAT_IDS: Set[str] = set()
TELEGRAM_LAST_UPDATE_ID = 0
TELEGRAM_POLLING_THREAD: Optional[threading.Thread] = None

DYNAMIC_THRESHOLD_PERCENT = float(CONFIG["threshold_percent"])
LATEST_ANALYSIS: Optional["HistoricalAnalysis"] = None
LOG_HEADER_INITIALIZED = False



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
    global DYNAMIC_THRESHOLD_PERCENT
    command = command.lower()
    register_telegram_chat(chat_id)

    if command == "/start":
        response = (
            "Hola! Ya estás registrado para recibir señales.\n"
            f"Threshold base: {CONFIG['threshold_percent']:.3f}% | dinámico: {DYNAMIC_THRESHOLD_PERCENT:.3f}%\n"
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

    if command == "/threshold":
        if not argument:
            tg_send_message(
                (
                    f"Threshold base: {CONFIG['threshold_percent']:.3f}% | "
                    f"dinámico: {DYNAMIC_THRESHOLD_PERCENT:.3f}%"
                ),
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
        DYNAMIC_THRESHOLD_PERCENT = value
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
    volume_quote_24h: float = 0.0

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
        volume_quote = 0.0
        try:
            stats = http_get_json("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym})
            volume_quote = float(stats.get("quoteVolume", 0.0))
        except Exception:
            volume_quote = 0.0
        return Quote(sym, bid, ask, int(time.time()*1000), volume_quote)

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
            volume_quote = float(item.get("turnover24h", 0.0))
            return Quote(sym, bid, ask, int(time.time()*1000), volume_quote)
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
            volume_quote = 0.0
            try:
                stats = http_get_json("https://api.kucoin.com/api/v1/market/stats", params={"symbol": sym})
                volume_quote = float(stats.get("data", {}).get("volValue", 0.0))
            except Exception:
                volume_quote = 0.0
            return Quote(sym, bid, ask, int(time.time()*1000), volume_quote)
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
            volume_quote = float(item.get("volCcy24h", 0.0))
            return Quote(sym, bid, ask, int(time.time()*1000), volume_quote)
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
    liquidity_score: float = 0.0
    volatility_score: float = 0.0
    priority_score: float = 0.0
    confidence_label: str = "media"


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


def safe_float(value: Optional[str], default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


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


def compute_liquidity_score(pair_quotes: Dict[str, Quote], buy_venue: str, sell_venue: str) -> float:
    buy_quote = pair_quotes.get(buy_venue)
    sell_quote = pair_quotes.get(sell_venue)
    if not buy_quote or not sell_quote:
        return 0.0

    volumes = [q.volume_quote_24h for q in pair_quotes.values() if q and q.volume_quote_24h > 0]
    if not volumes:
        return 0.0
    max_volume = max(volumes)
    if max_volume <= 0:
        return 0.0

    base_volume = min(buy_quote.volume_quote_24h, sell_quote.volume_quote_24h)
    return max(0.0, min(1.0, base_volume / max_volume))


def compute_volatility_score(pair: str, analysis: Optional[HistoricalAnalysis]) -> float:
    if not analysis or analysis.max_volatility <= 0:
        return 0.0
    vol = analysis.pair_volatility.get(pair, 0.0)
    if analysis.max_volatility <= 0:
        return 0.0
    return max(0.0, min(1.0, vol / analysis.max_volatility))


def compute_priority_score(net_percent: float, liquidity: float, volatility_score: float, threshold: float) -> Tuple[float, float]:
    normalized_net = 0.0
    if threshold > 0:
        normalized_net = max(0.0, min(1.5, net_percent / threshold))
    stability = 1.0 - volatility_score
    priority = (0.45 * min(1.0, normalized_net) + 0.4 * liquidity + 0.15 * max(0.0, stability))
    return max(0.0, min(1.0, priority)), normalized_net


def classify_confidence(priority: float, normalized_net: float, liquidity: float, volatility_score: float) -> str:
    if priority >= 0.75 and normalized_net >= 1.1 and liquidity >= 0.6 and volatility_score <= 0.35:
        return "alta"
    if priority >= 0.45 and liquidity >= 0.3:
        return "media"
    return "baja"


def enrich_opportunity(
    opp: Opportunity,
    pair_quotes: Dict[str, Quote],
    analysis: Optional[HistoricalAnalysis],
    threshold: float,
) -> Opportunity:
    liquidity = compute_liquidity_score(pair_quotes, opp.buy_venue, opp.sell_venue)
    volatility_score = compute_volatility_score(opp.pair, analysis)
    priority, normalized_net = compute_priority_score(opp.net_percent, liquidity, volatility_score, threshold)
    confidence = classify_confidence(priority, normalized_net, liquidity, volatility_score)
    opp.liquidity_score = liquidity
    opp.volatility_score = volatility_score
    opp.priority_score = priority
    opp.confidence_label = confidence
    return opp

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
    "liquidity_score",
    "volatility_score",
    "priority_score",
    "confidence",
]


def ensure_log_header(path: str) -> None:
    global LOG_HEADER_INITIALIZED
    if LOG_HEADER_INITIALIZED:
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(LOG_HEADER)
        LOG_HEADER_INITIALIZED = True
        return

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        rows = list(reader)

    if header == LOG_HEADER:
        LOG_HEADER_INITIALIZED = True
        return

    # Upgrade antiguo header -> nuevo formato con columnas extra
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(LOG_HEADER)
        for row in rows:
            record = {header[i]: row[i] for i in range(len(header))}
            writer.writerow([
                record.get("ts", ""),
                record.get("pair", ""),
                record.get("buy_venue", ""),
                record.get("sell_venue", ""),
                record.get("buy_price", ""),
                record.get("sell_price", ""),
                record.get("gross_%", ""),
                record.get("net_%", ""),
                record.get("est_profit_quote", ""),
                record.get("base_qty", ""),
                record.get("liquidity_score", ""),
                record.get("volatility_score", ""),
                record.get("priority_score", ""),
                record.get("confidence", ""),
            ])

    LOG_HEADER_INITIALIZED = True


def append_csv(path: str, opp: Opportunity, est_profit: float, base_qty: float) -> None:
    ensure_log_header(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
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
            f"{opp.liquidity_score:.4f}",
            f"{opp.volatility_score:.4f}",
            f"{opp.priority_score:.4f}",
            opp.confidence_label,
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
        f"Confianza: {opp.confidence_label.upper()}  |  Prioridad: {opp.priority_score:.2f}  |  Liquidez ~{opp.liquidity_score*100:.0f}%  |  Volatilidad rel. ~{opp.volatility_score*100:.0f}%\n"
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
    capital = float(CONFIG["simulation_capital_quote"])
    log_csv = CONFIG["log_csv_path"]

    global DYNAMIC_THRESHOLD_PERCENT, LATEST_ANALYSIS
    analysis = analyze_historical_performance(log_csv, capital)
    LATEST_ANALYSIS = analysis
    if analysis.rows_considered > 0:
        DYNAMIC_THRESHOLD_PERCENT = analysis.recommended_threshold
        print(
            "[ANALYSIS] Señales consideradas=%d | SR=%.1f%% | Neto medio=%.3f%% | Neto efectivo=%.3f%% -> Threshold dinámico=%.3f%%"
            % (
                analysis.rows_considered,
                analysis.success_rate * 100.0,
                analysis.average_net_percent,
                analysis.average_effective_percent,
                DYNAMIC_THRESHOLD_PERCENT,
            )
        )
    else:
        DYNAMIC_THRESHOLD_PERCENT = float(CONFIG["threshold_percent"])
        print("[ANALYSIS] Sin historial suficiente. Threshold base=%.3f%%" % DYNAMIC_THRESHOLD_PERCENT)

    threshold = DYNAMIC_THRESHOLD_PERCENT

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

    alerts = 0
    for pair, quotes in pair_quotes.items():
        if len(quotes) < 2:
            continue
        opps = [
            enrich_opportunity(opp, quotes, analysis, threshold)
            for opp in compute_opportunities_for_pair(pair, quotes, fee_map)
        ]
        opps.sort(key=lambda o: o.priority_score, reverse=True)
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
