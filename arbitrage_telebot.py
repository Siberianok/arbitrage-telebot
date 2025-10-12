#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import binascii
import csv
import hashlib
import itertools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple, Set, Any, Iterable

import requests

from observability import (
    ERROR_RATE_ALERT_THRESHOLD,
    is_circuit_open,
    log_event,
    record_exchange_attempt,
    record_exchange_error,
    record_exchange_no_data,
    record_exchange_skip,
    record_exchange_success,
    register_degradation_alert,
    reset_metrics,
)

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
    "offline_quotes": {
        "BTC/USDT": {"bid": 30050.0, "ask": 30060.0},
        "ETH/USDT": {"bid": 2050.0, "ask": 2052.5},
        "SOL/USDT": {"bid": 32.5, "ask": 32.6},
        "ADA/USDT": {"bid": 0.58, "ask": 0.581},
    },
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
TELEGRAM_ADMIN_IDS: Set[str] = set()

STATE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
DASHBOARD_STATE: Dict[str, Any] = {
    "last_run_summary": None,
    "latest_alerts": [],
    "config_snapshot": {},
}

MAX_ALERT_HISTORY = 20

WEB_AUTH_USER = os.getenv("WEB_AUTH_USER", "").strip()
WEB_AUTH_PASS = os.getenv("WEB_AUTH_PASS", "").strip()

LATEST_ANALYSIS: Optional[Any] = None


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
        "pairs": list(CONFIG.get("pairs", [])),
        "simulation_capital_quote": float(CONFIG.get("simulation_capital_quote", 0.0)),
        "venues": venues,
        "telegram_enabled": bool(CONFIG.get("telegram", {}).get("enabled", False)),
    }


def refresh_config_snapshot() -> None:
    with STATE_LOCK:
        DASHBOARD_STATE["config_snapshot"] = snapshot_public_config()

FEE_REGISTRY: Dict[Tuple[str, str], float] = {}


