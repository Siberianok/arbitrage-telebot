# Checklist de mejoras para Arbitrage TeleBot

Este checklist agrupa iniciativas para llevar el bot a un siguiente nivel en cuanto a cobertura de mercados, robustez operativa y calidad de las señales.

## 1. Profundizar cobertura de mercados y rutas
- [ ] Incorporar pares adicionales *crypto/crypto* (p. ej. triángulos USDT/USDC/BUSD) y *crypto/fiat* relevantes para la estrategia.
- [ ] Implementar detección de oportunidades de arbitraje triangular y cross-exchange más allá de la compra-venta simple.
- [ ] Permitir ponderar capital por par o ruta para priorizar los spreads más líquidos.

## 2. Mejorar la calidad y puntualidad de datos
- [ ] Paralelizar las consultas de cotizaciones (p. ej. `ThreadPoolExecutor` o `asyncio`) para reducir latencia.
- [ ] Incorporar caché de profundidad (L2) cuando la API lo permita para estimar slippage y volumen disponible.
- [ ] Validar timestamps y desfase de cada API; descartar datos atrasados o inconsistentes.
- [ ] Añadir verificación de integridad (checksum) de respuestas y fallback a endpoints secundarios.

## 3. Gestión avanzada de fees y costes
- [ ] Registrar y actualizar automáticamente las comisiones por exchange/pareja (niveles VIP, descuentos de token nativo, etc.).
- [ ] Modelar costes de retiro/deposito y tiempos de confirmación para evaluar arbitrajes de “vuelta completa”.
- [ ] Ajustar `estimate_profit` para aplicar comisiones sobre ambas piernas y considerar deslizamiento esperado.
- [ ] Simular impacto de rebalanceo de inventarios (transferencias entre exchanges) y costos asociados.

## 4. Monitoreo, alertas y confiabilidad
- [ ] Centralizar logging estructurado (JSON) y métricas (exitos/fallos por exchange) para observabilidad.
- [ ] Enviar alertas de degradación cuando un exchange quede sin datos o supere cierto porcentaje de errores.
- [ ] Implementar reintentos exponenciales y circuit breakers por exchange para aislar fallas.
- [ ] Crear pruebas automatizadas de conectividad/API que se ejecuten en el despliegue y previo a producción.

## 5. Inteligencia de señales y análisis
- [x] Evaluar precisión histórica de las señales guardadas (`logs/opportunities.csv`) y ajustar thresholds dinámicamente.
- [x] Priorizar oportunidades según liquidez estimada y volatilidad reciente del par.
- [x] Añadir clasificación del tipo de señal (alta confianza, media, baja) basada en métricas de estabilidad.
- [x] Integrar un módulo de backtesting para medir PnL realista (fees, slippage, rebalanceos, latencias).

## 6. Experiencia de usuario y operativa
- [ ] Exponer una interfaz web/dashboard con estado en tiempo real, últimas señales y controles de configuración.
- [ ] Permitir configurar thresholds, capital y pares vía comandos de Telegram seguros o panel web con autenticación.
- [ ] Añadir plantillas de mensajes enriquecidos (Markdown, emojis) con enlaces directos a órdenes de los exchanges.
- [ ] Documentar playbooks de respuesta ante oportunidades (ejecución manual, scripts auxiliares, bots de trading).

## 7. Resiliencia y despliegue
- [ ] Garantizar persistencia de `logs/` (volúmenes o almacenamiento remoto) y copias de respaldo.
- [ ] Añadir health checks más completos (latencia media, último envío Telegram, últimas cotizaciones).
- [ ] Automatizar despliegues con CI/CD que ejecuten tests, linting y validaciones de configuración.
- [ ] Configurar monitoreo externo (Prometheus, Grafana, UptimeRobot) para detectar caídas o atrasos en tiempo real.

Mantener el checklist actualizado tras cada iteración ayuda a priorizar inversiones y medir progreso hacia un bot de señales de arbitraje confiable y único.
