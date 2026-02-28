# Arbitrage Telebot

Bot de arbitraje cripto **multi-estrategia** con alertas en Telegram, observabilidad, panel web y despliegue por roles.
Está orientado a operación realista (inventario prefondeado), resiliencia en producción y trazabilidad completa de señales.

## 🚀 Qué hace hoy

- Escanea oportunidades **spot↔spot** entre Binance, Bybit, KuCoin y OKX (best bid/ask + validaciones de calidad de cotización).
- Soporta estrategias habilitables por configuración:
  - `spot_spot`
  - `spot_p2p`
  - `p2p_p2p`
  - `triangular_intra_venue`
  - `ars_usdt_roundtrip` (disponible, apagada por defecto)
- Calcula spread neto post-fees y simula PnL sobre capital configurable.
- Aplica **threshold dinámico** basado en análisis histórico y backtesting.
- Clasifica señales por confianza/prioridad usando liquidez, volatilidad y score compuesto.
- Expone métricas Prometheus y endpoints de salud para operación continua.
- Incluye comandos de Telegram para control operativo (pares, threshold, capital, status, test, etc.).
- Persiste configuración runtime y mantiene backups de seguridad.

## 🧩 Arquitectura por roles (recomendado en producción)

El proceso se puede ejecutar en modo monolítico o separado por responsabilidades:

- `scanner`: motor de escaneo y generación de oportunidades.
- `api`: dashboard web + API de estado/configuración.
- `telegram-worker`: polling de Telegram y procesamiento de comandos.
- `all`: modo único (todo junto, útil para desarrollo o setups simples).

En `render.yaml` ya está definido el split recomendado en 3 servicios:

1. `arbitrage-telebot-scanner` (worker)
2. `arbitrage-telebot-api` (web)
3. `arbitrage-telebot-telegram` (worker)

> Importante: el servicio crítico para comandos de Telegram es `arbitrage-telebot-telegram`. Si cae, el bot no responde comandos aunque scanner y API estén arriba.

## ⚙️ Ejecución local

### 1) Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2) Variables mínimas

```bash
export TG_BOT_TOKEN="..."
export TG_CHAT_IDS="123456789,-10011223344"
```

Opcionales recomendadas:

```bash
export TG_ADMIN_IDS="123456789"
export WEB_AUTH_USER="operador"
export WEB_AUTH_PASS="clave-segura"
export STATE_DB_URL="sqlite:///state.db"
```

### 3) Ejecutar

**Monolítico (rápido):**

```bash
python arbitrage_telebot.py --web --loop --interval 30 --port 10000
```

**Por roles (producción):**

```bash
python arbitrage_telebot.py --role scanner --loop --interval 30
python arbitrage_telebot.py --role api --web --port 10000
python arbitrage_telebot.py --role telegram-worker --web --port 10001
```

## 🤖 Comandos de Telegram

| Comando | Descripción |
| --- | --- |
| `/start` | Registra el chat y muestra ayuda operativa. |
| `/ping` | Responde `pong` para verificar conectividad. |
| `/status` | Estado general + threshold base/dinámico + pares + chats. |
| `/threshold [valor]` | Consulta/actualiza threshold base (admins para modificar). |
| `/capital [monto]` | Consulta/actualiza capital de simulación (admins para modificar). |
| `/pairs` | Lista pares monitoreados. |
| `/addpair` | Agrega activo como `BASE/USDT` (admins). |
| `/delpair` | Elimina par existente con teclado interactivo (admins). |
| `/test` | Envía señal de prueba con formato de alerta realista. |

Aliases soportados:

- `/listapares` → `/pairs`
- `/adherirpar` → `/addpair`
- `/eliminarpar` → `/delpair`
- `/senalprueba` → `/test`

## 📊 Panel web, APIs y salud

Al correr con `--web` se habilitan:

- `/` dashboard autenticado
- `/api/state` snapshot de estado
- `/api/config` actualización de configuración (POST autenticado)
- `/metrics` métricas Prometheus
- `/health` salud integral
- `/live` liveness
- `/ready` readiness

## 🧠 Threshold dinámico e inteligencia histórica

El bot recalcula y cachea análisis histórico para ajustar el umbral de alertas de forma controlada:

- parte de `threshold_percent` base,
- aplica reglas configurables de tuning,
- limita cambios con mínimos/máximos,
- usa la recomendación resultante durante el escaneo.

Esto mejora precisión de alertas y reduce ruido cuando cambian las condiciones del mercado.

## 🧾 Persistencia y logs

Rutas de salida (por defecto):

- `logs/opportunities.csv`
- `logs/triangular_opportunities.csv`
- `logs/signal_lifecycle.csv`
- `logs/execution_results.csv`
- backups en `log_backups/`

Variables útiles:

- `LOG_BASE_DIR`
- `LOG_BACKUP_DIR`
- `STATE_DB_URL`
- `QUOTE_WORKERS`
- `DEFAULT_QUOTE_ASSET`

## 🌐 Keepalive y estabilidad en Render

Para evitar suspensión en entornos con idling:

- `KEEPALIVE_URL` (normaliza automáticamente a `/health`)
- `KEEPALIVE_ENABLED`
- `KEEPALIVE_INTERVAL_SECONDS` (mínimo 60)
- `KEEPALIVE_TIMEOUT_SECONDS`

## 🧪 Testing

```bash
pytest
```

La suite cubre conectividad de adapters, observabilidad, ciclo de señales, estado runtime, polling de Telegram, keepalive y persistencia/configuración.

## 📚 Documentación adicional

- `docs/continuity_runbook.md` (continuidad operativa)
- `docs/playbooks.md` (playbooks manual/semi/automático)
- `docs/checklist_operativo.md`
- `docs/explicacion_cambios.md`

---

Si querés, en un próximo paso puedo agregar una sección de **“quickstart para Render en 5 minutos”** con checklist copy/paste de variables por servicio.
