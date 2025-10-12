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
from dataclasses import dataclass, field
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
        "binance": {
            "enabled": True,
            "fees": {
                "default": {
                    "taker": 0.10,
                    "maker": 0.08,
                    "slippage_bps": 1.0,
                    "native_token_discount_percent": 0.025,
                },
                "per_pair": {
                    "BTC/USDT": {"taker": 0.08, "slippage_bps": 0.8},
                    "ETH/USDT": {"taker": 0.085},
                },
                "vip_level": "VIP0",
                "vip_multipliers": {
                    "default": 1.0,
                    "VIP0": 1.0,
                    "VIP1": 0.95,
                    "VIP2": 0.90,
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
            },
        },
        "bybit": {
            "enabled": True,
            "fees": {
                "default": {
                    "taker": 0.10,
                    "maker": 0.10,
                    "slippage_bps": 1.5,
                },
                "vip_level": "VIP0",
                "vip_multipliers": {
                    "default": 1.0,
                    "VIP1": 0.97,
                    "VIP2": 0.93,
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
            },
        },
        "kucoin": {
            "enabled": True,
            "fees": {
                "default": {
                    "taker": 0.10,
                    "maker": 0.08,
                    "slippage_bps": 1.2,
                },
                "vip_level": "VIP0",
                "vip_multipliers": {
                    "default": 1.0,
                    "VIP1": 0.92,
                },
                "native_token_discount_percent": 0.02,
            },
            "transfers": {
                "BTC": {
                    "withdraw_fee": 0.0006,
                    "withdraw_minutes": 40,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 20,
                },
                "ETH": {
                    "withdraw_fee": 0.003,
                    "withdraw_minutes": 15,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 8,
                },
                "USDT": {
                    "withdraw_fee": 1.0,
                    "withdraw_minutes": 25,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 10,
                },
            },
        },
        "okx": {
            "enabled": True,
            "fees": {
                "default": {
                    "taker": 0.10,
                    "maker": 0.09,
                    "slippage_bps": 1.1,
                },
                "vip_level": "VIP0",
                "vip_multipliers": {
                    "default": 1.0,
                    "VIP1": 0.96,
                },
            },
            "transfers": {
                "BTC": {
                    "withdraw_fee": 0.0004,
                    "withdraw_minutes": 28,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 12,
                },
                "ETH": {
                    "withdraw_fee": 0.002,
                    "withdraw_minutes": 9,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 4,
                },
                "USDT": {
                    "withdraw_fee": 0.8,
                    "withdraw_minutes": 18,
                    "deposit_fee": 0.0,
                    "deposit_minutes": 6,
                },
            },
        },
        # add more venues aquí
    },
    "inventory_management": {
        "enabled": True,
        "rebalance_frequency_trades": 3,
    },
    "telegram": {
        "enabled": True,                 # poner False para pruebas sin enviar
        "bot_token_env": "TG_BOT_TOKEN",
        "chat_ids_env": "TG_CHAT_IDS",   # coma-separado: "-100123...,123456..."
    },
    "log_csv_path": "logs/opportunities.csv",
}

TELEGRAM_CHAT_IDS: Set[str] = set()
TELEGRAM_LAST_UPDATE_ID = 0
TELEGRAM_POLLING_THREAD: Optional[threading.Thread] = None

FEE_REGISTRY: Dict[Tuple[str, str], float] = {}


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
    estimated_profit_quote: float
    estimated_base_qty: float
    buy_fee_percent: float
    sell_fee_percent: float
    buy_slippage_bps: float
    sell_slippage_bps: float
    transfer_cost_quote: float
    transfer_minutes: float
    rebalance_cost_quote: float
    rebalance_minutes: float

