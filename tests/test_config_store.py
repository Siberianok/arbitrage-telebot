import config_store


def _base_config():
    return {
        "pairs": ["BTC/USDT", "ETH/USDT"],
        "threshold_percent": 0.3,
        "simulation_capital_quote": 1000,
        "strategies": {"spot_spot": True, "spot_p2p": False},
        "venues": {
            "binance": {"p2p": {"enabled": True, "pairs": {"BTC/USDT": {"fiat": "ARS"}}}},
            "bybit": {"p2p": {"enabled": False}},
        },
    }


def test_runtime_persistence_survives_restart(tmp_path):
    runtime_path = tmp_path / "runtime_config.json"
    base = _base_config()

    runtime_payload = config_store.build_runtime_payload(base)
    runtime_payload["pairs"] = ["SOL/USDT"]
    runtime_payload["threshold_percent"] = 0.7
    config_store.write_runtime_config(runtime_path, runtime_payload)

    loaded, loaded_from_runtime = config_store.load_config_with_runtime(base, runtime_path)

    assert loaded_from_runtime is True
    assert loaded["pairs"] == ["SOL/USDT"]
    assert loaded["threshold_percent"] == 0.7
    assert runtime_path.exists()


def test_runtime_load_rolls_back_when_primary_file_is_corrupt(tmp_path):
    runtime_path = tmp_path / "runtime_config.json"
    base = _base_config()

    valid_payload = config_store.build_runtime_payload(base)
    valid_payload["pairs"] = ["XRP/USDT"]
    config_store.write_runtime_config(runtime_path, valid_payload)

    runtime_path.write_text("{ archivo-json-corrupto", encoding="utf-8")

    loaded, loaded_from_runtime = config_store.load_config_with_runtime(base, runtime_path)

    assert loaded_from_runtime is True
    assert loaded["pairs"] == ["XRP/USDT"]
    restored = runtime_path.read_text(encoding="utf-8")
    assert "XRP/USDT" in restored


def test_validate_runtime_schema_rejects_missing_fields():
    invalid_runtime = {
        "config_version": 1,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "pairs": ["BTC/USDT"],
    }

    try:
        config_store.validate_runtime_schema(invalid_runtime)
    except ValueError as exc:
        assert "Faltan claves requeridas" in str(exc)
    else:
        raise AssertionError("Se esperaba ValueError por esquema incompleto")
