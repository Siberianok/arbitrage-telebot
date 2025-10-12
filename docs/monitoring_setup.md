# Monitoreo y alertas para Arbitrage TeleBot

## Endpoints expuestos

| Endpoint | Descripción |
| --- | --- |
| `/health`, `/live`, `/ready`, `/metrics` | Respuesta JSON con estado general, latencia media de fetch, últimos envíos a Telegram, timestamp de cotizaciones y pares cubiertos. |
| `/metrics/prom` | Métricas en formato Prometheus 0.0.4 con status binario, timestamps, latencia y resultado del último envío a Telegram. |

## Configuración sugerida de Prometheus

```yaml
scrape_configs:
  - job_name: "arbitrage-telebot"
    scrape_interval: 30s
    metrics_path: /metrics/prom
    static_configs:
      - targets: ["telebot.local:10000"]
```

Métricas clave:

- `arbitrage_bot_status`: 0 = OK, 1 = `status` "stale" (sin corridas recientes).
- `arbitrage_bot_average_fetch_latency_ms`: latencia media de fetch de cotizaciones.
- `arbitrage_bot_last_run_alerts`: cuántas señales se emitieron en la última corrida.
- `arbitrage_bot_last_telegram_send_ok`: 1 si el último envío a Telegram fue exitoso.

### Alertmanager / Grafana Alerting

Ejemplos de reglas:

- Disparar alerta si `arbitrage_bot_status` = 1 durante más de 5 minutos.
- Disparar alerta si `arbitrage_bot_average_fetch_latency_ms` supera 2500 ms en un promedio de 10 minutos.
- Disparar alerta si `arbitrage_bot_last_telegram_send_ok` = 0 por más de 3 corridas consecutivas.

## UptimeRobot

1. Crear un nuevo monitor HTTP(S) a la ruta `/health`.
2. Opcional: añadir un monitor de palabras clave que busque `"status": "ok"` en la respuesta JSON.
3. Configurar alertas por correo/Telegram si el endpoint devuelve error o si el JSON no contiene `"status": "ok"`.

## Dashboards de Grafana

- Crear un panel con latencia, estado y cantidad de alertas.
- Añadir tabla para mostrar `arbitrage_bot_last_run_alerts` por instancia.
- Usar `Query inspector` para validar que Prometheus recibe las métricas actualizadas.

## Backups y persistencia

- Volúmenes persistentes definidos en `deploy/docker-compose.yaml` garantizan que `logs/` y `backups/` no se pierdan.
- Programar `scripts/backup_logs.py` (por ejemplo, `0 * * * * python /app/scripts/backup_logs.py`) para generar respaldos horarios.
- Habilitar subida a S3 con variables `S3_BUCKET` y `S3_PREFIX` para redundancia off-site.
