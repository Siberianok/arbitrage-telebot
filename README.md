arbitrage-telebot

Bot de arbitraje multi-exchange (spot) con alertas en Telegram, cálculo de spread neto (post-fees) y simulación de PnL sobre un capital configurable. Arquitectura lista para extender a P2P/fiat y simulación de “vuelta completa”. Sin humo.

✨ Características

CEX soportados (v1): Binance, Bybit, KuCoin, OKX (spot, best bid/ask).

Pares configurables: BTC/USDT, ETH/USDT, XRP/USDT, ADA/USDT, ALGO/USDT, SHIB/USDT (podés sumar más).

Spread neto = spread bruto − (fee taker buy + fee taker sell).

Umbral de alerta configurable (recomendado 0.8–1.0%).

Simulación de PnL para un capital (ej. 10.000 USDT) adjunta en la alerta.

Alertas Telegram a múltiples destinos (DM y/o canal).

Logs CSV de oportunidades (timestamp, par, venue, spreads, PnL simulado).

Diseño extensible: fácil de sumar nuevos exchanges, P2P, puentes fiat, límites y latencias.

Esta v1 implementa arbitraje de inventario: ejecutás el spread manteniendo saldo en ambos exchanges (sin transferencias), lo que es realista para oportunidades rápidas.

🧠 Cómo funciona (resumen)

Para cada par, obtiene best bid/ask de cada exchange habilitado.

Genera todas las rutas comprar→vender entre venues.

Calcula spread bruto y neto (resta fees taker).

Si el neto ≥ umbral, simula PnL para tu capital y envía alerta a Telegram.

Registra la oportunidad en logs/opportunities.csv.
