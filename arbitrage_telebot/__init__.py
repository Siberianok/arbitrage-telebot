"""Main package for arbitrage telebot.

Loads legacy implementation into package namespace so existing monkeypatch-based
call sites keep working while enabling package submodules.
"""

from pathlib import Path

_LEGACY_PATH = Path(__file__).with_name("legacy.py")
exec(compile(_LEGACY_PATH.read_text(encoding="utf-8"), str(_LEGACY_PATH), "exec"), globals(), globals())
