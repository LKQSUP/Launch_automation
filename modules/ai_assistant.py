"""Local heuristic diagnostic advisor — no cloud AI APIs."""

from __future__ import annotations

import re
from typing import Dict, List


# Critical families where automated clear/reset is discouraged without tech review.
CRITICAL_PREFIXES = ("P0", "P1", "P2", "C0", "C1", "U0")
SAFE_CLEAR_HINTS = ("P05", "P06", "B00", "B10")  # often emissions/body informational


def _severity(code: str) -> str:
    code = code.upper()
    if code.startswith(("P03", "P02", "P001", "P002")):
        return "high"  # misfire / fuel / cam-crank
    if code.startswith(("C00", "C10")):
        return "high"  # chassis / ABS
    if code.startswith(("U01", "U00")):
        return "medium"  # network
    if code.startswith(SAFE_CLEAR_HINTS):
        return "low"
    if code.startswith(CRITICAL_PREFIXES):
        return "medium"
    return "low"


def analyze_dtcs(vin: str, make: str, model: str, dtc_list: List[str]) -> Dict[str, object]:
    """Evaluate DTCs with a local rule engine and recommend safe actions.

    Args:
        vin: Vehicle identification number.
        make: Vehicle make.
        model: Vehicle model.
        dtc_list: List of DTC codes (e.g. ``P0301``).

    Returns:
        Dict with ``recommendation``, ``safe_to_reset``, ``notes``, and ``details``.
    """
    codes = [re.sub(r"[^A-Za-z0-9]", "", c).upper() for c in dtc_list if c and c.strip()]
    codes = [c for c in codes if re.match(r"^[PBCU][0-9A-F]{4}$", c)]

    if not codes:
        return {
            "recommendation": "No valid DTCs provided — nothing to clear",
            "safe_to_reset": False,
            "notes": "Enter OBD-II style codes such as P0301, C0040, U0100.",
            "details": [],
        }

    details = [{"code": c, "severity": _severity(c)} for c in codes]
    high = [d for d in details if d["severity"] == "high"]
    medium = [d for d in details if d["severity"] == "medium"]

    vehicle = f"{make or ''} {model or ''}".strip() or "vehicle"
    if high:
        return {
            "recommendation": "Review with technician — do not auto-clear",
            "safe_to_reset": False,
            "notes": (
                f"Local heuristic flagged {len(high)} high-severity code(s) on {vehicle} "
                f"(VIN {vin}). Investigate root cause before clearing fault memory or "
                f"running service resets."
            ),
            "details": details,
        }

    if medium:
        return {
            "recommendation": "Conditional clear - technician acknowledgement recommended",
            "safe_to_reset": False,
            "notes": (
                f"{len(medium)} medium-severity code(s) detected. Automated clear may hide "
                f"intermittent network/powertrain faults. Prefer scan + report first."
            ),
            "details": details,
        }

    return {
        "recommendation": "Proceed with automated clear / service reset",
        "safe_to_reset": True,
        "notes": (
            f"All {len(codes)} code(s) classified low severity for {vehicle}. "
            f"Safe for automated Clear DTC or routine service reset per local policy."
        ),
        "details": details,
    }
