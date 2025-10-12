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
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple, Set, Any

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
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
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
                data = json.loads(raw.decode("utf-8") or "{}")
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
            f"Capital simulado: {CONFIG['simulation_capital_quote']:.2f} USDT\n"
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
        if not ensure_admin(chat_id, enabled):
            return
        try:
            value = float(argument.replace("%", "").strip())
        except ValueError:
            tg_send_message("Valor inválido. Ej: /threshold 0.8", enabled=enabled, chat_id=chat_id)
            return
        with CONFIG_LOCK:
            CONFIG["threshold_percent"] = value
        refresh_config_snapshot()
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
    base_asset, _ = split_pair(opp.pair)
    link_parts: List[str] = []
    for item in build_trade_link_items(opp.buy_venue, opp.sell_venue, opp.pair):
        link_parts.append(f"[{item['label']}]({item['url']})")
    links_line = " | ".join(link_parts)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    header = "🚨 *Oportunidad de arbitraje spot*"
    spread = f"📊 *Spread:* bruto {opp.gross_percent:.3f}% · neto {opp.net_percent:.3f}%"
    simulation = (
        "💰 *Simulación {capital_quote:.0f} USDT:* "
        f"PnL ≈ {est_profit:.2f} USDT ({est_percent:.3f}%)"
    )
    volume_line = f"📦 Volumen estimado: {base_qty:.6f} {base_asset}"
    venue_line = (
        f"🛒 Comprar en *{opp.buy_venue.title()}* a {opp.buy_price:.6f}\n"
        f"🏦 Vender en *{opp.sell_venue.title()}* a {opp.sell_price:.6f}"
    )
    parts = [
        header,
        f"*Par:* `{opp.pair}`",
        spread,
        venue_line,
        simulation,
        volume_line,
    ]
    if links_line:
        parts.append(f"🔗 {links_line}")
    parts.append(f"_Actualizado {timestamp} UTC_")
    return "\n".join(parts)

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

    with CONFIG_LOCK:
        pairs = list(CONFIG["pairs"])
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
    summary_opps: List[Dict[str, Any]] = []
    alert_records: List[Dict[str, Any]] = []
    run_ts = int(time.time())

    for pair, quotes in pair_quotes.items():
        if len(quotes) < 2:
            continue
        opps = compute_opportunities_for_pair(pair, quotes, fee_map)
        for opp in opps[:5]:
            f_buy = fee_map.get(opp.buy_venue)
            f_sell = fee_map.get(opp.sell_venue)
            if not f_buy or not f_sell:
                continue
            total_fee_pct = f_buy.taker_fee_percent + f_sell.taker_fee_percent
            est_profit, est_percent, base_qty = estimate_profit(capital, opp.buy_price, opp.sell_price, total_fee_pct)
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
                "links": link_items,
                "threshold_hit": opp.net_percent >= threshold,
            }
            summary_opps.append(entry)
            if opp.net_percent >= threshold:
                append_csv(log_csv, opp, est_profit, base_qty)
                msg = fmt_alert(opp, est_profit, est_percent, base_qty, capital)
                tg_send_message(msg, enabled=tg_enabled)
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
