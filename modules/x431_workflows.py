"""Multi-step Launch X431 Euro Link diagnostic workflow state machine.

Uses uiautomator2 for primary UI automation with local OCR fallback via
``LocalVisionEngine``. No cloud AI APIs are required.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from modules import adb_controller as adb
from modules.db import ensure_database, save_dtc, save_report, set_operator_context
from modules.engineer_session import DEFAULT_REPORT_EMAIL, normalize_report_email
from modules.local_ocr_vision import LocalVisionEngine

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, Optional[float]], None]

# Labels verified against X-431 EURO LINK V8.00.027 home grid.
HOME_AUTO_DIAGNOSE: Sequence[str] = (
    "Intelligent Diagnose",
    "Intelligent Diagnosis",
    "Inttelligent Diagnose",  # OCR typo tolerance
)
HOME_MANUAL_DIAGNOSE: Sequence[str] = (
    "Local Diagnose",
    "Local Diagnosis",
)
HOME_SERVICE: Sequence[str] = (
    "Service Function",
    "Service Functions",
)
LAUNCHER_APP_LABELS: Sequence[str] = (
    "X-431 EURO LINK",
    "X-431 EUROLINK",
    "X431 EURO LINK",
    "EURO LINK",
)

COMMON_DIALOG_LABELS: Sequence[str] = (
    "OK",
    "Confirm",
    "Yes",
    "Agree",
    "Continue",
    "Allow",
    "Accept",
    "Switch Ignition ON",
    "Turn Ignition ON",
    "Ignition ON",
    "Network Notice",
    "Network Connection Required",
    "I Know",
    "Got it",
    "Close",
    "Next",
)

SERVICE_RESET_LABELS: Dict[str, Sequence[str]] = {
    "oil": ("Oil Maintenance Reset", "Oil Reset", "Oil Service Reset", "Service Oil"),
    "brake": ("Brake Reset", "EPB Reset", "Electronic Parking Brake", "Brake Pad Reset"),
    "sas": ("Steering Angle Reset", "SAS Reset", "Steering Angle Sensor", "Steering Angle"),
    "bms": ("BMS Reset", "Battery Reset", "Battery Management", "Battery Registration"),
}


class LaunchX431WorkflowEngine:
    """State-driven automation for Launch X431 Euro Link on a connected tablet."""

    def __init__(
        self,
        serial: str,
        callback: Optional[ProgressCallback] = None,
        vision: Optional[LocalVisionEngine] = None,
        engineer: str = "",
        report_email: str = "",
    ) -> None:
        """Bind the engine to an ADB serial and optional progress callback.

        Args:
            serial: Target device serial.
            callback: Optional ``(message, progress)`` stream for Streamlit.
            vision: Optional shared :class:`LocalVisionEngine` instance.
            engineer: Toolbox login / operator name stored on every scan row.
            report_email: Gmail To: address (empty → hotline.support@lkqbelgium.be).
        """
        self.serial = serial
        self.callback = callback
        self.vision = vision or LocalVisionEngine()
        self.device = None
        self.logs: List[str] = []
        self.engineer = (engineer or "").strip()
        self.report_email = normalize_report_email(report_email)
        ensure_database()
        set_operator_context(self.engineer, self.report_email)

    # ------------------------------------------------------------------ helpers

    def device_label(self) -> str:
        """Friendly ADB label for this tablet (falls back to serial)."""
        return adb.get_device_label(self.serial)

    def device_stamp(self) -> Dict[str, str]:
        """Identity fields to attach to run results / persistence."""
        return {
            "serial": self.serial,
            "device_label": self.device_label(),
            "engineer": self.engineer or "",
            "report_email": self.report_email or DEFAULT_REPORT_EMAIL,
        }

    def _step(self, message: str, progress: Optional[float] = None) -> None:
        self.logs.append(message)
        adb.emit_progress(self.callback, message, progress)
        label = self.device_label()
        tag = f"{label}|{self.serial}" if label != self.serial else self.serial
        logger.info("[%s] %s", tag, message)

    def ensure_device(self):
        """Return a live u2 device, reconnecting only if needed."""
        if self.device is not None:
            return self.device
        self._step("Connecting uiautomator2 session", None)
        self.device = adb.connect_u2(self.serial, reconnect=False)
        self._step("uiautomator2 connected", None)
        return self.device

    def connect(self, reconnect: bool = False):
        """Establish a uiautomator2 session."""
        self._step("Connecting uiautomator2 session", None)
        self.device = adb.connect_u2(self.serial, reconnect=reconnect)
        self._step("uiautomator2 connected", None)
        return self.device

    def _click_by_text(self, labels: Sequence[str], timeout: float = 4.0) -> Optional[str]:
        """Try u2 text / description / xpath matches, then OCR fallback."""
        device = self.ensure_device()
        per_label = max(0.8, min(2.5, timeout / max(len(labels), 1)))
        for label in labels:
            try:
                # Exact text first (EURO LINK V8 uses plain labels).
                node = device(text=label)
                if node.exists(timeout=per_label):
                    node.click()
                    time.sleep(0.5)
                    self._step(f"Clicked text='{label}'")
                    return label
                node = device(textContains=label)
                if node.exists(timeout=0.6):
                    node.click()
                    time.sleep(0.5)
                    self._step(f"Clicked textContains='{label}'")
                    return label
                node = device(textMatches=f"(?i).*{re.escape(label)}.*")
                if node.exists(timeout=0.6):
                    node.click()
                    time.sleep(0.5)
                    self._step(f"Clicked textMatches='{label}'")
                    return label
                node = device(descriptionMatches=f"(?i).*{re.escape(label)}.*")
                if node.exists(timeout=0.5):
                    node.click()
                    time.sleep(0.5)
                    self._step(f"Clicked description='{label}'")
                    return label
                # XPath fallback (more reliable on some Launch builds).
                xp = device.xpath(f'//*[contains(@text, "{label}")]')
                if xp.exists:
                    xp.click()
                    time.sleep(0.5)
                    self._step(f"Clicked xpath text='{label}'")
                    return label
            except Exception as exc:
                logger.debug("u2 click miss for %s: %s", label, exc)

        # ADB UI hierarchy fallback.
        for label in labels:
            if adb.find_and_tap_text(self.serial, label, timeout=2):
                self._step(f"Clicked via UI dump: '{label}'")
                return label

        # Local OCR fallback.
        shot = adb.capture_screenshot(self.serial)
        if shot:
            for label in labels:
                point = self.vision.find_text_bounds(shot, label, confidence=0.35)
                if point:
                    try:
                        device.click(point[0], point[1])
                    except Exception:
                        adb.tap(self.serial, point[0], point[1])
                    time.sleep(0.5)
                    self._step(f"OCR clicked '{label}' at {point}")
                    return label
        return None

    def _text_present(self, labels: Sequence[str], timeout: float = 2.0) -> bool:
        device = self.ensure_device()
        for label in labels:
            try:
                if device(textMatches=f"(?i).*{re.escape(label)}.*").exists(timeout=timeout):
                    return True
                if device(descriptionMatches=f"(?i).*{re.escape(label)}.*").exists(timeout=0.5):
                    return True
            except Exception:
                continue
        return adb.wait_for_text(self.serial, labels[0], timeout=int(timeout)) if labels else False

    def _wait_progress_complete(self, timeout: int = 120) -> bool:
        """Poll until a 100% scan indicator appears (u2 text or OCR)."""
        device = self.ensure_device()
        deadline = time.time() + timeout
        patterns = ("100%", "100 %", "Completed", "Scan Complete", "Finished")
        while time.time() < deadline:
            self.handle_common_dialogs()
            for pattern in patterns:
                try:
                    if device(textMatches=f"(?i).*{re.escape(pattern)}.*").exists(timeout=0.6):
                        return True
                except Exception:
                    pass
            if adb.wait_for_text(self.serial, "100%", timeout=2):
                return True
            shot = adb.capture_screenshot(self.serial)
            if shot:
                for pattern in ("100%", "completed", "finished"):
                    if self.vision.find_text_bounds(shot, pattern, confidence=0.3):
                        return True
            time.sleep(1.5)
        return False

    # --------------------------------------------------------------- public API

    def handle_find_new_version(self, wait_upgrade: float = 600.0) -> bool:
        """If the home 'Find New Version' popup is shown, tap UPDATE and wait.

        Buttons on that dialog: CANCEL | SKIP THIS VERSION | UPDATE.
        We always choose UPDATE so the tablet upgrades (e.g. 212MB package).
        """
        visible = False
        try:
            if self._text_present(("Find New Version",), timeout=0.25):
                visible = True
        except Exception:
            pass
        if not visible:
            try:
                blob = " ".join(
                    (el.get("text") or "") + " " + (el.get("content_desc") or "")
                    for el in adb.get_ui_elements(self.serial)
                ).lower()
            except Exception:
                blob = ""
            if "find new version" in blob or (
                "upgrade package" in blob and "skip this version" in blob
            ):
                visible = True
        if not visible:
            return False

        self._step("Find New Version popup — tapping UPDATE", 0.04)
        tapped = False
        # Exact label only — never "SKIP THIS VERSION"
        try:
            if adb.find_and_tap_text(self.serial, "UPDATE", timeout=3):
                tapped = True
                self._step("Tapped UPDATE (ADB text)")
        except Exception:
            pass
        if not tapped:
            # Rightmost red button on the dialog (typical 1280×800)
            try:
                proc = adb._run_adb(
                    ["-s", self.serial, "shell", "wm", "size"], timeout=4
                )
                text = (proc.stdout or "") + (proc.stderr or "")
                m = re.search(r"(\d+)x(\d+)", text)
                w, h = (int(m.group(1)), int(m.group(2))) if m else (1280, 800)
            except Exception:
                w, h = 1280, 800
            x, y = int(w * 0.78), int(h * 0.62)
            try:
                adb.tap(self.serial, x, y)
                tapped = True
                self._step(f"Tapped UPDATE (layout) at ({x},{y})")
            except Exception as exc:
                self._step(f"UPDATE tap failed: {exc}")
                return False

        # Upgrade can take several minutes (hundreds of MB). Wait for home.
        deadline = time.time() + max(60.0, float(wait_upgrade))
        last_hb = 0.0
        self._step("Waiting for tablet upgrade / home after UPDATE…", 0.05)
        while time.time() < deadline:
            cancel_fn = getattr(self, "raise_if_cancelled", None)
            if callable(cancel_fn):
                cancel_fn()
            try:
                blob = " ".join(
                    (el.get("text") or "") + " " + (el.get("content_desc") or "")
                    for el in adb.get_ui_elements(self.serial)
                ).lower()
            except Exception:
                blob = ""
            still_prompt = "find new version" in blob or (
                "skip this version" in blob and "upgrade package" in blob
            )
            on_home = any(
                t in blob
                for t in (
                    "intelligent diagnose",
                    "intelligent diagnosis",
                    "service function",
                    "local diagnose",
                )
            )
            if on_home and not still_prompt:
                self._step("Upgrade dialog gone — EURO LINK home ready", 0.06)
                return True
            now = time.time()
            if now - last_hb >= 8:
                left = int(deadline - now)
                phase = "upgrading" if not still_prompt else "waiting for UPDATE to start"
                self._step(f"Tablet {phase}… {left}s left", 0.05)
                last_hb = now
            time.sleep(1.0)

        self._step("Upgrade wait timed out — continuing (check tablet if still updating)")
        return True

    def handle_confirm_vehicle_type(self) -> bool:
        """If 'Confirm Vehicle Type' is shown, always tap Automotive (never Heavy Duty)."""
        visible = False
        try:
            if self._text_present(("Confirm Vehicle Type",), timeout=0.25):
                visible = True
        except Exception:
            pass
        blob = ""
        if not visible:
            try:
                blob = " ".join(
                    (el.get("text") or "") + " " + (el.get("content_desc") or "")
                    for el in adb.get_ui_elements(self.serial)
                ).lower()
            except Exception:
                blob = ""
            if "confirm vehicle type" in blob or (
                "automotive" in blob and "heavy duty" in blob
            ):
                visible = True
        if not visible:
            return False

        self._step("Confirm Vehicle Type — tapping Automotive", 0.04)
        tapped = False
        # Prefer exact Automotive bounds (never Heavy Duty)
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                if raw.lower() != "automotive":
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                x, y = (l + r) // 2, (t + b) // 2
                adb.tap(self.serial, x, y)
                self._step(f"Tapped Automotive (ADB bounds) at ({x},{y})")
                tapped = True
                break
        except Exception:
            pass
        if not tapped:
            try:
                if adb.find_and_tap_text(self.serial, "Automotive", timeout=3):
                    tapped = True
                    self._step("Tapped Automotive (ADB text)")
            except Exception:
                pass
        if not tapped:
            try:
                proc = adb._run_adb(
                    ["-s", self.serial, "shell", "wm", "size"], timeout=4
                )
                text = (proc.stdout or "") + (proc.stderr or "")
                m = re.search(r"(\d+)x(\d+)", text)
                w, h = (int(m.group(1)), int(m.group(2))) if m else (1280, 800)
            except Exception:
                w, h = 1280, 800
            # Top red button in the Confirm Vehicle Type dialog
            x, y = int(w * 0.50), int(h * 0.42)
            try:
                adb.tap(self.serial, x, y)
                tapped = True
                self._step(f"Tapped Automotive (layout) at ({x},{y})")
            except Exception as exc:
                self._step(f"Automotive tap failed: {exc}")
                return False

        time.sleep(0.45)
        return True

    def handle_common_dialogs(self, max_rounds: int = 4) -> List[str]:
        """Detect and dismiss recurring Launch popups.

        Returns:
            Labels that were successfully tapped.
        """
        dismissed: List[str] = []
        if self.handle_find_new_version():
            dismissed.append("Find New Version → UPDATE")
        if self.handle_confirm_vehicle_type():
            dismissed.append("Confirm Vehicle Type → Automotive")
        for _ in range(max_rounds):
            hit = self._click_by_text(COMMON_DIALOG_LABELS, timeout=1.5)
            if not hit:
                break
            dismissed.append(hit)
            self._step(f"Dismissed dialog: {hit}")
            time.sleep(0.5)
        return dismissed

    def open_x431(self) -> None:
        """Open X-431 EURO LINK and wait until the home tile grid is usable.

        Raises:
            RuntimeError: If the app cannot be brought to the home screen.
        """
        self._step("Opening X-431 EURO LINK", 0.05)
        # Already on the in-app home grid?
        if self._text_present(HOME_AUTO_DIAGNOSE, timeout=1.5):
            self._step("Already on EURO LINK home (Intelligent Diagnose visible)")
            self.handle_common_dialogs()
            return

        launched = False
        try:
            msg = adb.launch_x431(self.serial)
            self._step(f"Launch result: {msg}", 0.08)
            launched = True
        except Exception as exc:
            self._step(f"Package launch failed: {exc} — trying launcher icon tap")
            tapped = self._click_by_text(LAUNCHER_APP_LABELS, timeout=5)
            if tapped:
                self._step(f"Opened via launcher icon: {tapped}")
                launched = True
            else:
                # OCR fallback for launcher label.
                shot = adb.capture_screenshot(self.serial)
                if shot:
                    for label in LAUNCHER_APP_LABELS:
                        point = self.vision.find_text_bounds(shot, label, confidence=0.3)
                        if point:
                            adb.tap(self.serial, point[0], point[1])
                            self._step(f"OCR launcher tap: {label} @ {point}")
                            launched = True
                            break

        if not launched:
            raise RuntimeError(
                "Could not launch X-431 EURO LINK. "
                "Expected package com.cnlaunch.x431.europro5 (or similar)."
            )

        # Splash / loading can take several seconds on these tablets.
        time.sleep(3.0)
        self.handle_common_dialogs()

        ready_labels = (
            tuple(HOME_AUTO_DIAGNOSE)
            + tuple(HOME_MANUAL_DIAGNOSE)
            + tuple(HOME_SERVICE)
            + ("AutoDetect Result", "Vehicle Information", "Diagnostic")
        )
        for i in range(12):
            if self._text_present(ready_labels, timeout=1.2):
                self._step("EURO LINK UI ready")
                return
            # Re-assert app in foreground mid-wait.
            try:
                import uiautomator2 as u2  # type: ignore

                cur = u2.connect(self.serial).app_current() or {}
                pkg = cur.get("package") or ""
                self._step(f"Foreground package: {pkg or 'unknown'} ({i + 1}/12)")
                if pkg and "x431" not in pkg.lower() and "launch" not in pkg.lower():
                    adb.launch_x431(self.serial)
                    time.sleep(2.0)
            except Exception as exc:
                self._step(f"Foreground check: {exc}")
            self.handle_common_dialogs()
            time.sleep(1.2)

        raise RuntimeError(
            "X-431 package started but home/AutoDetect UI was not detected. "
            "Unlock the tablet and dismiss any blocking dialogs."
        )

    def identify_vehicle(
        self,
        mode: str = "auto",
        make: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, object]:
        """Identify the vehicle from the EURO LINK V8 home screen.

        Args:
            mode: ``"auto"`` taps **Intelligent Diagnose** (VIN auto-ID).
                  ``"manual"`` taps **Local Diagnose** then Make/Model.
            make: Required for manual mode (OEM brand on Local Diagnose list).
            model: Optional model label for manual mode.

        Returns:
            Result dict with ``ok``, ``mode``, and ``logs`` summary fields.
        """
        self.logs = []
        self.ensure_device()
        self.open_x431()
        mode = (mode or "auto").lower().strip()
        result: Dict[str, object] = {"ok": False, "mode": mode, "error": None, **self.device_stamp()}

        try:
            if mode == "manual":
                self._step("Manual mode: tapping Local Diagnose", 0.15)
                if not self._click_by_text(HOME_MANUAL_DIAGNOSE, timeout=6):
                    raise RuntimeError("Could not open Local Diagnose on EURO LINK home")
                self.handle_common_dialogs()

                if not make:
                    raise RuntimeError("Manual mode requires a vehicle make")
                self._step(f"Selecting make: {make}", 0.35)
                if not self._click_by_text((make,), timeout=8):
                    raise RuntimeError(f"Make '{make}' not found on screen")
                self.handle_common_dialogs()

                if model:
                    self._step(f"Selecting model: {model}", 0.5)
                    self._click_by_text((model,), timeout=8)
                    self.handle_common_dialogs()

                self._step("Manual Local Diagnose selection completed", 0.65)
                result["ok"] = True
            else:
                # Already on AutoDetect Result from a previous Intelligent Diagnose run?
                if self._text_present(("AutoDetect Result", "Vehicle Information"), timeout=1.5):
                    self._step("Already on AutoDetect Result — continuing", 0.2)
                else:
                    self._step("Auto mode: tapping Intelligent Diagnose", 0.15)
                    entered = self._click_by_text(HOME_AUTO_DIAGNOSE, timeout=8)
                    if not entered:
                        self._step("Intelligent Diagnose not found — OCR recovery", 0.2)
                        shot = adb.capture_screenshot(self.serial)
                        recovered = False
                        if shot:
                            for label in HOME_AUTO_DIAGNOSE:
                                point = self.vision.find_text_bounds(shot, label, confidence=0.3)
                                if point:
                                    adb.tap(self.serial, point[0], point[1])
                                    recovered = True
                                    self._step(f"OCR recovered: {label}", 0.25)
                                    break
                        if not recovered:
                            raise RuntimeError(
                                "Intelligent Diagnose not found on EURO LINK home screen"
                            )

                self.handle_common_dialogs()
                self._step("Waiting for AutoDetect / VIN decode", 0.4)
                for i in range(15):
                    self.handle_common_dialogs()
                    if self._text_present(
                        (
                            "AutoDetect Result",
                            "Vehicle Information",
                            "Diagnostic",
                            "System Scan",
                            "Health Report",
                            "Read DTCs",
                        ),
                        timeout=1.2,
                    ):
                        break
                    time.sleep(1.5)
                    self._step(f"Waiting for VIN / AutoDetect ({i + 1}/15)", 0.4 + i * 0.02)

                # EURO LINK V8 shows AutoDetect Result with a Diagnostic button.
                if self._text_present(("AutoDetect Result", "Diagnostic"), timeout=2):
                    self._step("AutoDetect Result shown — tapping Diagnostic", 0.55)
                    diag = self._click_by_text(
                        ("Diagnostic", "Diagnose", "Enter", "OK", "Continue"),
                        timeout=6,
                    )
                    if not diag:
                        self._step("Diagnostic button not tapped — continuing anyway", 0.58)
                    else:
                        time.sleep(2.0)
                        self.handle_common_dialogs()

                self._step("Intelligent Diagnose identification completed", 0.65)
                result["ok"] = True

            save_report(
                self.serial,
                f"identify_vehicle_{mode}",
                "completed" if result["ok"] else "failed",
                "\n".join(self.logs),
                None,
            )
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"identify_vehicle error: {exc}", None)
            self.handle_common_dialogs()
            save_report(self.serial, f"identify_vehicle_{mode}", "failed", "\n".join(self.logs), None)

        return result

    def perform_full_dtc_scan_and_clear(
        self,
        auto_clear: bool = True,
        vin: str = "UNKNOWN",
        skip_identify: bool = False,
    ) -> Dict[str, object]:
        """Run Health Report / full DTC scan and optionally clear fault memory.

        Args:
            auto_clear: If True, trigger Clear DTC after scan reaches 100%.
            vin: VIN used when persisting extracted DTC codes.
            skip_identify: If True, assume vehicle is already identified.

        Returns:
            Result dict including ``ok``, ``dtcs``, ``cleared``, and ``logs``.
        """
        self.logs = []
        self.ensure_device()
        result: Dict[str, object] = {
            "ok": False,
            "dtcs": [],
            "cleared": False,
            "error": None,
            **self.device_stamp(),
        }

        try:
            if not skip_identify:
                id_result = self.identify_vehicle(mode="auto")
                if not id_result.get("ok"):
                    self._step("Intelligent Diagnose stalled — dialog/OCR recovery", 0.2)
                    self.handle_common_dialogs()
                    id_result = self.identify_vehicle(mode="auto")
                    if not id_result.get("ok"):
                        raise RuntimeError(id_result.get("error") or "Vehicle identification failed")

            self._step("Opening Health Report / System Scan", 0.7)
            opened = self._click_by_text(
                ("Health Report", "System Scan", "Full System Scan", "Quick Test", "Scan"),
                timeout=8,
            )
            if not opened:
                raise RuntimeError("Could not open Health Report / System Scan")
            self.handle_common_dialogs()

            self._step("Polling DTC scan progress until 100%", 0.78)
            completed = self._wait_progress_complete(timeout=150)
            if not completed:
                self._step("Scan progress indicator not confirmed — continuing with capture", 0.82)
            else:
                self._step("Scan reached completion indicator", 0.85)

            self.handle_common_dialogs()
            dtcs = self._capture_and_store_dtcs(vin=vin)
            result["dtcs"] = dtcs
            self._step(f"Captured {len(dtcs)} DTC code(s)", 0.9)

            if auto_clear:
                self._step("Triggering Clear DTC / Clear Fault Memory", 0.93)
                cleared = self._click_by_text(
                    (
                        "Clear DTC",
                        "Clear Fault Memory",
                        "Erase Codes",
                        "Clear Codes",
                        "Clear Memory",
                        "Erase DTCs",
                    ),
                    timeout=8,
                )
                if cleared:
                    self.handle_common_dialogs()
                    # Confirm secondary prompts.
                    self._click_by_text(("OK", "Confirm", "Yes", "Continue"), timeout=4)
                    result["cleared"] = True
                    self._step(f"Clear action triggered via '{cleared}'", 0.97)
                else:
                    self._step("Clear DTC control not found — scan results retained", 0.97)

            result["ok"] = True
            self._step("Full DTC scan/clear workflow completed", 1.0)
            report_dir = Path(__file__).resolve().parent.parent / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"dtc_scan_{int(time.time())}.txt"
            report_path.write_text(
                f"DEVICE={self.device_label()}\nSERIAL={self.serial}\n"
                + "\n".join(self.logs)
                + "\n",
                encoding="utf-8",
            )
            save_report(
                self.serial,
                "full_dtc_scan_and_clear",
                "completed",
                "\n".join(self.logs),
                str(report_path),
            )
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"perform_full_dtc_scan_and_clear error: {exc}")
            save_report(self.serial, "full_dtc_scan_and_clear", "failed", "\n".join(self.logs), None)

        return result

    def perform_service_reset(self, reset_type: str) -> Dict[str, object]:
        """Open **Service Function** on EURO LINK home and run a guided reset.

        Args:
            reset_type: One of ``oil``, ``brake``, ``sas``, ``bms`` (or a free-text label).

        Returns:
            Result dict with ``ok`` and ``reset_type``.
        """
        self.logs = []
        self.ensure_device()
        key = (reset_type or "").strip().lower()
        aliases = SERVICE_RESET_LABELS.get(key)
        if aliases is None:
            # Accept free-text / UI label passthrough.
            aliases = (reset_type, reset_type.replace("_", " ").title())
            key = key or "custom"

        result: Dict[str, object] = {"ok": False, "reset_type": key, "error": None, **self.device_stamp()}

        try:
            self.open_x431()
            self._step(f"Opening Service Function for '{key}'", 0.2)
            opened = self._click_by_text(HOME_SERVICE, timeout=8)
            if not opened:
                raise RuntimeError("Service Function tile not found on EURO LINK home")
            self.handle_common_dialogs()

            self._step(f"Selecting reset: {aliases[0]}", 0.45)
            selected = self._click_by_text(aliases, timeout=10)
            if not selected:
                raise RuntimeError(f"Service reset option not found for '{reset_type}'")

            self.handle_common_dialogs()
            self._step("Executing guided adaptation / confirmation sequence", 0.7)
            for _ in range(6):
                dismissed = self.handle_common_dialogs()
                advanced = self._click_by_text(
                    ("Start", "Continue", "Next", "Execute", "OK", "Confirm", "Yes", "Finish", "Done"),
                    timeout=3,
                )
                if not dismissed and not advanced:
                    break
                time.sleep(1.0)

            # Completion heuristics.
            if self._text_present(("Success", "Completed", "Finished", "Done"), timeout=3):
                self._step("Reset completion indicator detected", 0.95)
            else:
                self._step("Reset sequence finished (no explicit success banner)", 0.95)

            result["ok"] = True
            self._step(f"Service reset '{key}' completed", 1.0)
            save_report(self.serial, f"service_reset_{key}", "completed", "\n".join(self.logs), None)
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"perform_service_reset error: {exc}")
            save_report(self.serial, f"service_reset_{key}", "failed", "\n".join(self.logs), None)

        return result

    def _capture_and_store_dtcs(self, vin: str) -> List[Dict[str, str]]:
        """Screenshot + OCR DTC extraction persisted to SQLite."""
        shot = adb.capture_screenshot(self.serial)
        extracted: List[Dict[str, str]] = []
        if not shot:
            self._step("Screenshot unavailable for DTC OCR")
            return extracted

        codes = self.vision.extract_dtc_codes(shot)
        for code, desc in codes:
            save_dtc(
                vin or "UNKNOWN",
                code,
                desc,
                source="x431_scan",
                serial=self.serial,
            )
            extracted.append({"code": code, "description": desc})
        return extracted
