import importlib


MODULES = [
    "arbitrage_telebot",
    "arbitrage_telebot.config_store",
    "arbitrage_telebot.observability",
    "arbitrage_telebot.runtime_state",
    "arbitrage_telebot.runtime.runner",
    "arbitrage_telebot.transport.telegram",
    "arbitrage_telebot.web.dashboard",
]


def test_import_smoke_modules_load_without_side_effect_errors():
    for module_name in MODULES:
        module = importlib.import_module(module_name)
        assert module is not None
