"""Live ticket polling — real DB rows only (no mock VINs)."""

from typing import Dict, List

from modules.db import ensure_database, fetch_live_tickets, fetch_tickets


def poll_tickets(limit: int = 20) -> List[Dict[str, str]]:
    """Return the latest live ticket per VIN from the database."""
    ensure_database()
    df = fetch_live_tickets(limit=limit)
    if df.empty:
        return []
    rows: List[Dict[str, str]] = []
    for _, row in df.iterrows():
        rows.append(
            {
                "vin": str(row.get("vin") or ""),
                "make": str(row.get("make") or ""),
                "model": str(row.get("model") or ""),
                "status": str(row.get("status") or ""),
                "device_label": str(row.get("device_label") or ""),
                "serial": str(row.get("serial") or ""),
                "engineer": str(row.get("engineer") or ""),
                "last_seen": str(row.get("last_seen") or ""),
            }
        )
    return rows


def ticket_history(limit: int = 50):
    """Return recent ticket history dataframe (real events only)."""
    ensure_database()
    return fetch_tickets(limit=limit)
