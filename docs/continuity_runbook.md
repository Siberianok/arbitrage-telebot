# Runbook de continuidad operativa

## 1) Separación de procesos lógicos

La operación queda dividida en 3 procesos independientes:

1. **Scanner/engine**
   - Comando: `python arbitrage_telebot.py --role scanner --loop --interval 30`
   - Responsabilidad: recolectar quotes, evaluar oportunidades, registrar eventos y emitir alertas.
2. **API/dashboard**
   - Comando: `python arbitrage_telebot.py --role api --web --port $PORT`
   - Responsabilidad: exponer `/health`, `/live`, `/ready`, `/metrics`, `/api/state` y dashboard.
3. **Worker de Telegram polling**
   - Comando: `python arbitrage_telebot.py --role telegram-worker`
   - Responsabilidad: consumir updates y responder comandos de Telegram sin bloquear scanner.

> Requisito crítico: si `telegram.enabled=true`, debe existir `TG_BOT_TOKEN` (o la env definida en `telegram.bot_token_env`) para los roles `all`, `scanner` y `telegram-worker`. Sin ese secreto, el proceso aborta startup (`exit 1`) y registra `telegram.startup.missing_token`.

## 2) Infra con uptime continuo

- En `render.yaml` cada proceso está en servicio independiente, con plan `starter` (sin sleep por inactividad).
- La recuperación se apoya en health checks por servicio y restart automático administrado por la plataforma.
- Recomendación: desplegar en 2 regiones (primaria + secundaria) para failover manual/automatizado.

## 3) Restart policy + health/liveness por proceso

Endpoints:

- `/live`: liveness básica del proceso.
- `/ready`: readiness operativa.
- `/health`: estado extendido con checks por rol (`scanner_loop`, `telegram_polling`, `api`).

Criterios:

- Scanner degrada si su loop cae o si no actualiza estado por más de `max(90s, interval*3)`.
- Telegram worker degrada si el hilo de polling no está activo.
- API se reporta viva cuando el servidor HTTP responde.

### Validación obligatoria en Render para Telegram worker

Ante cualquier incidencia tipo **"el bot no responde comandos"**, ejecutar este bloque como **primer check obligatorio**:

1. Confirmar que existe un servicio dedicado `arbitrage-telebot-telegram` con `--role telegram-worker` en `render.yaml`.
2. Revisar logs recientes del proceso y validar presencia de eventos de polling/comandos:
   - `telegram.poll.*`
   - `telegram.commands.*`
3. Consultar `/health` del servicio de Telegram y validar:
   - `process.role == "telegram-worker"`
   - `process.checks.telegram_polling.required == true`
   - `process.checks.telegram_polling.alive == true`

Ejemplo de verificación rápida:

```bash
curl -fsS https://<telegram-worker-url>/health | jq '.process'
```

Si `alive=false`, reiniciar el worker y revisar inmediatamente conflictos de polling (`telegram.poll.conflict`) o errores de comando (`telegram.commands.error`).

### Alerta recomendada (opcional)

Crear alerta operativa (Render o monitor externo) sobre `/health` del worker Telegram con condición:

- `process.checks.telegram_polling.required == true`
- `process.checks.telegram_polling.alive == false`

Escalar como incidente de disponibilidad de comandos hasta recuperar `alive=true`.

## 4) Persistencia de logs y estado

- Mantener `LOG_BASE_DIR` y `LOG_BACKUP_DIR` en almacenamiento persistente (volumen/objeto externo).
- Configurar `STATE_DB_URL` para mover estado operacional a DB externa (PostgreSQL recomendado).
- No depender exclusivamente del FS efímero de cada instancia.

## 5) Contingencia operacional

### Failover

1. Detectar degradación (`/health` != `ok` en primario).
2. Escalar temporalmente servicios en región secundaria.
3. Redirigir webhook/API gateway o DNS al secundario.
4. Confirmar métricas y entrega de alertas en Telegram.

### Rotación de tokens (Telegram/API)

1. Generar token nuevo.
2. Cargar secreto en plataforma (`TG_BOT_TOKEN`, `WEB_AUTH_PASS`).
3. Reiniciar worker Telegram y API.
4. Verificar `/health` y comando `/ping`.
5. Revocar token anterior.

### Recuperación de estado tras reinicio

1. Restaurar acceso a `STATE_DB_URL` y almacenamiento de logs.
2. Iniciar primero API, luego scanner y worker Telegram.
3. Validar último snapshot y timestamp de corrida.
4. Ejecutar corrida de diagnóstico (`--diagnose-exchanges`) antes de habilitar alertas críticas.