COMMANDS_HELP: List[Tuple[str, str]] = [
    ("/help", "Muestra este listado de comandos"),
    ("/ping", "Responde con 'pong' para verificar conectividad"),
    ("/status", "Resume configuración actual y chats registrados"),
    ("/threshold <valor>", "Consulta o actualiza el umbral de alerta (%)"),
    ("/capital <USDT>", "Consulta o ajusta el capital simulado en USDT"),
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

    def _require_authentication(self) -> bool:
        if not WEB_AUTH_USER and not WEB_AUTH_PASS:
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            self._send_unauthorized()
            return False
        parts = auth_header.split(" ", 1)
        if len(parts) < 2:
            self._send_unauthorized()
            return False
        try:
            decoded = base64.b64decode(parts[1]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            self._send_unauthorized()
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
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path in ("/", "/dashboard"):
            if not self._require_authentication():
                return
            self._send_html(DASHBOARD_HTML)
            return
        if self.path == "/api/state":
            if not self._require_authentication():
                return
            with STATE_LOCK:
                payload = {
                    "last_run_summary": DASHBOARD_STATE.get("last_run_summary"),
                    "latest_alerts": DASHBOARD_STATE.get("latest_alerts", []),
                    "config_snapshot": DASHBOARD_STATE.get("config_snapshot", {}),
                }
            self._send_json(payload)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/config":
            if not self._require_authentication():
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                decoded_body = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._send_json({"error": "JSON inválido"}, status=400)
                return
            if not decoded_body:
                decoded_body = "{}"
            try:
                data = json.loads(decoded_body)
            except json.JSONDecodeError:
                self._send_json({"error": "JSON inválido"}, status=400)
                return
            updated = {}
            errors: List[str] = []
            with CONFIG_LOCK:
                if "threshold_percent" in data:
                    try:
                        value = float(data["threshold_percent"])
                        CONFIG["threshold_percent"] = value
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
    pass

def current_millis() -> int:
    return int(time.time() * 1000)


@dataclass
class HttpJsonResponse:
    data: Dict[str, Any]
    checksum: str
    received_ts: int


LAST_CHECKSUMS: Dict[str, Tuple[str, int]] = {}
MAX_CHECKSUM_STALENESS_MS = 60_000


def http_get_json(
    url: str,
    params: Optional[dict] = None,
    timeout: int = 8,
    retries: int = 3,
    integrity_key: Optional[str] = None,
) -> HttpJsonResponse:
    last_exc: Optional[Exception] = None
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code != 200:
                raise HttpError(f"HTTP {r.status_code} {url} params={params}")

            received_ts = current_millis()
            checksum = hashlib.sha256(r.content).hexdigest()
            payload = r.json()
            if not isinstance(payload, dict):
                raise HttpError(f"Respuesta no es JSON objeto en {url}")

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
            time.sleep(0.5)
    raise last_exc or HttpError("GET failed")


MAX_ALLOWED_CLOCK_SKEW_MS = 5_000


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


def tg_send_message(text: str, enabled: bool = True, chat_id: Optional[str] = None) -> None:
    preview = text if len(text) <= 400 else text[:400] + "…"
    if not enabled:
        log_event("telegram.send.skip", reason="disabled", preview=preview)
        return

    token = get_bot_token()
    if not token:
        log_event("telegram.send.skip", reason="missing_token", preview=preview)
        return

    targets: List[str]
    if chat_id is not None:
        targets = [str(chat_id)]
    else:
        targets = get_registered_chat_ids()

    if not targets:
        log_event("telegram.send.skip", reason="no_targets", preview=preview)
        return

    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in targets:
        try:
            payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}
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
        if not ensure_admin(chat_id, enabled):
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

    if command == "/capital":
        if not argument:
            tg_send_message(
                f"Capital simulado: {CONFIG['simulation_capital_quote']:.2f} USDT",
                enabled=enabled,
                chat_id=chat_id,
            )
            return
        if not ensure_admin(chat_id, enabled):
            return
        try:
            value = float(argument.replace(",", "").strip())
        except ValueError:
            tg_send_message(
                "Valor inválido. Ej: /capital 15000",
                enabled=enabled,
                chat_id=chat_id,
            )
            return
        if value <= 0:
            tg_send_message("El capital debe ser mayor que cero.", enabled=enabled, chat_id=chat_id)
            return
        with CONFIG_LOCK:
            CONFIG["simulation_capital_quote"] = value
        refresh_config_snapshot()
        tg_send_message(
            f"Nuevo capital simulado guardado: {CONFIG['simulation_capital_quote']:.2f} USDT",
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
        if not ensure_admin(chat_id, enabled):
            return
        pair = argument.upper().strip()
        if pair in CONFIG["pairs"]:
            tg_send_message(f"{pair} ya estaba en la lista.", enabled=enabled, chat_id=chat_id)
        else:
            with CONFIG_LOCK:
                CONFIG["pairs"].append(pair)
            refresh_config_snapshot()
            tg_send_message(f"Par agregado: {pair}", enabled=enabled, chat_id=chat_id)
        return

    if command == "/delpair":
        if not argument:
            tg_send_message("Uso: /delpair BTC/USDT", enabled=enabled, chat_id=chat_id)
            return
        if not ensure_admin(chat_id, enabled):
            return
        pair = argument.upper().strip()
        if pair not in CONFIG["pairs"]:
            tg_send_message(f"{pair} no está en la lista.", enabled=enabled, chat_id=chat_id)
        else:
            with CONFIG_LOCK:
                CONFIG["pairs"] = [p for p in CONFIG["pairs"] if p != pair]
            refresh_config_snapshot()
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
        sym = self.normalize_symbol(pair)
        url = "https://api.binance.com/api/v3/ticker/bookTicker"
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"symbol": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
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
        url = "https://api.binance.com/api/v3/depth"
        try:
            response = http_get_json(
                url,
                params={"symbol": sym, "limit": 20},
                integrity_key=self._integrity_key(sym, "depth"),
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

class Bybit(ExchangeAdapter):
    name = "bybit"
    depth_supported = True

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        sym = self.normalize_symbol(pair)
        url = "https://api.bybit.com/v5/market/tickers"
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"category": "spot", "symbol": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
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
        url = "https://api.bybit.com/v5/market/orderbook"
        try:
            response = http_get_json(
                url,
                params={"category": "spot", "symbol": sym, "limit": 25},
                integrity_key=self._integrity_key(sym, "depth"),
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

class KuCoin(ExchangeAdapter):
    name = "kucoin"
    depth_supported = True

    def normalize_symbol(self, pair: str) -> str:
        return pair.replace("/", "-")

    def fetch_quote(self, pair: str) -> Optional[Quote]:
        sym = self.normalize_symbol(pair)
        url = "https://api.kucoin.com/api/v1/market/orderbook/level1"
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"symbol": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
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
        url = "https://api.kucoin.com/api/v1/market/orderbook/level2_20"
        try:
            response = http_get_json(
                url,
                params={"symbol": sym},
                integrity_key=self._integrity_key(sym, "depth"),
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
        sym = self.normalize_symbol(pair)
        url = "https://www.okx.com/api/v5/market/ticker"
        quote: Optional[Quote] = None
        try:
            response = http_get_json(
                url,
                params={"instId": sym},
                integrity_key=self._integrity_key(sym, "ticker"),
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
        url = "https://www.okx.com/api/v5/market/books"
        try:
            response = http_get_json(
                url,
                params={"instId": sym, "sz": "20"},
                integrity_key=self._integrity_key(sym, "depth"),
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

def build_adapters() -> Dict[str, ExchangeAdapter]:
    adapters: Dict[str, ExchangeAdapter] = {}
    vcfg = CONFIG["venues"]
    if vcfg.get("binance", {}).get("enabled", False):
        adapters["binance"] = Binance()
    if vcfg.get("bybit", {}).get("enabled", False):
        adapters["bybit"] = Bybit()
    if vcfg.get("kucoin", {}).get("enabled", False):
        adapters["kucoin"] = KuCoin()
    if vcfg.get("okx", {}).get("enabled", False):
        adapters["okx"] = OKX()
    return adapters


def collect_pair_quotes(pairs: List[str], adapters: Dict[str, ExchangeAdapter]) -> Dict[str, Dict[str, Quote]]:
    pair_quotes: Dict[str, Dict[str, Quote]] = {pair: {} for pair in pairs}
    if not pairs or not adapters:
        return pair_quotes

    futures_map: Dict[Any, Tuple[str, str]] = {}
    max_workers = min(32, max(1, len(pairs) * len(adapters)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for pair in pairs:
            for vname, adapter in adapters.items():
                futures_map[executor.submit(adapter.fetch_quote, pair)] = (pair, vname)

        for future in as_completed(futures_map):
            pair, vname = futures_map[future]
            try:
                quote = future.result()
            except Exception as exc:
                print(f"[{vname}] error fetch {pair}: {exc}")
                continue
            if quote:
                pair_quotes[pair][vname] = quote

    return pair_quotes

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
def estimate_profit(
    capital_quote: float,
    buy_price: float,
    sell_price: float,
    total_percent_fee: float,
    max_base_qty: Optional[float] = None,
) -> Tuple[float, float, float]:
    if buy_price <= 0 or sell_price <= 0 or capital_quote <= 0:
        return 0.0, 0.0, 0.0

    base_qty = capital_quote / buy_price
    if max_base_qty is not None:
        base_qty = min(base_qty, max_base_qty)

    if base_qty <= 0:
        return 0.0, 0.0, 0.0

    effective_capital = base_qty * buy_price
    gross_proceeds = base_qty * sell_price
    fee_loss = (total_percent_fee / 100.0) * effective_capital
    profit = gross_proceeds - effective_capital - fee_loss
    net_pct = (profit / effective_capital) * 100.0 if effective_capital > 0 else 0.0
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
]


def ensure_log_header(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(LOG_HEADER)


def append_csv(path: str, opp: Opportunity, est_profit: float, base_qty: float) -> None:
    ensure_log_header(path)
    buy_volume = buy_depth.ask_volume if buy_depth else 0.0
    sell_volume = sell_depth.bid_volume if sell_depth else 0.0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
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
def fmt_alert(
    opp: Opportunity,
    est_profit: float,
    est_percent: float,
    base_qty: float,
    capital_quote: float,
) -> str:
    lines = [
        "ARBITRAJE SPOT",
        f"Par: {opp.pair}",
        f"Comprar en {opp.buy_venue}: {opp.buy_price:.6f}",
        f"Vender en {opp.sell_venue}: {opp.sell_price:.6f}",
        f"Spread bruto: {opp.gross_percent:.3f}%  |  Neto: {opp.net_percent:.3f}%",
        f"PnL estimado: ~{est_profit:.2f} USDT  (~{est_percent:.3f}%)",
        f"Capital simulado: {capital_quote:.2f} USDT",
        f"Cantidad base estimada: {base_qty:.6f}",
        time.strftime("%Y-%m-%d %H:%M:%S"),
    ]
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
    adapters = build_adapters()
    if not adapters:
        log_event("run.skip", reason="no_venues")
        return

    reset_metrics(adapters.keys())
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
    fee_map = build_fee_map(all_pairs)

    fee_map = build_fee_map(all_pairs)
    summary_opps: List[Dict[str, Any]] = []
    alert_records: List[Dict[str, Any]] = []
    run_ts = int(time.time())

    pair_quotes: Dict[str, Dict[str, Quote]] = {p: {} for p in all_pairs}
    for pair in all_pairs:
        for vname, adapter in adapters.items():
            if is_circuit_open(vname):
                record_exchange_skip(vname, "circuit_open", pair)
                continue

            record_exchange_attempt(vname, pair)
            quote: Optional[Quote] = None
            try:
                quote = adapter.fetch_quote(pair)
            except Exception as exc:
                record_exchange_error(vname, str(exc), pair)
            else:
                if quote:
                    record_exchange_success(vname, pair)
                    pair_quotes[pair][vname] = quote
                    log_event(
                        "exchange.quote",
                        exchange=vname,
                        pair=pair,
                        bid=quote.bid,
                        ask=quote.ask,
                    )
                else:
                    record_exchange_no_data(vname, pair)

    alerts = 0
    for pair in pairs:
        quotes = pair_quotes.get(pair, {})
        if len(quotes) < 2:
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
            est_profit, est_percent, base_qty, capital_used = estimate_profit(
                capital_for_pair,
                opp.buy_price,
                opp.sell_price,
                total_fee_pct,
            )
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
                "threshold_hit": opp.net_percent >= threshold,
            }
            summary_opps.append(entry)
            if opp.net_percent >= threshold:
                est_profit, est_percent, base_qty, capital_used = estimate_profit(
                    capital_for_pair,
                    opp.buy_price,
                    opp.sell_price,
                    total_fee_pct,
                )

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
                    opp.buy_depth,
                    opp.sell_depth,
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
                alerts += 1
                alert_entry = dict(entry)
                alert_entry["ts"] = int(time.time())
                alert_entry["ts_str"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert_entry["ts"]))
                alert_records.append(alert_entry)

    summary_opps.sort(key=lambda item: item["net_percent"], reverse=True)
    if len(summary_opps) > 20:
        summary_opps = summary_opps[:20]

    summary = {
        "ts": run_ts,
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(run_ts)),
        "threshold": threshold,
        "capital": capital,
        "pairs": pairs,
        "opportunities": summary_opps,
        "alerts_sent": alerts,
    }

    if alert_records:
        alert_records.sort(key=lambda item: item["ts"], reverse=True)

    with STATE_LOCK:
        DASHBOARD_STATE["last_run_summary"] = summary
        if alert_records:
            history = DASHBOARD_STATE.get("latest_alerts", [])
            history.extend(alert_records)
            history.sort(key=lambda item: item.get("ts", 0), reverse=True)
            DASHBOARD_STATE["latest_alerts"] = history[:MAX_ALERT_HISTORY]

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
