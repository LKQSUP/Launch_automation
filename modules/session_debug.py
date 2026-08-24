"""Live session diagnostics when the X431 controller is stuck or failing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from modules import adb_controller as adb
from modules.db import fetch_reports, fetch_tickets, fetch_vin_audit


def _visible_texts(serial: str) -> List[str]:
    texts: List[str] = []
    for el in adb.get_ui_elements(serial):
        for key in ("text", "content_desc"):
            value = (el.get(key) or "").strip()
            if value and value not in texts:
                texts.append(value)
    return texts


def _advice_from_screen(texts: List[str], adb_ok: bool, u2_ok: bool) -> List[str]:
    blob = " | ".join(texts).lower()
    tips: List[str] = []

    if not adb_ok:
        tips.append("ADB device not reachable — reconnect USB/Wi-Fi, then Sync ADB.")
        tips.append("Confirm USB debugging is enabled and the tablet is unlocked.")
        return tips
    if not u2_ok:
        tips.append("uiautomator2 is down — reboot tablet or re-run Sync ADB, then retry.")
        tips.append("If it keeps failing, run `python -m uiautomator2 init` once on this PC.")

    if not texts:
        tips.append("UI dump returned no text — screen may be off, locked, or animating. Wake/unlock tablet.")
        return tips

    dialog_hits = (
        "ok",
        "agree",
        "accept",
        "confirm",
        "allow",
        "continue",
        "got it",
        "i know",
        "permission",
        "cancel",
    )
    if any(h in blob for h in dialog_hits) and any(
        x in blob for x in ("permission", "privacy", "update", "notice", "agree", "allow")
    ):
        tips.append("Blocking dialog likely open — dismiss OK / Agree / Allow, then retry the workflow.")

    if "processing" in blob or "intelligent vehicle identification" in blob:
        tips.append("Stuck on Intelligent Vehicle Identification / Processing.")
        tips.append("Check VCI connected, ignition ON, and wait up to ~90s for AutoDetect Result.")
        tips.append("If it never finishes: back out, reopen Intelligent Diagnose, or re-seat the VCI.")

    if "autodetect result" in blob or ("vehicle information" in blob and "diagnostic" in blob):
        tips.append("On AutoDetect Result — VIN should be readable; tap Diagnostic to continue.")
        tips.append("If VIN is missing: confirm vehicle power and VCI, then re-run Intelligent Diagnose.")

    if "system and function" in blob or "system topology" in blob:
        tips.append("On System and Function / Topology — tap High-speed Scan or Smart Detection.")
        tips.append("If scan buttons are missing: swipe the footer area or re-enter Diagnostic.")

    if "high-speed scan" in blob or "smart detection" in blob:
        tips.append("Scan controls are visible — if workflow is stuck, tap High-speed Scan manually.")

    if "intelligent diagnose" in blob and "autodetect" not in blob and "system and function" not in blob:
        tips.append("EURO LINK home visible — start/retry Intelligent Diagnose from the controller page.")

    if "local diagnose" in blob and "intelligent diagnose" in blob:
        tips.append("On home: prefer Intelligent Diagnose for auto VIN; use Local Diagnose only for manual brand path.")

    if not tips:
        tips.append("Unrecognized screen — use the screenshot + visible texts below to locate the UI.")
        tips.append("Recovery: press Back a few times to EURO LINK home, then re-run auto detection.")
        tips.append("If still stuck: Only open EURO LINK, force-stop the app on tablet, relaunch, retry.")

    return tips


def diagnose_session(serial: str) -> Dict[str, Any]:
    """Capture device/session state and produce recovery guidance."""
    result: Dict[str, Any] = {
        "ok": False,
        "serial": serial,
        "device_label": adb.get_device_label(serial),
        "connection": {},
        "device_info": {},
        "screenshot": None,
        "visible_texts": [],
        "screen_summary": "",
        "advice": [],
        "recent_failures": [],
        "recent_tickets": [],
        "recent_audit": [],
        "error": None,
    }

    try:
        status = adb.get_connection_status(serial)
        result["connection"] = status
        result["device_info"] = adb.get_device_info(serial)

        adb_ok = bool(status.get("adb_connected"))
        u2_ok = bool(status.get("u2_connected"))

        shot = None
        texts: List[str] = []
        if adb_ok:
            shot = adb.capture_screenshot(serial)
            if shot:
                result["screenshot"] = str(shot)
            texts = _visible_texts(serial)
            result["visible_texts"] = texts[:80]
            result["screen_summary"] = " · ".join(texts[:12]) if texts else "(no visible text)"

        result["advice"] = _advice_from_screen(texts, adb_ok=adb_ok, u2_ok=u2_ok)

        reports = fetch_reports(limit=30)
        if not reports.empty:
            if "serial" in reports.columns:
                same = reports[reports["serial"].astype(str) == str(serial)]
                if not same.empty:
                    reports = same
            failed = reports[reports["status"].astype(str).str.lower().isin(["failed", "error", "timeout"])]
            if failed.empty:
                failed = reports.head(5)
            else:
                failed = failed.head(10)
            result["recent_failures"] = failed.to_dict("records")

        tickets = fetch_tickets(limit=10)
        if not tickets.empty:
            if "serial" in tickets.columns:
                same_t = tickets[tickets["serial"].astype(str) == str(serial)]
                if not same_t.empty:
                    tickets = same_t
            result["recent_tickets"] = tickets.head(10).to_dict("records")

        audit = fetch_vin_audit(limit=10)
        if not audit.empty:
            if "serial" in audit.columns:
                same_a = audit[audit["serial"].astype(str) == str(serial)]
                if not same_a.empty:
                    audit = same_a
            result["recent_audit"] = audit.head(10).to_dict("records")

        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
        result["advice"] = [
            f"Diagnostics crashed: {exc}",
            "Verify ADB is on PATH and the tablet is connected, then retry Diagnose session.",
        ]

    return result


def list_local_report_files(limit: int = 20) -> List[Dict[str, str]]:
    """List newest text reports under ``reports/``."""
    root = Path(__file__).resolve().parent.parent / "reports"
    if not root.exists():
        return []
    files = sorted(root.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[Dict[str, str]] = []
    for path in files[:limit]:
        out.append(
            {
                "name": path.name,
                "path": str(path),
                "size": str(path.stat().st_size),
            }
        )
    return out
