"""Local adaptive agent loop for Launch X431 tablet automation.

Coordinates perception (uiautomator2 + EasyOCR), popup dismissal, retries,
and task routing without any external cloud AI APIs.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from modules import adb_controller as adb
from modules.local_ocr_vision import LocalVisionEngine
from modules.x431_workflows import LaunchX431WorkflowEngine

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, Optional[float]], None]


class LocalX431AgentLoop:
    """Autonomous executor that routes diagnostic tasks with recovery paths.

    Recovery strategy:
    1. Dismiss common Launch dialogs.
    2. Retry Auto-VIN once.
    3. Fall back to OCR text taps for known entry labels.
    4. Surface a non-blocking error (result dict) instead of crashing Streamlit.
    """

    def __init__(
        self,
        serial: str,
        callback: Optional[ProgressCallback] = None,
        vision: Optional[LocalVisionEngine] = None,
        max_retries: int = 2,
    ) -> None:
        """Create an agent bound to a device serial.

        Args:
            serial: ADB serial of the Launch tablet.
            callback: Optional progress stream for UI.
            vision: Shared local vision engine.
            max_retries: Retries per recoverable failure class.
        """
        self.serial = serial
        self.callback = callback
        self.vision = vision or LocalVisionEngine()
        self.max_retries = max_retries
        self.engine = LaunchX431WorkflowEngine(serial, callback=callback, vision=self.vision)
        self.history: List[Dict[str, Any]] = []

    def _notify(self, message: str, progress: Optional[float] = None) -> None:
        adb.emit_progress(self.callback, message, progress)
        logger.info("[agent:%s] %s", self.serial, message)

    def ensure_ready(self) -> Dict[str, Any]:
        """Verify ADB + u2 connectivity with auto-reconnect."""
        status = adb.get_connection_status(self.serial)
        if status.get("adb_connected") and status.get("u2_connected"):
            self._notify("Device ready (ADB + uiautomator2)", 0.05)
            return status

        self._notify("Device not ready — attempting reconnect", 0.02)
        try:
            adb.sync_adb_devices()
            self.engine.connect(reconnect=True)
            status = adb.get_connection_status(self.serial)
        except Exception as exc:
            status["error"] = str(exc)
            self._notify(f"Reconnect failed: {exc}")
        return status

    def recover_screen(self) -> List[str]:
        """Dismiss popups and attempt OCR recovery taps for common blockers."""
        actions: List[str] = []
        dismissed = self.engine.handle_common_dialogs(max_rounds=5)
        actions.extend(f"dismiss:{d}" for d in dismissed)

        shot = adb.capture_screenshot(self.serial)
        if not shot:
            return actions

        recovery_labels = (
            "OK",
            "Confirm",
            "Yes",
            "Agree",
            "Continue",
            "Intelligent Diagnose",
            "Local Diagnose",
            "Service Function",
            "X-431 EURO LINK",
            "Back",
        )
        for label in recovery_labels:
            point = self.vision.find_text_bounds(shot, label, confidence=0.35)
            if point:
                try:
                    adb.tap(self.serial, point[0], point[1])
                    actions.append(f"ocr_tap:{label}@{point}")
                    self._notify(f"OCR recovery tap: {label} at {point}")
                    time.sleep(0.6)
                    # Refresh screenshot after a successful tap.
                    shot = adb.capture_screenshot(self.serial) or shot
                except Exception as exc:
                    self._notify(f"OCR tap failed for {label}: {exc}")
        return actions

    def run_task(self, task: str, **kwargs: Any) -> Dict[str, Any]:
        """Route and execute a named diagnostic task with recovery.

        Supported tasks:
            - ``auto_vin_health_scan``: Auto-VIN + full DTC scan (optional clear)
            - ``dtc_clear``: Full scan with ``auto_clear=True``
            - ``service_reset``: Requires ``reset_type``
            - ``identify``: Vehicle identification only (``mode``, ``make``, ``model``)

        Returns:
            Structured result; never raises for recoverable device/UI errors.
        """
        started = time.time()
        result: Dict[str, Any] = {
            "task": task,
            "ok": False,
            "error": None,
            "recoveries": [],
            "payload": {},
            "elapsed_s": 0.0,
        }

        ready = self.ensure_ready()
        if not ready.get("adb_connected"):
            result["error"] = ready.get("error") or "ADB device not connected"
            result["elapsed_s"] = time.time() - started
            self.history.append(result)
            return result

        try:
            self.engine.ensure_device()
        except Exception as exc:
            result["error"] = f"uiautomator2 unavailable: {exc}"
            result["elapsed_s"] = time.time() - started
            self.history.append(result)
            return result

        last_error: Optional[str] = None
        for attempt in range(1, self.max_retries + 2):
            try:
                self._notify(f"Task '{task}' attempt {attempt}", 0.08)
                payload = self._dispatch(task, **kwargs)
                if payload.get("ok"):
                    result["ok"] = True
                    result["payload"] = payload
                    break

                last_error = str(payload.get("error") or "Task returned not ok")
                self._notify(f"Task soft-fail: {last_error} — recovering", None)
                recoveries = self.recover_screen()
                result["recoveries"].extend(recoveries)
                if attempt > self.max_retries:
                    result["payload"] = payload
                    result["error"] = last_error
                    break
            except Exception as exc:
                last_error = str(exc)
                self._notify(f"Task exception: {exc} — recovering", None)
                recoveries = self.recover_screen()
                result["recoveries"].extend(recoveries)
                if attempt > self.max_retries:
                    result["error"] = last_error
                    break
                time.sleep(1.0)

        result["elapsed_s"] = round(time.time() - started, 2)
        # Non-blocking: always return a dict for Streamlit to render.
        if not result["ok"] and not result["error"]:
            result["error"] = last_error or "Unknown failure"
        self.history.append(result)
        return result

    def _dispatch(self, task: str, **kwargs: Any) -> Dict[str, Any]:
        """Map task name to workflow engine methods."""
        name = (task or "").strip().lower()

        if name in ("auto_vin_health_scan", "full_auto_scan", "health_scan"):
            auto_clear = bool(kwargs.get("auto_clear", False))
            vin = str(kwargs.get("vin") or "UNKNOWN")
            return self.engine.perform_full_dtc_scan_and_clear(
                auto_clear=auto_clear,
                vin=vin,
                skip_identify=False,
            )

        if name in ("dtc_clear", "full_dtc_clear", "clear_dtc"):
            vin = str(kwargs.get("vin") or "UNKNOWN")
            return self.engine.perform_full_dtc_scan_and_clear(
                auto_clear=True,
                vin=vin,
                skip_identify=bool(kwargs.get("skip_identify", False)),
            )

        if name in ("service_reset", "reset"):
            reset_type = str(kwargs.get("reset_type") or "oil")
            return self.engine.perform_service_reset(reset_type)

        if name in ("identify", "identify_vehicle", "auto_vin"):
            return self.engine.identify_vehicle(
                mode=str(kwargs.get("mode") or "auto"),
                make=kwargs.get("make"),
                model=kwargs.get("model"),
            )

        return {"ok": False, "error": f"Unknown task: {task}"}
