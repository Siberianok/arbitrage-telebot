# Checklist operativo (A, B y C)

## A) Configuración

- [x] Modo datos reales con `test_mode.enabled=False` y `pause_live_requests=False`.
- [x] Pares spot habilitados: `BTC/USDT`, `ETH/USDT`, `XRP/USDT`.
- [x] Estrategias activas: `spot_spot`, `spot_p2p`, `p2p_p2p` y `triangular_intra_venue`.
- [x] Rutas triangulares sembradas por venue: `USDT→BTC→ETH→USDT` y `USDT→ETH→BTC→USDT`.
- [x] Umbral neto unificado en `threshold_percent = 0.30`.
- [x] Fees/slippage configurados por venue, incluyendo fees P2P estimados y costos de transferencias cross-exchange.
- [x] `max_quote_age_seconds` ajustado a 12 segundos.
- [x] Límites operativos definidos (`min_notional`, `min_qty`, `step_size`) por venue/par.

## B) Normalización y validaciones

- [x] Pares normalizados a `BASE/QUOTE` en mayúsculas, sin duplicados.
- [x] Estrategia spot↔spot requiere dos venues con cotización fresca o marca `[SKIP] … <2 venues`.
- [x] Conversión de libros P2P a bids/asks efectivos con filtros aplicados y validación de método de pago/monto mínimo.
- [x] Triángulos intra-venue exigen tres piernas líquidas con timestamps coherentes y fees por pierna.
- [x] Transfer checks consideran fees/ETA y descartan rutas bajo umbral (`transfer_fee/ETA`).
- [x] Se verifican `min_notional`/`min_qty` antes de aceptar cualquier señal.

## C) Logs y reporting

- [x] Flags de arranque impresos (`test_mode.enabled`, `pause_live_requests`, estrategias activas).
- [x] Cobertura spot emitida como `[COVERAGE] PAR: ['venue1','venue2']`.
- [x] Cobertura P2P emitida como `[P2P] ASSET: venue=… fiat=… offers=… side=… filtros=… elegido=…`.
- [x] Rutas triangulares logueadas con `[TRI]` en el CSV dedicado.
- [x] Motivos de descarte explícitos: `<2 venues`, `p2p_sin_ofertas`, `min_notional`, `transfer_fee/ETA`, `stale_quote`.
- [x] Resumen por tick: `alerts_spot`, `alerts_p2p`, `alerts_tri` y duración total.

