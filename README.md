ARBITRAJE-TELEBOT

Bot de arbitraje multi-exchange (spot) con alertas en Telegram, cálculo de spread neto (post-fees) y simulación de PnL sobre un capital configurable.
Arquitectura lista para extender a P2P/fiat y simulación de “vuelta completa”. Sin humo. 🚀

✨ Características

CEX soportados (v1): Binance, Bybit, KuCoin, OKX (spot, best bid/ask).

Pares configurables: BTC/USDT, ETH/USDT, XRP/USDT, ADA/USDT, ALGO/USDT, SHIB/USDT (podés sumar más).

Spread neto: spread_neto = spread_bruto − (fee_taker_buy + fee_taker_sell).

Umbral de alerta: configurable (recomendado 0.8–1.0%).

Simulación de PnL: incluida en cada alerta (ej.: capital = 10,000 USDT). 🧮

Alertas Telegram: múltiples destinos (DM y/o canal). 🔔

Logs CSV: logs/opportunities.csv (timestamp, par, venues, spreads, PnL simulado). 🧾

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
