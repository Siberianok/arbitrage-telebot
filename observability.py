"""Compatibility wrapper for observability helpers."""

import arbitrage_telebot.observability as _impl

for _name in dir(_impl):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_impl, _name)
