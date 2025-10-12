#!/usr/bin/env python3
"""Validaciones rápidas sobre la configuración estática del bot."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from arbitrage_telebot import CONFIG


def validate_pairs(errors: List[str]) -> None:
    pairs = CONFIG.get("pairs", [])
    if not isinstance(pairs, list) or not pairs:
        errors.append("CONFIG['pairs'] debe ser una lista no vacía")
        return

    normalized = set()
    for pair in pairs:
        if not isinstance(pair, str):
            errors.append(f"Par inválido (no string): {pair!r}")
            continue
        if "/" not in pair:
            errors.append(f"Par sin separador /: {pair}")
        upper = pair.upper()
        if upper in normalized:
            errors.append(f"Par duplicado: {upper}")
        normalized.add(upper)


def validate_threshold(errors: List[str]) -> None:
    threshold = CONFIG.get("threshold_percent")
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        errors.append("CONFIG['threshold_percent'] debe ser numérico")
        return
    if value <= 0:
        errors.append("CONFIG['threshold_percent'] debe ser positivo")
    if value > 10:
        errors.append("CONFIG['threshold_percent'] parece excesivo (>10%). Revisa el valor")


def validate_log_path(errors: List[str]) -> None:
    log_path = CONFIG.get("log_csv_path")
    if not isinstance(log_path, str) or not log_path.strip():
        errors.append("CONFIG['log_csv_path'] debe ser una cadena no vacía")


def validate_venues(errors: List[str]) -> None:
    venues = CONFIG.get("venues", {})
    if not isinstance(venues, dict) or not venues:
        errors.append("CONFIG['venues'] debe ser un dict no vacío")
        return
    for name, data in venues.items():
        if not isinstance(data, dict):
            errors.append(f"Venue {name} debe ser un dict")
            continue
        if "enabled" not in data or not isinstance(data["enabled"], bool):
            errors.append(f"Venue {name} necesita flag booleano 'enabled'")
        fee = data.get("taker_fee_percent")
        try:
            fee_value = float(fee)
        except (TypeError, ValueError):
            errors.append(f"Venue {name} tiene fee inválido: {fee}")
            continue
        if fee_value < 0 or fee_value > 1:
            errors.append(f"Venue {name} tiene fee fuera de rango esperado (0-1%): {fee_value}")


def main() -> None:
    errors: List[str] = []
    validate_pairs(errors)
    validate_threshold(errors)
    validate_log_path(errors)
    validate_venues(errors)

    if errors:
        for err in errors:
            print(f"[CONFIG] {err}")
        sys.exit(1)
    print("Configuración válida ✔️")


if __name__ == "__main__":
    main()