def compute_opportunities_for_pair(
    pair: str,
    quotes: Dict[str, Quote],
    fees: Dict[str, VenueFees],
    transfers: Dict[str, VenueTransfers],
    capital_quote: float,
) -> List[Opportunity]:
    venues = list(quotes.keys())
    out: List[Opportunity] = []
    for buy_v, sell_v in itertools.permutations(venues, 2):
        qb = quotes.get(buy_v)
        qs = quotes.get(sell_v)
        if qb is None or qs is None:
            continue

        raw_buy_price = qb.ask
        raw_sell_price = qs.bid
        if raw_buy_price <= 0 or raw_sell_price <= 0:
            continue

        buy_fee_cfg = fees.get(buy_v)
        sell_fee_cfg = fees.get(sell_v)
        if not buy_fee_cfg or not sell_fee_cfg:
            continue

        buy_schedule = buy_fee_cfg.schedule_for_pair(pair)
        sell_schedule = sell_fee_cfg.schedule_for_pair(pair)

        executed_buy_price = apply_slippage(raw_buy_price, buy_schedule.slippage_bps, "buy")
        executed_sell_price = apply_slippage(raw_sell_price, sell_schedule.slippage_bps, "sell")
        if executed_buy_price <= 0 or executed_sell_price <= 0:
            continue

        base_qty = compute_base_quantity(capital_quote, raw_buy_price, buy_schedule.slippage_bps)
        transfer_estimate = estimate_round_trip_transfer_cost(
            pair,
            buy_v,
            sell_v,
            base_qty,
            executed_sell_price,
            transfers,
        )
        rebalance_cost, rebalance_minutes = simulate_inventory_rebalance(
            pair,
            buy_v,
            sell_v,
            base_qty,
            executed_sell_price,
            transfers,
        )

        profit, net_pct, realized_base_qty = estimate_profit(
            capital_quote=capital_quote,
            buy_price=raw_buy_price,
            sell_price=raw_sell_price,
            buy_fee_percent=buy_schedule.taker_fee_percent,
            sell_fee_percent=sell_schedule.taker_fee_percent,
            buy_slippage_bps=buy_schedule.slippage_bps,
            sell_slippage_bps=sell_schedule.slippage_bps,
            transfer_cost_quote=transfer_estimate.total_cost_quote,
            rebalance_cost_quote=rebalance_cost,
        )

        gross = 0.0
        if executed_buy_price > 0:
            gross = (executed_sell_price - executed_buy_price) / executed_buy_price * 100.0

        out.append(
            Opportunity(
                pair=pair,
                buy_venue=buy_v,
                sell_venue=sell_v,
                buy_price=executed_buy_price,
                sell_price=executed_sell_price,
                gross_percent=gross,
                net_percent=net_pct,
                estimated_profit_quote=profit,
                estimated_base_qty=realized_base_qty,
                buy_fee_percent=buy_schedule.taker_fee_percent,
                sell_fee_percent=sell_schedule.taker_fee_percent,
                buy_slippage_bps=buy_schedule.slippage_bps,
                sell_slippage_bps=sell_schedule.slippage_bps,
                transfer_cost_quote=transfer_estimate.total_cost_quote,
                transfer_minutes=transfer_estimate.total_minutes,
                rebalance_cost_quote=rebalance_cost,
                rebalance_minutes=rebalance_minutes,
            )
        )

    return sorted(out, key=lambda o: o.net_percent, reverse=True)

# =========================
# Simulación PnL (avanzada)
# =========================
def estimate_profit(
    capital_quote: float,
    buy_price: float,
    sell_price: float,
    buy_fee_percent: float,
    sell_fee_percent: float,
    buy_slippage_bps: float = 0.0,
    sell_slippage_bps: float = 0.0,
    transfer_cost_quote: float = 0.0,
    rebalance_cost_quote: float = 0.0,
) -> Tuple[float, float, float]:
    if buy_price <= 0 or sell_price <= 0 or capital_quote <= 0:
        return 0.0, 0.0, 0.0

    executed_buy = apply_slippage(buy_price, buy_slippage_bps, "buy")
    executed_sell = apply_slippage(sell_price, sell_slippage_bps, "sell")
    if executed_buy <= 0 or executed_sell <= 0:
        return 0.0, 0.0, 0.0

    base_qty = capital_quote / executed_buy
    gross_proceeds = base_qty * executed_sell

    buy_fee_amount = capital_quote * (buy_fee_percent / 100.0)
    sell_fee_amount = gross_proceeds * (sell_fee_percent / 100.0)

    net_proceeds = gross_proceeds - sell_fee_amount
    total_costs = buy_fee_amount + transfer_cost_quote + rebalance_cost_quote
    profit = net_proceeds - capital_quote - total_costs
    net_pct = (profit / capital_quote) * 100.0 if capital_quote > 0 else 0.0

    return profit, net_pct, base_qty

