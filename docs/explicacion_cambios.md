# Explicación de las mejoras recientes

Estas son las principales mejoras introducidas en el bot tras la última iteración:

## 1. Recálculo y caché del análisis histórico
- Se añadió la función `update_analysis_state` para recalcular los indicadores históricos y el umbral dinámico antes de cada corrida. Esto garantiza que el bot trabaje con datos actualizados del CSV de oportunidades y evita usar métricas obsoletas.
- El resultado del análisis (filas consideradas, tasa de éxito, promedio neto y umbral recomendado) se guarda en `DASHBOARD_STATE['analysis']` para que los paneles de observabilidad puedan mostrar la información más reciente.

## 2. Lógica para calcular el umbral dinámico
- El módulo de análisis calcula un umbral dinámico (`DYNAMIC_THRESHOLD_PERCENT`) a partir de la distribución de señales netas, penalizaciones por costos de ejecución y la tasa de éxito observada. La función `compute_dynamic_threshold` combina estos factores y limita el valor a un rango configurable para evitar saltos bruscos.
- `analyze_historical_performance` reutiliza esa lógica para producir una recomendación basada en backtesting que luego se aplica automáticamente si el análisis está habilitado.

## 3. Uso del umbral dinámico durante los escaneos
- Cada ejecución del bot llama a `update_analysis_state` y utiliza `DYNAMIC_THRESHOLD_PERCENT` (o el umbral base si no está disponible) al filtrar oportunidades y registrar resúmenes. Así se adapta el envío de alertas al desempeño reciente sin perder el control manual del umbral base.
- Los resúmenes del dashboard incluyen tanto el umbral base como el dinámico para facilitar el monitoreo de la configuración efectiva.

## 4. Cobertura con pruebas automatizadas
- `tests/test_analysis.py` valida que `update_analysis_state` actualiza los estados globales, maneja errores del análisis y emite eventos de telemetría, lo que protege contra regresiones en el cálculo del umbral dinámico.
- `tests/test_telegram_polling.py` comprueba que el comando `/status` incluye el valor del umbral dinámico y que el reinicio del webhook de Telegram respeta los periodos de enfriamiento tras conflictos, reforzando la confiabilidad del bot frente a incidencias de red.

En conjunto, estos cambios mejoran la precisión de las alertas, la visibilidad operativa y la estabilidad del bot frente a condiciones cambiantes.
