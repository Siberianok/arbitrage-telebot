ARBITRAJE-TELEBOT

Bot de arbitraje multi-exchange (spot) con alertas en Telegram, cálculo de spread neto (post-fees) y simulación de PnL sobre un capital configurable.
Arquitectura lista para extender a P2P/fiat y simulación de “vuelta completa”. Sin humo. 🚀

✨ Características

CEX soportados (v1): Binance, Bybit, KuCoin, OKX (spot, best bid/ask).

Pares configurables: BTC/USDT, ETH/USDT, XRP/USDT, ADA/USDT, ALGO/USDT, SHIB/USDT (podés sumar más).

Spread neto: spread_neto = spread_bruto − (fee_taker_buy + fee_taker_sell).

Umbral de alerta: configurable (recomendado 0.8–1.0%).

Simulación de PnL: incluida en cada alerta (ej.: capital = 10,000 USDT). 🧮

Priorización avanzada: cada señal pondera liquidez (depth L2), volatilidad histórica y clasifica confianza (alta/media/baja).

Recolección resiliente: consultas paralelas con fallback multi-endpoint, verificación de checksum y métricas Prometheus listas para consumir.

Inteligencia histórica: ajuste dinámico del threshold, backtesting con costes realistas y clasificación de confianza por señal. 📈

Alertas Telegram: múltiples destinos (DM y/o canal) con formato enriquecido y enlaces directos a los libros de órdenes. 🔔

Logs CSV: logs/opportunities.csv (timestamp, par, venues, spreads, PnL simulado) + respaldos automáticos en `log_backups/`. 🧾

Dashboard web: panel autenticado con estado en tiempo real, historial de alertas y edición segura de configuración. 📊

Diseño extensible: fácil de sumar exchanges, P2P, puentes fiat, límites y manejo de latencias. 🧰

Ejecución v1 (inventario): ejecutás el spread con saldo en ambos exchanges (sin transferencias) — realista para oportunidades rápidas. ⚡

📟 Comandos de Telegram disponibles

| Comando | Descripción |
| --- | --- |
| `/start` | Registra el chat y muestra la ayuda operativa con accesos rápidos. |
| `/ping` | Devuelve `pong` para verificar la conectividad del bot. |
| `/status` | Resume threshold base/dinámico, historial, pares monitoreados y chats registrados. |
| `/threshold [valor]` | Consulta o actualiza el threshold base dentro del rango configurado _(solo admins)_. |
| `/capital [monto]` | Consulta o actualiza el capital simulado (USDT) _(solo admins pueden modificar)_. |
| `/pairs` | Muestra la lista de pares configurados actualmente. |
| `/addpair` | Solicita la cripto a adherir y la agrega como `BASE/USDT` _(solo admins)_. |
| `/delpair` | Despliega botones con los pares actuales para elegir cuál eliminar _(solo admins)_. |
| `/test` | Envía una señal de prueba para confirmar entregas. |

Aliases soportados: `/listapares` → `/pairs`, `/adherirpar` → `/addpair`, `/eliminarpar` → `/delpair`, `/senalprueba` → `/test`.

Para restringir quién puede modificar parámetros, definir `TG_ADMIN_IDS` (lista de chat IDs separados por coma). Si no se configura, cualquier chat registrado podrá editar la configuración.

📈 Panel web

Ejecutar el bot con `--web` expone:

- `/health` — JSON con latencia de la última corrida, métricas por exchange y timestamp del último envío a Telegram.
- `/` — dashboard autenticado con estado y controles.
- `/api/state` — snapshot JSON para integraciones.
- `/api/config` — actualización de configuración vía POST autenticado.
- `/metrics` — métricas Prometheus listas para scrapear (latencias, alertas, intentos por exchange, etc.).

Configurar credenciales básicas vía variables de entorno:

```
export WEB_AUTH_USER="operador"
export WEB_AUTH_PASS="clave-super-segura"
```

Luego iniciar con `python arbitrage_telebot.py --web --interval 30 --port 10000`.



🏗️ Ejecución por procesos (producción)

Para separar responsabilidades y mejorar disponibilidad, usar roles dedicados:

- Scanner/engine: `python arbitrage_telebot.py --role scanner --loop --interval 30`
- API/dashboard: `python arbitrage_telebot.py --role api --web --port 10000`
- Telegram polling worker: `python arbitrage_telebot.py --role telegram-worker --web --port 10001`

Todos los roles exponen `/health`, `/live` y `/ready` cuando arrancan con `--web`, incluyendo checks específicos por proceso en el payload (`process.checks`).

🛟 Continuidad operativa y contingencia

- Infra recomendada sin sleep: servicios separados en plan always-on (`render.yaml`).
- Logs y backups: usar almacenamiento persistente vía `LOG_BASE_DIR` / `LOG_BACKUP_DIR`.
- Estado externo: configurar `STATE_DB_URL` para persistencia fuera del filesystem local.
- Runbook completo: `docs/continuity_runbook.md`.

📦 Variables clave para despliegues resilientes

- `LOG_BASE_DIR` / `LOG_BACKUP_DIR`: directorios para logs y respaldos persistentes (por defecto `logs/` y `log_backups/`).
- `QUOTE_WORKERS`: máximo de workers concurrentes para la recolección de precios (default 16).
- `DEFAULT_QUOTE_ASSET`: moneda estable por defecto para componer pares al adherir o eliminar desde Telegram (default `USDT`).
- `KEEPALIVE_URL`: URL pública del servicio para auto-ping (ej. `https://arbitrage-telebot-web.onrender.com`).
- `KEEPALIVE_ENABLED`: habilita/deshabilita el auto-ping (`true`/`false`, default activado si `KEEPALIVE_URL` está definido).
- `KEEPALIVE_INTERVAL_SECONDS`: cada cuántos segundos ejecutar el ping (mínimo 60, default 240).
- `KEEPALIVE_TIMEOUT_SECONDS`: timeout HTTP del ping (default 8).

🔎 Playbooks operativos

Revisa `docs/playbooks.md` para procedimientos manuales, semiautomáticos y automatizados ante oportunidades detectadas.

🧠 Cómo funciona (resumen)

Para cada par, obtiene best bid/ask de cada exchange habilitado.

Genera todas las rutas comprar → vender entre venues.

Calcula spread bruto y luego neto (resta fees taker).

Si el neto ≥ umbral, simula PnL para tu capital y envía alerta a Telegram.

Registra la oportunidad en logs/opportunities.csv.