# =========================
# Logging CSV
# =========================
def append_csv(path: str, opp: Opportunity) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow([
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
                "buy_fee_percent",
                "sell_fee_percent",
                "buy_slippage_bps",
                "sell_slippage_bps",
                "transfer_cost_quote",
                "rebalance_cost_quote",
                "transfer_minutes",
                "rebalance_minutes",
            ])
        w.writerow([
            int(time.time()), opp.pair, opp.buy_venue, opp.sell_venue,
            f"{opp.buy_price:.8f}", f"{opp.sell_price:.8f}",
            f"{opp.gross_percent:.4f}", f"{opp.net_percent:.4f}",
            f"{opp.estimated_profit_quote:.4f}", f"{opp.estimated_base_qty:.8f}",
            f"{opp.buy_fee_percent:.6f}", f"{opp.sell_fee_percent:.6f}",
            f"{opp.buy_slippage_bps:.4f}", f"{opp.sell_slippage_bps:.4f}",
            f"{opp.transfer_cost_quote:.6f}", f"{opp.rebalance_cost_quote:.6f}",
            f"{opp.transfer_minutes:.2f}", f"{opp.rebalance_minutes:.2f}",
        ])

# =========================
# Formato de alerta
# =========================
def fmt_alert(opp: Opportunity, capital_quote: float) -> str:
    base_asset, quote_asset = opp.pair.split("/")
    rebalance_cfg = CONFIG.get("inventory_management", {})
    rebalance_freq = int(rebalance_cfg.get("rebalance_frequency_trades", 1) or 1)
    return (
        "ARBITRAJE SPOT (inventario)\n"
        f"Par: {opp.pair}\n"
        f"Comprar en {opp.buy_venue}: {opp.buy_price:.6f}\n"
        f"Vender en {opp.sell_venue}: {opp.sell_price:.6f}\n"
        f"Spread bruto (con slippage): {opp.gross_percent:.3f}%  |  Neto: {opp.net_percent:.3f}%\n"
        f"Fees taker buy/sell: {opp.buy_fee_percent:.4f}% / {opp.sell_fee_percent:.4f}%\n"
        f"Slippage esperado (bps) buy/sell: {opp.buy_slippage_bps:.2f} / {opp.sell_slippage_bps:.2f}\n"
        f"Costos transferencia round-trip: {opp.transfer_cost_quote:.4f} {quote_asset} (~{opp.transfer_minutes:.1f} min)\n"
        f"Rebalance promedio ({rebalance_freq} trades): {opp.rebalance_cost_quote:.4f} {quote_asset} (~{opp.rebalance_minutes:.1f} min)\n"
        f"Simulación sobre {capital_quote:.0f} {quote_asset}: PnL ~{opp.estimated_profit_quote:.2f} {quote_asset}  (~{opp.net_percent:.3f}%)\n"
        f"Base movida: {opp.estimated_base_qty:.6f} {base_asset}\n"
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

# =========================
# Run (una vez)
# =========================
def run_once() -> None:
    adapters = build_adapters()
    if not adapters:
        print("No hay venues habilitados en CONFIG['venues'].")
        return

    tg_enabled = bool(CONFIG["telegram"].get("enabled", False))
    polling_active = TELEGRAM_POLLING_THREAD and TELEGRAM_POLLING_THREAD.is_alive()
    if tg_enabled and not polling_active:
        tg_process_updates(enabled=tg_enabled)

    pairs = list(CONFIG["pairs"])
    fee_map = build_fee_map(pairs)
    transfer_profiles = build_transfer_profiles()
    threshold = float(CONFIG["threshold_percent"])
    capital = float(CONFIG["simulation_capital_quote"])
    log_csv = CONFIG["log_csv_path"]

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
        opps = compute_opportunities_for_pair(pair, quotes, fee_map, transfer_profiles, capital)
        for opp in opps:
            if opp.net_percent >= threshold:
                append_csv(log_csv, opp)
                msg = fmt_alert(opp, capital)
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
