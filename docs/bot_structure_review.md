# Arbitrage TeleBot Script Review

## 1. Arquitectura general
- `CONFIG` concentra los parámetros clave (pares, venues, fees, Telegram, logging).
- Los *adapters* (`Binance`, `Bybit`, `KuCoin`, `OKX`) heredan de `ExchangeAdapter` y exponen `fetch_quote`.
- `run_once` construye adaptadores y fees, descarga cotizaciones por par/venue, calcula oportunidades, registra CSV y envía alertas por Telegram.
- La CLI (`main`) permite ejecución única, loop o modo web con *health check*.

## 2. Hallazgos principales
1. **Cobertura limitada de mercados**
   - Solo se monitorizan pares *crypto/USDT* configurados en `CONFIG["pairs"]`.
   - No existe soporte para pares fiat (p.ej. USD/ARS) ni para arbitraje entre stablecoins (USDT/USD, USDT/ARS).
2. **Lógica Telegram dependiente de variables de entorno**
   - Si `TG_BOT_TOKEN` o `TG_CHAT_IDS` no están presentes, el bot imprime un aviso pero no envía nada (`tg_send_message`).
   - No hay métricas ni reintentos; si las variables están mal formateadas los mensajes se pierden silenciosamente.
3. **Tolerancia a fallos de APIs**
   - `http_get_json` reintenta 3 veces pero `fetch_quote` atrapa cualquier excepción y devuelve `None` sin registrar detalle específico.
   - `run_once` omite pares con menos de dos cotizaciones, lo que puede dejar al bot sin señales si cualquier venue falla una sola llamada.
4. **Modelo de fees simplificado**
   - La comisión neta se resta simplemente sumando fees de compra/venta, pero `estimate_profit` aplica los fees como porcentaje sobre el capital inicial (no sobre ambas piernas), lo que subestima el impacto real.
5. **Persistencia de logs**
   - El CSV se crea bajo `logs/opportunities.csv`; si se ejecuta en un entorno inmutable (Render) la carpeta podría no persistir entre deploys.
6. **Escalabilidad / desempeño**
   - Las llamadas HTTP son secuenciales; a mayor número de pares/venues la latencia crecerá linealmente.

## 3. Hipótesis sobre ausencia de señales
- Variables de Telegram no configuradas o `CONFIG["telegram"]["enabled"] = False` en ejecución.
- Los umbrales (`threshold_percent = 0.8`) pueden ser demasiado altos para mercados de baja volatilidad.
- Fallas recurrentes en los endpoints (`fetch_quote` retorna `None`), dejando cada par con <2 cotizaciones válidas.

## 4. Tareas recomendadas
1. **Verificar configuración de despliegue**
   - Confirmar que el proceso se ejecuta con `TG_BOT_TOKEN`, `TG_CHAT_IDS`, `INTERVAL_SECONDS` adecuados.
   - Revisar logs de consola buscando `[TELEGRAM]` o `[<venue>] error fetch`.
2. **Añadir soporte a mercados fiat/stablecoins**
   - Extender `CONFIG["pairs"]` y los adaptadores para incluir pares como `USDT/ARS`, `USD/ARS`, `USDT/USD` según disponibilidad del exchange.
   - Considerar crear un nuevo adapter para brokers locales o APIs FX.
3. **Mejorar manejo de fallos**
   - Registrar excepciones concretas en `fetch_quote` (status code, payload) para facilitar debug.
   - Implementar métricas simples (contador de éxitos/fallos) o enviar una alerta cuando un venue quede sin datos.
4. **Refinar cálculo de fees/PnL**
   - Calcular comisiones sobre ambas piernas en función de la cantidad base/traded y ajustar `estimate_profit`.
   - Permitir fees distintos por par o niveles VIP.
5. **Reprocesar oportunidades**
   - Reducir `threshold_percent` o hacerlo configurable vía variable de entorno para pruebas.
   - Añadir notificaciones cuando se detecten spreads negativos persistentes (indicando posible problema de datos).
6. **Paralelismo / caché**
   - Usar `ThreadPoolExecutor` para descargar cotizaciones en paralelo y reducir tiempos muertos.
7. **Persistencia y observabilidad**
   - Asegurar que la ruta `logs/` sea escribible en producción (configurar almacenamiento persistente o logging remoto).
   - Integrar un endpoint adicional que exponga el último estado/alertas para monitoreo.

## 5. Próximos pasos sugeridos
1. Realizar una corrida manual con `python arbitrage_telebot.py --once` y revisar la salida.
2. Instrumentar logs más detallados (nivel INFO/ERROR estructurado).
3. Priorizar incorporación de mercados FX/cripto-fiat según disponibilidad de APIs.
4. Después de confirmar envíos de Telegram, ajustar threshold y capital simulado acorde a estrategia.
