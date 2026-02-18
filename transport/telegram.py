"""Telegram transport compatibility module."""

from arbitrage_telebot import (
    ensure_telegram_polling_thread,
    tg_api_request,
    tg_enable_menu_button,
    tg_handle_command,
    tg_handle_pending_input,
    tg_process_updates,
    tg_send_message,
    tg_sync_command_menu,
)

__all__ = [
    "ensure_telegram_polling_thread",
    "tg_api_request",
    "tg_enable_menu_button",
    "tg_handle_command",
    "tg_handle_pending_input",
    "tg_process_updates",
    "tg_send_message",
    "tg_sync_command_menu",
]
