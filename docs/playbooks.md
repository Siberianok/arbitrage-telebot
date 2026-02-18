# Playbooks de respuesta ante oportunidades

## 1. Evaluación rápida (manual)
1. Abrir el dashboard web autenticado (`/` del bot) y validar:
   - Última ejecución y spreads netos.
   - Historial de alertas para confirmar repetición o novedad.
   - Configuración activa (threshold, capital, pares habilitados).
2. Confirmar liquidez en los exchanges objetivo con las ligas rápidas de "Comprar" y "Vender".
3. Validar disponibilidad de capital en cada venue y latencia de transferencias internas.
4. Ejecutar órdenes manuales en los exchanges con las ligas directas desde Telegram o dashboard.

## 2. Ejecución asistida con scripts
1. Tomar el CSV `logs/opportunities.csv` para parametrizar el script de trading.
2. Lanzar el script con límites de slippage y tamaño de orden basados en el capital simulado de la alerta.
3. Monitorear el panel para confirmar spreads posteriores a la ejecución.
4. Registrar resultados en hoja de control compartida.

## 3. Automatización con bots de trading
1. Ajustar el threshold mediante `/threshold [valor]` (solo administradores, respeta min/máx configurados) y actualizar el capital simulado con `/capital [monto]` o desde dashboard para adecuarlos al algoritmo.
2. Habilitar el bot de trading con API keys en modo "paper" y validar ejecución contra las alertas recibidas.
3. Pasar a modo real cuando se verifiquen métricas de latencia y slippage.
4. Mantener monitorización continua del dashboard y Telegram para detectar divergencias.

## 4. Gestión de incidentes
1. Si el dashboard deja de actualizar, revisar logs del servicio web (`/health`) y reiniciar proceso.
2. Ante errores de APIs de exchanges, considerar pausar alertas temporalmente bajando el threshold a un valor alto.
3. Documentar incidentes en un runbook y actualizar este playbook con lecciones aprendidas.

## 5. Checklist post-operación
- Confirmar ejecución en ambos exchanges.
- Reconciliar balances y fees cobrados.
- Actualizar threshold y capital con `/threshold` y `/capital` si el mercado cambió.
- Archivar logs CSV y capturas relevantes en el repositorio de operaciones.


## 6. Comandos operativos de referencia
- `/start`: registro inicial del chat y ayuda contextual.
- `/status`: estado del bot (threshold base/dinámico, histórico y pares).
- `/threshold [valor]`: lectura/ajuste de threshold (solo admins).
- `/capital [monto]`: lectura/ajuste de capital simulado (solo admins).
- `/pairs`, `/addpair`, `/delpair`: gestión de universo de pares (`/listapares`, `/adherirpar`, `/eliminarpar` se mantienen como alias).
- `/test`: validación de entrega de alertas (`/senalprueba` como alias).
