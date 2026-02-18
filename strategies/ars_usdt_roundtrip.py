from __future__ import annotations

from typing import Any, Dict, List, Optional


def _has_transfer_path(transfers: Dict[str, Dict[str, Any]], from_venue: str, to_venue: str, asset: str) -> bool:
    if from_venue == to_venue:
        return True
    src = transfers.get(from_venue, {}) if isinstance(transfers, dict) else {}
    dst = transfers.get(to_venue, {}) if isinstance(transfers, dict) else {}
    return asset in src and asset in dst


def compute_ars_usdt_roundtrip(
    *,
    spot_quotes: Dict[str, Dict[str, float]],
    p2p_quotes: Dict[str, Dict[str, Any]],
    transfers: Optional[Dict[str, Dict[str, Any]]] = None,
    asset: str = "USDT",
    fiat: str = "ARS",
) -> List[Dict[str, Any]]:
    """Calcula rutas ARS/USDT de ida y vuelta para evaluación rápida.

    Retorna tres familias de rutas:
    1) ARS -> spot buy USDT -> P2P sell USDT a ARS
    2) ARS -> P2P buy USDT -> spot sell USDT
    3) Cross-venue entre venues distintos solo cuando hay transfer habilitada.
    """

    transfers = transfers or {}
    opportunities: List[Dict[str, Any]] = []

    for spot_venue, spot in spot_quotes.items():
        spot_ask = float(spot.get("ask", 0.0) or 0.0)
        spot_bid = float(spot.get("bid", 0.0) or 0.0)
        if spot_ask <= 0 and spot_bid <= 0:
            continue

        for p2p_venue, p2p in p2p_quotes.items():
            if str(p2p.get("fiat", fiat)).upper() != fiat.upper():
                continue
            p2p_ask = float(p2p.get("ask", 0.0) or 0.0)
            p2p_bid = float(p2p.get("bid", 0.0) or 0.0)

            if spot_ask > 0 and p2p_bid > 0:
                gross = (p2p_bid - spot_ask) / spot_ask * 100.0
                opportunities.append(
                    {
                        "strategy": "ars_usdt_roundtrip",
                        "route": "spot_to_p2p",
                        "asset": asset,
                        "fiat": fiat,
                        "buy_venue": spot_venue,
                        "sell_venue": f"{p2p_venue}_p2p",
                        "buy_price": spot_ask,
                        "sell_price": p2p_bid,
                        "gross_percent": gross,
                        "cross_venue": spot_venue != p2p_venue,
                        "transfer_enabled": _has_transfer_path(transfers, spot_venue, p2p_venue, asset),
                    }
                )

            if p2p_ask > 0 and spot_bid > 0:
                gross = (spot_bid - p2p_ask) / p2p_ask * 100.0
                opportunities.append(
                    {
                        "strategy": "ars_usdt_roundtrip",
                        "route": "p2p_to_spot",
                        "asset": asset,
                        "fiat": fiat,
                        "buy_venue": f"{p2p_venue}_p2p",
                        "sell_venue": spot_venue,
                        "buy_price": p2p_ask,
                        "sell_price": spot_bid,
                        "gross_percent": gross,
                        "cross_venue": spot_venue != p2p_venue,
                        "transfer_enabled": _has_transfer_path(transfers, p2p_venue, spot_venue, asset),
                    }
                )

    return [
        opp
        for opp in sorted(opportunities, key=lambda item: item.get("gross_percent", 0.0), reverse=True)
        if not opp.get("cross_venue") or opp.get("transfer_enabled")
    ]
