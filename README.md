ARBITRAJE-TELEBOT

Bot de arbitraje multi-exchange (spot) con alertas en Telegram, cálculo de spread neto (post-fees) y simulación de PnL sobre un capital configurable.
Arquitectura lista para extender a P2P/fiat y simulación de “vuelta completa”. Sin humo. 🚀

✨ Características

CEX soportados (v1): Binance, Bybit, KuCoin, OKX (spot, best bid/ask).

Pares configurables: BTC/USDT, ETH/USDT, XRP/USDT, ADA/USDT, ALGO/USDT, SHIB/USDT (podés sumar más).

Spread neto: spread_neto = spread_bruto − (fee_taker_buy + fee_taker_sell).

Umbral de alerta: configurable (recomendado 0.8–1.0%).

Simulación de PnL: incluida en cada alerta (ej.: capital = 10,000 USDT). 🧮

Inteligencia histórica: ajuste dinámico del threshold, backtesting con costes realistas y clasificación de confianza por señal. 📈

Alertas Telegram: múltiples destinos (DM y/o canal). 🔔

Logs CSV: logs/opportunities.csv (timestamp, par, venues, spreads, PnL simulado) con backups automáticos vía `scripts/backup_logs.py`. 🧾

Observabilidad reforzada: endpoint `/health` JSON con latencias promedio, último envío Telegram y últimas cotizaciones, `/metrics/prom` listo para Prometheus y CI/CD con linting, compilación y validación de configuración.

Dashboard web: panel autenticado con estado en tiempo real, historial de alertas y edición segura de configuración. 📊

Diseño extensible: fácil de sumar exchanges, P2P, puentes fiat, límites y manejo de latencias. 🧰

Ejecución v1 (inventario): ejecutás el spread con saldo en ambos exchanges (sin transferencias) — realista para oportunidades rápidas. ⚡

📟 Comandos de Telegram disponibles

| Comando | Descripción |
| --- | --- |
| `/help` | Muestra el listado de comandos disponibles. |
| `/ping` | Devuelve `pong` para verificar la conectividad del bot. |
| `/status` | Resume el threshold configurado, pares monitoreados y chats registrados. |
| `/threshold <valor>` | Consulta o actualiza el umbral de alerta (en %) _(solo admins pueden modificar)_. |
| `/capital <USDT>` | Consulta o ajusta el capital simulado en USDT _(solo admins pueden modificar)_. |
| `/pairs` | Lista los pares configurados actualmente. |
| `/addpair <PAR>` | Agrega un nuevo par (por ejemplo `BTC/USDT`) _(solo admins)_. |
| `/delpair <PAR>` | Elimina un par del monitoreo _(solo admins)_. |
| `/test` | Envía una señal de prueba para confirmar entregas. |

Para restringir quién puede modificar parámetros, definir `TG_ADMIN_IDS` (lista de chat IDs separados por coma). Si no se configura, cualquier chat registrado podrá editar la configuración.

📈 Panel web

Ejecutar el bot con `--web` expone:

- `/health` — endpoint simple para liveness/readiness.
- `/` — dashboard autenticado con estado y controles.
- `/api/state` — snapshot JSON para integraciones.
- `/api/config` — actualización de configuración vía POST autenticado.

Configurar credenciales básicas vía variables de entorno:

```
export WEB_AUTH_USER="operador"
export WEB_AUTH_PASS="clave-super-segura"
```

Luego iniciar con `python arbitrage_telebot.py --web --interval 30 --port 10000`.

🔎 Playbooks operativos

Revisa `docs/playbooks.md` para procedimientos manuales, semiautomáticos y automatizados ante oportunidades detectadas.

🧠 Cómo funciona (resumen)

Para cada par, obtiene best bid/ask de cada exchange habilitado.

Genera todas las rutas comprar → vender entre venues.

Calcula spread bruto y luego neto (resta fees taker).

Si el neto ≥ umbral, simula PnL para tu capital y envía alerta a Telegram.

Registra la oportunidad en logs/opportunities.csv.

## Despliegue y persistencia de logs

### Docker Compose con volúmenes persistentes

```bash
cd deploy
docker compose up -d
```

El archivo `deploy/docker-compose.yaml` monta volúmenes `telebot-logs` y `telebot-backups` para que `logs/` y `backups/` sobrevivan
a reinicios o recreaciones del contenedor. Podés redefinir la ruta del CSV con `LOG_CSV_PATH=/data/logs/opportunities.csv` si montás
un volumen externo.

### Backups automatizados

Ejecutá `scripts/backup_logs.py` desde un cron o pipeline para comprimir `logs/` y guardar un respaldo en `backups/`. Opcionalmente
acepta `--s3-bucket`/`--s3-prefix` (o variables `S3_BUCKET`/`S3_PREFIX`) para subir el archivo a Amazon S3.

```bash
python scripts/backup_logs.py --logs-dir logs --backups-dir backups --s3-bucket mi-bucket --s3-prefix arbitrage/telebot
```

### Render.com

- Configurá un disco persistente en Render montado en `/var/data` y ajustá `LOG_CSV_PATH=/var/data/logs/opportunities.csv` (ya
  definido en `render.yaml`).
- Programá una tarea cron en Render (o un job separado) que ejecute `python scripts/backup_logs.py --logs-dir /var/data/logs --backups-dir /var/data/backups`.

## Health checks y monitoreo externo

- `GET /health` `/live` `/ready`: JSON con latencia promedio de fetch, último envío a Telegram, timestamp de últimas cotizaciones y
  pares con datos frescos.
- `GET /metrics`: mismo JSON para integraciones ligeras.
- `GET /metrics/prom`: métrica en formato Prometheus (status, último run, latencia, alertas, estado de Telegram).

### Integraciones sugeridas

- **Prometheus/Grafana**: añadí un job de scrape apuntando a `/metrics/prom` y grafica las métricas en Grafana.
- **UptimeRobot / health-checkers**: monitorizá `https://tu-dominio/health` (espera HTTP 200) y añadí alertas si el JSON indica
  `status="stale"`.
- **Alertas adicionales**: Grafana Alerting o Prometheus Alertmanager pueden dispararse si `arbitrage_bot_status` ≠ 0 o si
  `arbitrage_bot_average_fetch_latency_ms` supera tu umbral.

## CI/CD

El workflow `.github/workflows/ci.yml` ejecuta linting (`ruff`), compilación (`python -m compileall`) y validaciones de configuración
en cada push/pull request para detectar errores antes de desplegar.
