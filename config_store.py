from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

RUNTIME_CONFIG_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_yaml_path(path: Path) -> bool:
    return path.suffix.lower() in {".yaml", ".yml"}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _read_raw(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if _is_yaml_path(path):
        if yaml is None:
            raise ValueError("Formato YAML no soportado: faltan dependencias")
        loaded = yaml.safe_load(text)
    else:
        loaded = json.loads(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("El archivo de configuración runtime debe ser un objeto")
    return loaded


def validate_runtime_schema(runtime_cfg: Dict[str, Any]) -> None:
    if not isinstance(runtime_cfg, dict):
        raise ValueError("La configuración runtime debe ser un objeto")

    required = {
        "config_version",
        "updated_at",
        "pairs",
        "threshold_percent",
        "simulation_capital_quote",
        "strategies",
        "p2p",
    }
    missing = [key for key in sorted(required) if key not in runtime_cfg]
    if missing:
        raise ValueError(f"Faltan claves requeridas: {', '.join(missing)}")

    if not isinstance(runtime_cfg.get("config_version"), int):
        raise ValueError("config_version debe ser int")
    if not isinstance(runtime_cfg.get("updated_at"), str) or not runtime_cfg["updated_at"].strip():
        raise ValueError("updated_at debe ser string no vacío")
    if not isinstance(runtime_cfg.get("pairs"), list) or not runtime_cfg["pairs"]:
        raise ValueError("pairs debe ser una lista no vacía")
    if not isinstance(runtime_cfg.get("threshold_percent"), (int, float)):
        raise ValueError("threshold_percent debe ser numérico")
    if not isinstance(runtime_cfg.get("simulation_capital_quote"), (int, float)):
        raise ValueError("simulation_capital_quote debe ser numérico")
    if not isinstance(runtime_cfg.get("strategies"), dict):
        raise ValueError("strategies debe ser objeto")
    if not isinstance(runtime_cfg.get("p2p"), dict):
        raise ValueError("p2p debe ser objeto")


def build_runtime_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    runtime_payload = {
        "config_version": RUNTIME_CONFIG_VERSION,
        "updated_at": utc_now_iso(),
        "pairs": copy.deepcopy(config.get("pairs", [])),
        "threshold_percent": float(config.get("threshold_percent", 0.0)),
        "simulation_capital_quote": float(config.get("simulation_capital_quote", 0.0)),
        "strategies": copy.deepcopy(config.get("strategies", {})),
        "p2p": {
            venue: copy.deepcopy((venue_cfg or {}).get("p2p", {}))
            for venue, venue_cfg in (config.get("venues") or {}).items()
        },
    }
    validate_runtime_schema(runtime_payload)
    return runtime_payload


def apply_runtime_overrides(base_config: Dict[str, Any], runtime_payload: Dict[str, Any]) -> Dict[str, Any]:
    validate_runtime_schema(runtime_payload)

    overrides = {
        "pairs": runtime_payload.get("pairs", []),
        "threshold_percent": runtime_payload.get("threshold_percent"),
        "simulation_capital_quote": runtime_payload.get("simulation_capital_quote"),
        "strategies": runtime_payload.get("strategies", {}),
    }
    merged = _deep_merge(base_config, overrides)

    p2p_overrides = runtime_payload.get("p2p", {})
    for venue, venue_p2p in p2p_overrides.items():
        venue_cfg = merged.setdefault("venues", {}).setdefault(venue, {})
        venue_cfg["p2p"] = _deep_merge(venue_cfg.get("p2p", {}), venue_p2p or {})

    merged["config_version"] = int(runtime_payload.get("config_version", RUNTIME_CONFIG_VERSION))
    merged["updated_at"] = str(runtime_payload.get("updated_at") or utc_now_iso())
    return merged


def load_config_with_runtime(base_config: Dict[str, Any], runtime_path: Path) -> Tuple[Dict[str, Any], bool]:
    if not runtime_path.exists():
        config = copy.deepcopy(base_config)
        config.setdefault("config_version", RUNTIME_CONFIG_VERSION)
        config.setdefault("updated_at", utc_now_iso())
        return config, False

    backup_path = runtime_path.with_suffix(runtime_path.suffix + ".bak")

    try:
        runtime_payload = _read_raw(runtime_path)
        merged = apply_runtime_overrides(base_config, runtime_payload)
        if runtime_payload:
            write_runtime_config(runtime_path, runtime_payload)
        return merged, True
    except Exception:
        if backup_path.exists():
            backup_payload = _read_raw(backup_path)
            merged = apply_runtime_overrides(base_config, backup_payload)
            write_runtime_config(runtime_path, backup_payload)
            return merged, True

    config = copy.deepcopy(base_config)
    config.setdefault("config_version", RUNTIME_CONFIG_VERSION)
    config.setdefault("updated_at", utc_now_iso())
    return config, False


def write_runtime_config(runtime_path: Path, runtime_payload: Dict[str, Any]) -> None:
    validate_runtime_schema(runtime_payload)

    if _is_yaml_path(runtime_path):
        if yaml is None:
            raise ValueError("Formato YAML no soportado: faltan dependencias")
        serialized = yaml.safe_dump(runtime_payload, sort_keys=True, allow_unicode=True)
    else:
        serialized = json.dumps(runtime_payload, ensure_ascii=False, indent=2, sort_keys=True)

    backup_path = runtime_path.with_suffix(runtime_path.suffix + ".bak")
    _atomic_write(runtime_path, serialized + "\n")
    _atomic_write(backup_path, serialized + "\n")
