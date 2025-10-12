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

Diseño extensible: fácil de sumar exchanges, P2P, puentes fiat, límites y manejo de latencias. 🧰

Ejecución v1 (inventario): ejecutás el spread con saldo en ambos exchanges (sin transferencias) — realista para oportunidades rápidas. ⚡

🧠 Cómo funciona (resumen)

Para cada par, obtiene best bid/ask de cada exchange habilitado.

Genera todas las rutas comprar → vender entre venues.

Calcula spread bruto y luego neto (resta fees taker).

Si el neto ≥ umbral, simula PnL para tu capital y envía alerta a Telegram.

Registra la oportunidad en logs/opportunities.csv.
