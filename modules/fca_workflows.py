"""FCA (Fiat / Stellantis) service-reset workflows on Launch X431 EURO LINK.

Current path: Oil Maintenance Reset on SGW vehicles (Fiat 500e, 500X, etc.).

Flow:
  Intelligent Diagnose → AutoDetect Result → Diagnostic
  → Fiat vehicle ID popup OK → Please Wait (reading)
  → (SGW unlock Success/Fail OK if shown) → System and Function / Topology
  → Common Special Function → Oil Maintenance Reset
  → OK / Continue until "Service Information Have Been Reset" → OK
  → hard reset (force-stop + relaunch EURO LINK home)
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple

from modules import adb_controller as adb
from modules.db import save_report, save_ticket, save_vin_audit
from modules.vag_workflows import VAGWorkflowEngine


POPUP_MARKERS = (
    "please record",
    "identified wrong vehicle",
    "prompt information",
    "secure gateway",
    "reset service information",
    "model name",
    "model description",
    "car code",
    "body name",
    "cancel",
)

FIAT_POPUP_MARKERS = (
    "model name",
    "please record",
    "identified wrong vehicle",
    "car code",
    "body name",
    "model description",
)

RESET_DONE = "service information have been reset"


class FCAWorkflowEngine(VAGWorkflowEngine):
    """FCA oil reset; reuses Intelligent Diagnose / AutoDetect / Diagnostic taps."""

    def _fca_blob_is_autodetect(self, blob: str) -> bool:
        """True when AutoDetect Result page is visible."""
        low = (blob or "").lower()
        if "autodetect result" in low:
            return True
        if "scan history" in low and ("vehicle information" in low or "diagnostic" in low):
            return True
        if "vin:" in low and "make:" in low:
            return True
        return False

    def _fca_adb_ui_texts(self) -> List[str]:
        """Read visible labels via ADB uiautomator dump (works when u2 is wedged)."""
        texts: List[str] = []
        try:
            for el in adb.get_ui_elements(self.serial):
                for key in ("text", "content_desc"):
                    val = (el.get(key) or "").strip()
                    if val:
                        texts.append(val)
        except Exception:
            pass
        return texts

    def _fca_adb_ui_blob(self) -> str:
        return " ".join(self._fca_adb_ui_texts()).lower()

    def _fca_bounds_center(self, bounds_str: str) -> Optional[Tuple[int, int]]:
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str or "")
        if not match:
            return None
        l, t, r, b = (int(match.group(i)) for i in range(1, 5))
        return (l + r) // 2, (t + b) // 2

    def _fca_is_fiat_vehicle_popup(self, blob: str = "") -> bool:
        """Fiat/Stellantis vehicle identification modal (VIN + Model Name + OK/CANCEL)."""
        low = (blob or "").lower()
        if not low:
            if self._u2_has_text("Model Name", timeout=0.08):
                return self._u2_has_text("CANCEL", timeout=0.06) or self._u2_has_text("OK", timeout=0.06)
            low = self._fca_adb_ui_blob()
        if not low:
            return False
        has_popup_text = any(marker in low for marker in FIAT_POPUP_MARKERS)
        has_vin = bool(re.search(r"vin\s*[:：]", low))
        has_actions = "cancel" in low
        if has_popup_text and has_actions:
            return True
        if has_vin and has_actions and ("model" in low or "fiat" in low or "jeep" in low):
            return True
        return False

    def _fca_tap_confirm_ok(self, xml: Optional[str] = None) -> bool:
        """Tap OK on modal dialogs — always the right/red button, never CANCEL."""
        # Fast path: live u2 click on exact "OK" (works for single-OK SGW success dialog).
        try:
            device = self.ensure_device()
            node = device(text="OK")
            if node.exists(timeout=0.2):
                info = node.info or {}
                bounds = info.get("bounds") or {}
                if isinstance(bounds, dict) and bounds:
                    x = (int(bounds.get("left", 0)) + int(bounds.get("right", 0))) // 2
                    y = (int(bounds.get("top", 0)) + int(bounds.get("bottom", 0))) // 2
                    if x > 0 and y > 0:
                        self._adb_tap(x, y)
                        self._step(f"Tapped OK (u2 bounds) at ({x},{y})", 0.44)
                        return True
                node.click()
                self._step("Tapped OK (u2 click)", 0.44)
                return True
        except Exception:
            pass

        if xml:
            if self.tap_ok_not_cancel(xml):
                self._step("Tapped OK (XML bounds)", 0.44)
                return True

        ok_points: List[Tuple[int, int, int]] = []
        has_cancel = False
        try:
            for el in adb.get_ui_elements(self.serial):
                text = (el.get("text") or el.get("content_desc") or "").strip().upper()
                center = self._fca_bounds_center(el.get("bounds", ""))
                if not center:
                    continue
                x, y = center
                if text == "CANCEL":
                    has_cancel = True
                elif text == "OK":
                    ok_points.append((x, y, x))
        except Exception as exc:
            self._step(f"ADB OK lookup skip: {exc}")

        if ok_points:
            if has_cancel:
                x, y, _ = max(ok_points, key=lambda item: item[0])
            else:
                x, y, _ = ok_points[0]
            self._adb_tap(x, y)
            self._step(f"Tapped OK (ADB {'right' if has_cancel else 'center'}) at ({x},{y})", 0.44)
            return True

        if self.tap_ok_not_cancel(None):
            self._step("Tapped OK (layout fallback)", 0.44)
            return True

        w, h = self._window_size()
        # Center-bottom of typical Prompt Information modal (SGW success has one OK only).
        self._adb_tap(int(w * 0.50), int(h * 0.62))
        self._step("Tapped OK (fixed layout fallback)", 0.44)
        return True

    def _fca_sgw_unlocked_visible(self) -> bool:
        """True when 'Secure Gateway Unlocked Successfully!' prompt is on screen."""
        if self._u2_has_text("Unlocked Successfully", timeout=0.12):
            return True
        if self._u2_has_text("Secure Gateway Unlocked", timeout=0.1):
            return True
        blob = self._fca_adb_ui_blob()
        return "unlocked successfully" in blob or "secure gateway unlocked" in blob

    def _fca_sgw_failed_visible(self) -> bool:
        """True when SGW unlock finished unsuccessfully."""
        if self._u2_has_text("Unlock Failed", "Unlocked Failed", timeout=0.1):
            return True
        if self._u2_has_text("Unlock Unsuccessful", "Failed to Unlock", timeout=0.08):
            return True
        blob = self._fca_adb_ui_blob()
        markers = (
            "unlock failed",
            "unlocked failed",
            "unlock unsuccessful",
            "failed to unlock",
            "unable to unlock",
            "secure gateway unlock failed",
            "not unlocked",
        )
        if any(m in blob for m in markers):
            return True
        # Generic failure on an SGW prompt (not still "unlocking…")
        if "secure gateway" in blob and any(
            m in blob for m in ("fail", "unsuccess", "error", "unable", "not support")
        ):
            if "unlocked successfully" not in blob:
                return True
        return False

    def _fca_sgw_result_visible(self) -> Optional[str]:
        """Return 'success' / 'failed' when SGW finished, else None (still waiting)."""
        if self._fca_sgw_unlocked_visible():
            return "success"
        if self._fca_sgw_failed_visible():
            return "failed"
        return None

    def _fca_sgw_busy(self) -> bool:
        """True while SGW unlock is still in progress (no result dialog yet)."""
        if self._fca_sgw_result_visible():
            return False
        if self._u2_has_text("Secure Gateway", timeout=0.1):
            return True
        blob = self._fca_adb_ui_blob()
        # Only treat as SGW busy when Secure Gateway is actually mentioned —
        # plain "Please Wait…" after Fiat OK is vehicle reading, not SGW.
        if "secure gateway" in blob and "unlocked successfully" not in blob:
            return True
        if "connecting to secure gateway" in blob or "sgw unlock" in blob:
            return True
        if "unlocking" in blob and "secure" in blob:
            return True
        return False

    def _fca_autodetect_visible(self) -> bool:
        if self._u2_has_text("AutoDetect Result", timeout=0.1):
            return True
        if self._u2_has_text("Scan History", timeout=0.08):
            return True
        if self._u2_has_text("Vehicle Information", timeout=0.08):
            return True
        blob = " ".join(self._fca_adb_ui_texts()).lower()
        return self._fca_blob_is_autodetect(blob)

    def _fca_tap_diagnostic(self) -> Dict[str, object]:
        """Tap Diagnostic on AutoDetect — ADB text first, then layout coords."""
        self._step("Tapping Diagnostic…", 0.86)
        try:
            if adb.find_and_tap_text(self.serial, "Diagnostic", timeout=3):
                self._step("Tapped Diagnostic (ADB text)", 0.87)
                return {"ok": True, "method": "adb_text", "point": None}
        except Exception as exc:
            self._step(f"ADB Diagnostic tap skip: {exc}")
        return self.tap_diagnostic()

    def _fca_fiat_confirm_visible(self) -> bool:
        """True when the FIAT vehicle confirm modal (CANCEL/OK) is on screen.

        u2 exact text misses 'Model Name:500BEV' — use ADB dump + contains.
        """
        if self._u2_has_text("CANCEL", timeout=0.1) and self._u2_has_text("OK", timeout=0.08):
            # Prefer ADB to confirm this is the vehicle identity modal, not another dialog.
            blob = self._fca_adb_ui_blob()
            if any(m in blob for m in FIAT_POPUP_MARKERS) or "model name" in blob or "car code" in blob:
                return True
            if "identified wrong vehicle" in blob or "please record" in blob:
                return True
        blob = self._fca_adb_ui_blob()
        return self._fca_is_fiat_vehicle_popup(blob)

    def _fca_screen_hint(self) -> str:
        """Short label of what is currently on screen (for heartbeat logs)."""
        if self._fca_on_system_function():
            return "System and Function / Topology"
        result = self._fca_sgw_result_visible()
        if result == "success":
            return "SGW Unlocked Successfully"
        if result == "failed":
            return "SGW Unlock Failed"
        if self._fca_fiat_confirm_visible():
            return "Fiat vehicle ID popup"
        blob = self._fca_adb_ui_blob()
        if "secure gateway" in blob or ("unlocking" in blob and "secure" in blob):
            return "SGW unlock in progress"
        if self._fca_is_reading_wait(blob):
            return "Please Wait / reading"
        if "autodetect result" in blob:
            return "AutoDetect Result"
        if "prompt information" in blob:
            return "Prompt Information"
        if "continue" in blob:
            return "Continue prompt"
        return "unknown"

    def _fca_advance_from_autodetect(self, info: Dict[str, object]) -> bool:
        """Read VIN/model on AutoDetect, tap Diagnostic (Fiat OK comes after Diagnostic)."""
        if not self._fca_autodetect_visible():
            return False

        texts = self._fca_adb_ui_texts()
        if not texts:
            try:
                texts = self.visible_texts()
            except Exception:
                texts = []

        identity = self._fca_parse_autodetect_fields(texts)
        info["vin"] = identity.get("vin") or None
        info["make"] = identity.get("make") or self.preferred_brand
        info["model"] = identity.get("model") or None

        self._step("AutoDetect Result page confirmed", 0.84)
        if identity.get("vin"):
            self._step(
                f"AutoDetect · VIN {identity['vin']} · "
                f"Make {info['make']} · Model {identity.get('model') or '—'}",
                0.85,
            )

        # Fiat confirm popup appears AFTER Diagnostic — do not OK here.
        self._step("AutoDetect Result — tapping Diagnostic now", 0.86)
        diag = self._fca_tap_diagnostic()
        info["diagnostic_tapped"] = bool(diag.get("ok"))
        info["diagnostic_method"] = diag.get("method")
        info["ok"] = True
        return True

    def _fca_is_autodetect_page(self, xml: str = "") -> bool:
        """Broader FCA AutoDetect recognition than one exact title string."""
        blob = (xml or "").lower()
        if self._fca_blob_is_autodetect(blob):
            return True
        if not blob:
            return self._fca_autodetect_visible()
        return False

    def _fca_texts_from_xml(self, xml: str) -> List[str]:
        if not xml:
            return []
        texts = [t.strip() for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]
        texts.extend(t.strip() for t in re.findall(r'content-desc="([^"]+)"', xml) if t.strip())
        return texts

    def _fca_parse_autodetect_fields(self, texts: List[str]) -> Dict[str, str]:
        """VIN / Make / Model from AutoDetect Result and Fiat confirm popup."""
        fields = self._parse_fields_from_texts(texts)
        blob = " ".join(texts)

        model_name = ""
        model_desc = ""
        year = ""
        m = re.search(r"Model\s+Name\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9 \-/\.]{0,40})", blob, re.I)
        if m:
            model_name = re.sub(r"\s+", " ", m.group(1)).strip(" .:-")
        m = re.search(r"Model\s+Description\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9 \-/\.]{0,40})", blob, re.I)
        if m:
            model_desc = re.sub(r"\s+", " ", m.group(1)).strip(" .:-")
        m = re.search(r"Model\s+Year\s*[:：]\s*(\d{4})", blob, re.I)
        if m:
            year = m.group(1)

        if model_desc:
            fields["model"] = model_desc
        elif model_name:
            fields["model"] = model_name

        if year and fields.get("model") and year not in fields["model"]:
            fields["model"] = f"{fields['model']} ({year})"
        elif year and not fields.get("model"):
            fields["model"] = year

        if not fields.get("make"):
            for t in texts:
                up = t.strip().upper()
                if up in ("FIAT", "JEEP", "CHRYSLER", "ALFA ROMEO", "LANCIA", "ABARTH"):
                    fields["make"] = t.strip()
                    break

        if not fields.get("make"):
            fields["make"] = self.preferred_brand
        return fields

    def _fca_read_autodetect_identity(self) -> Dict[str, str]:
        xml = self._ui_xml(timeout=2.0)
        return self._fca_parse_autodetect_fields(self._fca_texts_from_xml(xml))

    def _fca_apply_identity(self, result: Dict[str, object], fields: Dict[str, str]) -> None:
        vin = fields.get("vin") or ""
        make = fields.get("make") or self.preferred_brand
        model = fields.get("model") or ""
        if vin:
            result["vin"] = vin
            self.detected_vin = vin
        if make:
            result["make"] = make
            self.detected_make = make
        if model:
            result["model"] = model
            self.detected_model = model

    def _fca_save_autodetect_audit(self, result: Dict[str, object]) -> None:
        vin = str(result.get("vin") or "UNKNOWN")
        make = str(result.get("make") or self.preferred_brand)
        model = str(result.get("model") or "")
        save_vin_audit(
            serial=self.serial,
            brand_selected=self.preferred_brand,
            make_detected=make,
            vin=vin,
            software="",
            source="fca_autodetect",
            model=model,
        )
        save_ticket(vin, make, model, "FCA AutoDetect", serial=self.serial)
        self._step(
            f"Identity saved · Make: {make} · Model: {model or '—'} · VIN: {vin}",
            0.35,
        )

    def _fca_wait_autodetect_result(self, timeout: float = 90.0) -> Dict[str, object]:
        """Wait for AutoDetect Result, read VIN/model, then tap Diagnostic."""
        info: Dict[str, object] = {
            "ok": False,
            "vin": None,
            "make": None,
            "model": None,
            "software": "",
            "saw_processing": False,
            "saw_local_diagnose": False,
            "diagnostic_tapped": False,
            "diagnostic_method": None,
            "error": None,
        }
        deadline = time.time() + timeout
        last_heartbeat = 0.0
        last_adb_check = 0.0
        home_retaps = 0
        select_make_taps = 0
        t_start = time.time()
        self._step("Waiting for AutoDetect Result (must see page before Diagnostic)…", 0.6)

        while time.time() < deadline:
            self.raise_if_cancelled()
            remaining = int(deadline - time.time())
            now = time.time()

            if self._fca_advance_from_autodetect(info):
                return info

            if select_make_taps < 3 and self.is_select_make_dialog():
                if self.handle_select_make_dialog():
                    info["saw_processing"] = True
                select_make_taps += 1
                time.sleep(0.2)
                continue

            if self._u2_has_text("Enter the model name", timeout=0.05):
                info["saw_local_diagnose"] = True
                info["error"] = "AutoDetect bounced to Local Diagnose — switching now"
                self._step(info["error"], 0.35)
                return info

            if now - last_adb_check >= 2.0:
                last_adb_check = now
                blob = " ".join(self._fca_adb_ui_texts()).lower()
                if self._fca_blob_is_autodetect(blob):
                    if self._fca_advance_from_autodetect(info):
                        return info

            if not info["saw_processing"]:
                if self._u2_has_text("Connect VCI", timeout=0.05):
                    info["saw_processing"] = True
                    self._step("Processing VIN (Connect VCI / Read VIN)…", 0.68)
                    time.sleep(0.12)
                    continue
                if home_retaps < 1 and (now - t_start) > 0.7:
                    if self._u2_has_text("Intelligent Diagnose", timeout=0.05):
                        self._step("Still on home — one more Intelligent Diagnose tap", 0.62)
                        home_retaps += 1
                        w, h = self._window_size()
                        self._adb_tap(int(w * 0.17), int(h * 0.28))
                time.sleep(0.1)
                continue

            if now - last_heartbeat >= 5:
                self._step(f"Still waiting for AutoDetect Result… {remaining}s left", 0.7)
                last_heartbeat = now
            time.sleep(0.15)

        info["error"] = (
            "Timed out waiting for AutoDetect Result / VIN "
            f"(waited {int(timeout)}s). Check VCI and ignition."
        )
        self._step(info["error"])
        return info

    def start_fca_oil_maintenance_reset(self, brand: Optional[str] = None) -> Dict[str, object]:
        """Full oil service reset for FCA cars (Fiat 500e / SGW)."""
        if brand:
            self.preferred_brand = brand
        result: Dict[str, object] = {
            "ok": False,
            "brand": self.preferred_brand,
            "vin": None,
            "make": self.preferred_brand,
            "model": None,
            "sgw": False,
            "error": None,
            **self.device_stamp(),
        }
        t0 = time.time()
        try:
            self._step(f"FCA oil reset — {self.preferred_brand} (SGW OK dialogs if shown)", 0.02)
            self.raise_if_cancelled()
            self.preflight_device()
            self.ensure_app_open_fast()

            if not self._u2_has_text("AutoDetect Result", timeout=0.12):
                self._step("Tapping Intelligent Diagnose", 0.15)
                self.tap_intelligent_diagnose_fast()

            detect = self._fca_wait_autodetect_result(timeout=90)
            if not detect.get("ok"):
                if detect.get("saw_local_diagnose") or self.is_local_diagnose_page():
                    fb = self.local_diagnose_brand_fallback()
                    if not fb.get("ok"):
                        result["error"] = fb.get("error") or detect.get("error")
                        return result
                    detect = self._fca_wait_autodetect_result(timeout=60)
            if not detect.get("ok"):
                result["error"] = detect.get("error") or "AutoDetect Result not reached"
                return result

            self._fca_apply_identity(result, {
                "vin": str(detect.get("vin") or ""),
                "make": str(detect.get("make") or self.preferred_brand),
                "model": str(detect.get("model") or ""),
            })
            self._fca_save_autodetect_audit(result)

            if not detect.get("diagnostic_tapped"):
                self._fca_tap_diagnostic()

            entered = self._fca_after_diagnostic(timeout=300)
            if not entered.get("ok"):
                result["error"] = entered.get("error") or "Did not reach System and Function"
                return result
            result["sgw"] = bool(entered.get("sgw"))
            if entered.get("vin") and not result.get("vin"):
                self._fca_apply_identity(result, {
                    "vin": str(entered.get("vin") or ""),
                    "make": str(entered.get("make") or self.preferred_brand),
                    "model": str(result.get("model") or ""),
                })
            if entered.get("model") and not result.get("model"):
                result["model"] = entered.get("model")
                self.detected_model = str(entered.get("model"))

            time.sleep(0.4)
            self._step("System and Function — opening Common Special Function sidebar", 0.54)
            if not self._fca_tap_common_special():
                result["error"] = "Could not tap Common Special Function"
                return result
            time.sleep(0.35)

            if not self._fca_tap_oil_maintenance_reset():
                result["error"] = "Could not tap Oil Maintenance Reset"
                return result
            time.sleep(0.3)

            reset = self._fca_oil_ok_continue_until_done(timeout=180)
            if not reset.get("ok"):
                result["error"] = reset.get("error") or "Oil reset did not finish"
                return result

            self._step("Oil reset confirmed — hard reset to EURO LINK home", 0.92)
            self._fca_return_home()

            vin = str(result.get("vin") or self.detected_vin or "UNKNOWN")
            make = str(result.get("make") or self.preferred_brand)
            model = str(result.get("model") or "")
            save_vin_audit(
                serial=self.serial,
                brand_selected=self.preferred_brand,
                make_detected=make,
                vin=vin,
                software="",
                source="fca_oil_reset",
                model=model,
            )
            save_ticket(vin, make, model, "FCA Oil Maintenance Reset", serial=self.serial)
            save_report(
                self.serial,
                "fca_oil_maintenance_reset",
                "completed",
                f"brand={self.preferred_brand}; make={make}; model={model}; vin={vin}; sgw={result['sgw']}",
                None,
            )
            elapsed = round(time.time() - t0, 2)
            result["ok"] = True
            result["vin"] = vin
            result["model"] = model
            self._step(f"FCA oil reset complete · {elapsed}s — home after hard reset", 1.0)
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"FCA oil reset error: {exc}")
        return result

    def _fca_popup_visible(self, xml: str = "", blob: str = "") -> bool:
        low = blob or (xml.lower() if xml else "")
        if not low:
            try:
                low = " ".join(self.visible_texts()).lower()
            except Exception:
                low = ""
        if not low:
            low = self._fca_adb_ui_blob()
        if self._fca_is_fiat_vehicle_popup(low):
            return True
        if not low:
            if self._u2_has_text("CANCEL", timeout=0.05) or self._u2_has_text("Prompt Information", timeout=0.05):
                return True
            if self._u2_has_text("Secure Gateway", timeout=0.05):
                return True
            return False
        return any(m in low for m in POPUP_MARKERS)

    def _fca_on_system_function(self) -> bool:
        """True on System and Function / Topology (sidebar + Smart Detection footer)."""
        if self._u2_has_text("Common Special Function", timeout=0.08):
            return True
        if self._u2_has_text("System and Function", timeout=0.08):
            return True
        if self._u2_has_text("System Topology", timeout=0.08):
            return True
        if self._u2_has_text("Smart Detection", timeout=0.08):
            return True
        blob = self._fca_adb_ui_blob()
        if "common special function" in blob:
            return True
        if "system and function" in blob:
            return True
        if "system topology" in blob and (
            "smart detection" in blob or "common special" in blob
        ):
            return True
        return False

    def _fca_is_reading_wait(self, blob: str = "") -> bool:
        """True on 'Please Wait…' reading progress after Fiat OK (no OK button yet)."""
        low = (blob or self._fca_adb_ui_blob()).lower()
        if "secure gateway" in low:
            return False
        if "please wait" in low:
            return True
        if "prompt information" in low and "ok" not in low and "cancel" not in low:
            return True
        return False

    def _fca_after_diagnostic(self, timeout: float = 300.0) -> Dict[str, object]:
        """After Diagnostic:

        1. Wait for Fiat vehicle ID popup → tap OK
        2. Wait while Please Wait… reading runs
        3. If SGW appears → wait Success/Fail → OK
        4. Land on System and Function / Topology → continue oil-reset flow
        """
        out: Dict[str, object] = {
            "ok": False,
            "sgw": False,
            "vin": None,
            "make": None,
            "model": None,
            "error": None,
        }
        self._step(
            "After Diagnostic — wait Fiat ID → OK → reading → Topology "
            "(or SGW Success/Fail if shown)…",
            0.4,
        )
        soft_deadline = time.time() + max(90.0, float(timeout))
        hard_deadline = time.time() + max(360.0, float(timeout) + 60.0)
        last_hb = 0.0
        last_adb = 0.0
        diag_retry = 0
        fiat_confirmed = False
        sgw_ok_tapped = False
        sgw_started = False
        screen_blob = ""
        t_start = time.time()

        while time.time() < min(soft_deadline, hard_deadline):
            self.raise_if_cancelled()
            remaining = int(min(soft_deadline, hard_deadline) - time.time())
            now = time.time()

            if now - last_adb >= 0.7:
                last_adb = now
                screen_blob = self._fca_adb_ui_blob()

            # 1) Destination: System and Function / Topology
            if self._fca_on_system_function():
                texts = self._fca_adb_ui_texts()
                fields = self._fca_parse_autodetect_fields(texts)
                if fields.get("vin") and not out.get("vin"):
                    out["vin"] = fields["vin"]
                if fields.get("model") and not out.get("model"):
                    out["model"] = fields["model"]
                if fields.get("make") and not out.get("make"):
                    out["make"] = fields["make"]
                path = "Topology (no SGW)" if not out.get("sgw") else "Topology after SGW"
                self._step(f"System and Function / Topology reached — {path}", 0.52)
                out["ok"] = True
                out["make"] = out.get("make") or self.preferred_brand
                return out

            # 2) SGW finished — Success OR Fail → tap OK once
            sgw_result = None
            if (
                "unlocked successfully" in screen_blob
                or "secure gateway unlocked" in screen_blob
            ):
                sgw_result = "success"
            elif any(
                m in screen_blob
                for m in (
                    "unlock failed",
                    "unlocked failed",
                    "unlock unsuccessful",
                    "failed to unlock",
                    "unable to unlock",
                )
            ):
                sgw_result = "failed"
            elif "secure gateway" in screen_blob:
                sgw_result = self._fca_sgw_result_visible()

            if sgw_result:
                out["sgw"] = True
                sgw_started = True
                soft_deadline = max(soft_deadline, time.time() + 90.0)
                if not sgw_ok_tapped:
                    label = (
                        "Secure Gateway Unlocked Successfully!"
                        if sgw_result == "success"
                        else "Secure Gateway Unlock Failed / Unsuccessful"
                    )
                    self._step(f"{label} — tapping OK", 0.48)
                    self._fca_tap_confirm_ok()
                    sgw_ok_tapped = True
                    time.sleep(0.7)
                    last_adb = 0.0
                else:
                    time.sleep(0.25)
                continue

            # 3) Fiat vehicle ID popup (pic 1) → OK once
            fiat_visible = (
                self._fca_is_fiat_vehicle_popup(screen_blob)
                if screen_blob
                else self._fca_fiat_confirm_visible()
            )
            if not fiat_confirmed and fiat_visible:
                texts = self._fca_adb_ui_texts()
                popup_id = self._fca_parse_autodetect_fields(texts)
                if popup_id.get("vin") and not out.get("vin"):
                    out["vin"] = popup_id["vin"]
                if popup_id.get("model") and not out.get("model"):
                    out["model"] = popup_id["model"]
                if popup_id.get("make") and not out.get("make"):
                    out["make"] = popup_id["make"]
                model_label = popup_id.get("model") or popup_id.get("vin") or "vehicle"
                self._step(f"Fiat vehicle ID ({model_label}) — tapping OK", 0.42)
                self._fca_tap_confirm_ok()
                fiat_confirmed = True
                soft_deadline = max(soft_deadline, time.time() + 180.0)
                self._step(
                    "Fiat OK — waiting for reading / Topology "
                    "(or SGW if this car has Secure Gateway)…",
                    0.44,
                )
                time.sleep(0.55)
                last_adb = 0.0
                continue

            if (
                not fiat_confirmed
                and self._u2_has_text("CANCEL", timeout=0.1)
                and self._u2_has_text("OK", timeout=0.08)
            ):
                screen_blob = self._fca_adb_ui_blob()
                last_adb = now
                if self._fca_is_fiat_vehicle_popup(screen_blob) or any(
                    m in screen_blob for m in ("model name", "car code", "please record")
                ):
                    self._step("Fiat vehicle ID (CANCEL/OK) — tapping OK", 0.42)
                    self._fca_tap_confirm_ok()
                    fiat_confirmed = True
                    soft_deadline = max(soft_deadline, time.time() + 180.0)
                    time.sleep(0.55)
                    last_adb = 0.0
                    continue

            # 4) Please Wait… reading (pic 2) — no SGW yet; do not tap, just wait
            if fiat_confirmed and self._fca_is_reading_wait(screen_blob):
                soft_deadline = max(soft_deadline, time.time() + 90.0)
                if now - last_hb >= 5:
                    elapsed = int(now - t_start)
                    self._step(
                        f"Reading vehicle (Please Wait…)… {elapsed}s — "
                        "next: Topology or SGW",
                        0.45,
                    )
                    last_hb = now
                time.sleep(0.35)
                continue

            # 5) SGW still unlocking — wait for Success/Fail (do not spam OK)
            sgw_busy = (
                "secure gateway" in screen_blob
                and "unlocked successfully" not in screen_blob
            ) or (
                fiat_confirmed
                and not sgw_ok_tapped
                and self._fca_sgw_busy()
            )
            if fiat_confirmed and sgw_busy and not sgw_ok_tapped:
                out["sgw"] = True
                sgw_started = True
                soft_deadline = max(soft_deadline, time.time() + 120.0)
                if now - last_hb >= 6:
                    elapsed = int(now - t_start)
                    self._step(
                        f"SGW unlocking… waiting for Success/Fail "
                        f"({elapsed}s elapsed, {remaining}s soft left)",
                        0.46,
                    )
                    last_hb = now
                time.sleep(0.4)
                continue

            # 6) Other Prompt Information with OK (after Fiat OK, not reading-only)
            if (
                fiat_confirmed
                and "prompt information" in screen_blob
                and "ok" in screen_blob
                and not self._fca_is_reading_wait(screen_blob)
            ):
                if sgw_busy and not sgw_ok_tapped:
                    time.sleep(0.35)
                    continue
                self._step("Prompt Information — tapping OK", 0.45)
                self._fca_tap_confirm_ok()
                time.sleep(0.5)
                last_adb = 0.0
                continue

            # 7) Still on AutoDetect without Fiat modal — retry Diagnostic
            if (
                not fiat_confirmed
                and diag_retry < 2
                and "autodetect result" in screen_blob
                and not self._fca_is_fiat_vehicle_popup(screen_blob)
            ):
                self._step("Still on AutoDetect — tapping Diagnostic again", 0.38)
                self._fca_tap_diagnostic()
                diag_retry += 1
                time.sleep(0.5)
                last_adb = 0.0
                continue

            if now - last_hb >= 6:
                hint = self._fca_screen_hint()
                if "SGW Unlocked" in hint or "SGW Unlock Failed" in hint:
                    last_adb = 0.0
                    screen_blob = self._fca_adb_ui_blob()
                    last_hb = now
                    continue
                if "Please Wait" in hint or "reading" in hint.lower():
                    soft_deadline = max(soft_deadline, time.time() + 60.0)
                self._step(
                    f"Watching screen ({hint})… {remaining}s left",
                    0.45,
                )
                last_hb = now
            time.sleep(0.3)

        waited = int(time.time() - t_start)
        if sgw_started and not sgw_ok_tapped:
            out["error"] = (
                f"Timed out waiting for SGW Success/Fail after {waited}s "
                "(unlock still in progress or result not recognized)"
            )
        else:
            out["error"] = (
                "Timed out waiting for System and Function / Topology "
                f"after Fiat ID / reading (waited {waited}s)"
            )
        self._step(out["error"])
        return out

    def _fca_on_special_function_grid(self) -> bool:
        """True when Common Special Function grid (Oil Maintenance Reset, etc.) is open."""
        if self._u2_has_text("Oil Maintenance Reset", timeout=0.1):
            return True
        blob = self._fca_adb_ui_blob()
        return any(
            marker in blob
            for marker in (
                "oil maintenance reset",
                "steering angle reset",
                "brake reset",
                "battery registration",
            )
        )

    def _fca_find_sidebar_label(
        self, needle: str, *, exact: bool = False
    ) -> Optional[Tuple[int, int, int, int, str]]:
        """Find a sidebar label on System and Function (left column only)."""
        want = needle.strip().lower()
        w, h = self._window_size()
        max_x = int(w * 0.40)
        min_y = int(h * 0.10)
        max_y = int(h * 0.86)
        found: List[Tuple[int, int, int, int, int, str]] = []
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                if not raw:
                    continue
                low = raw.lower()
                if exact:
                    if low != want:
                        continue
                elif want not in low:
                    continue
                bounds = el.get("bounds") or ""
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                cx, cy = (l + r) // 2, (t + b) // 2
                if cx > max_x or cy < min_y or cy > max_y:
                    continue
                width, height = r - l, b - t
                if width < 30 or height < 12:
                    continue
                found.append((width * height, l, t, r, b, raw))
        except Exception:
            return None
        if not found:
            return None
        found.sort(key=lambda item: item[0], reverse=True)
        _area, l, t, r, b, name = found[0]
        return (l, t, r, b, name)

    def _fca_find_grid_label(
        self, needle: str, *, exact: bool = False
    ) -> Optional[Tuple[int, int, int, int, str]]:
        """Find a label in the main function grid (center/right area)."""
        want = needle.strip().lower()
        w, h = self._window_size()
        min_x = int(w * 0.12)
        min_y = int(h * 0.08)
        max_y = int(h * 0.78)
        found: List[Tuple[int, int, int, int, int, str]] = []
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                if not raw:
                    continue
                low = raw.lower()
                if exact:
                    if low != want:
                        continue
                elif want not in low:
                    continue
                bounds = el.get("bounds") or ""
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                cx, cy = (l + r) // 2, (t + b) // 2
                if cx < min_x or cy < min_y or cy > max_y:
                    continue
                width, height = r - l, b - t
                if width < 30 or height < 12:
                    continue
                found.append((width * height, l, t, r, b, raw))
        except Exception:
            return None
        if not found:
            return None
        found.sort(key=lambda item: item[0], reverse=True)
        _area, l, t, r, b, name = found[0]
        return (l, t, r, b, name)

    def _fca_tap_common_special(self) -> bool:
        """Tap Common Special Function in the left sidebar on System and Function."""
        self._step("Looking for Common Special Function in sidebar…", 0.55)
        deadline = time.time() + 22.0
        w, h = self._window_size()
        layout_points = (
            (int(w * 0.14), int(h * 0.52)),
            (int(w * 0.14), int(h * 0.56)),
            (int(w * 0.12), int(h * 0.54)),
            (int(w * 0.16), int(h * 0.53)),
        )
        layout_idx = 0
        last_hb = 0.0

        while time.time() < deadline:
            self.raise_if_cancelled()

            if self._fca_on_special_function_grid():
                self._step("Common Special Function grid open", 0.56)
                return True

            for label in ("Common Special Function", "Common Special"):
                box = self._fca_find_sidebar_label(label)
                if box:
                    l, t, r, b, name = box
                    x, y = (l + r) // 2, (t + b) // 2
                    self._adb_tap(x, y)
                    self._step(f"Tapped sidebar '{name}' at ({x},{y})", 0.56)
                    time.sleep(0.5)
                    if self._fca_on_special_function_grid():
                        return True
                    break

            xml = self._ui_xml(timeout=1.4)
            if xml:
                hit = self._tap_label_from_xml(
                    xml,
                    ("Common Special Function", "Common Special"),
                    min_y_ratio=0.10,
                    max_x_ratio=0.42,
                )
                if hit:
                    self._step(f"Tapped Common Special Function (XML) at {hit[1:]}", 0.56)
                    time.sleep(0.5)
                    if self._fca_on_special_function_grid():
                        return True

            try:
                if adb.find_and_tap_text(self.serial, "Common Special Function", timeout=2):
                    self._step("Tapped Common Special Function (ADB text)", 0.56)
                    time.sleep(0.5)
                    if self._fca_on_special_function_grid():
                        return True
            except Exception:
                pass

            try:
                device = self.ensure_device()
                node = device(textContains="Common Special")
                if node.exists(timeout=0.12):
                    node.click()
                    self._step("Tapped Common Special Function (u2)", 0.56)
                    time.sleep(0.5)
                    if self._fca_on_special_function_grid():
                        return True
            except Exception:
                pass

            if layout_idx < len(layout_points):
                x, y = layout_points[layout_idx]
                layout_idx += 1
                self._adb_tap(x, y)
                self._step(f"Tapped sidebar layout fallback at ({x},{y})", 0.555)
                time.sleep(0.5)
                if self._fca_on_special_function_grid():
                    return True

            now = time.time()
            if now - last_hb >= 5:
                self._step("Still on System Topology — retrying Common Special Function…", 0.54)
                last_hb = now
            time.sleep(0.2)

        self._step("Could not open Common Special Function grid", 0.55)
        return False

    def _fca_find_exact_text_bounds(
        self, xml: str, needle: str
    ) -> Optional[Tuple[int, int, int, int, str]]:
        """Exact label match only (not substring). Returns left,top,right,bottom,text."""
        if not xml:
            return None
        want = needle.strip().lower()
        found: list[Tuple[int, int, int, int, int, str]] = []
        for tag in re.findall(r"<node\b[^>]*>", xml):
            text_m = re.search(r'text="([^"]*)"', tag)
            desc_m = re.search(r'content-desc="([^"]*)"', tag)
            bounds = re.search(r"bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"", tag)
            if not bounds:
                continue
            raw = (text_m.group(1) if text_m else "").strip()
            desc = (desc_m.group(1) if desc_m else "").strip()
            if raw.lower() != want and desc.lower() != want:
                continue
            l, t, r, b = (int(bounds.group(i)) for i in range(1, 5))
            width, height = r - l, b - t
            if width < 40 or height < 14:
                continue
            found.append((width * height, l, t, r, b, raw or desc))
        if not found:
            return None
        found.sort(key=lambda item: item[0])
        _area, l, t, r, b, name = found[0]
        return (l, t, r, b, name)

    def _fca_tap_left_of_bounds(self, l: int, t: int, r: int, b: int) -> Tuple[int, int]:
        """Tap the LEFT side of a cell so a full-row node does not hit the right column."""
        x = l + max(18, (r - l) // 5)
        y = (t + b) // 2
        self._adb_tap(x, y)
        return (x, y)

    def _fca_tap_oil_maintenance_reset(self) -> bool:
        """Tap only the exact 'Oil Maintenance Reset' cell — never sibling functions."""
        self._step("Reading grid for exact 'Oil Maintenance Reset'…", 0.62)
        deadline = time.time() + 16.0
        w, h = self._window_size()
        while time.time() < deadline:
            self.raise_if_cancelled()

            box = self._fca_find_grid_label("Oil Maintenance Reset")
            if box:
                l, t, r, b, name = box
                x, y = self._fca_tap_left_of_bounds(l, t, r, b)
                self._step(
                    f"Tapped exact '{name}' left-of-cell at ({x},{y}) bounds=[{l},{t}][{r},{b}]",
                    0.66,
                )
                return True

            xml = self._ui_xml(timeout=2.0)
            box = self._fca_find_exact_text_bounds(xml, "Oil Maintenance Reset")
            if box:
                l, t, r, b, name = box
                x, y = self._fca_tap_left_of_bounds(l, t, r, b)
                self._step(f"Tapped exact '{name}' left-of-cell at ({x},{y}) bounds=[{l},{t}][{r},{b}]", 0.66)
                return True
            blob = (xml or "").lower()
            on_grid = "steering angle reset" in blob or "brake reset" in blob
            if on_grid:
                # Left column, first row of the two-column grid (not center / right).
                x, y = int(w * 0.22), int(h * 0.24)
                self._adb_tap(x, y)
                self._step(
                    f"Exact text missing in dump — tapping top-left grid cell only ({x},{y})",
                    0.65,
                )
                return True
            if self._u2_has_text("System Topology", timeout=0.05):
                self._step("Still on Topology — tapping Common Special Function again", 0.56)
                self._fca_tap_common_special()
            time.sleep(0.2)
        self._step("Oil Maintenance Reset not found on grid", 0.65)
        return False

    # ------------------------------------------------------------------
    # ECU System Scan page (after Oil Maintenance Reset tap)
    # ------------------------------------------------------------------

    ECU_KEYWORDS = (
        "instrument panel cluster",
        "instrument panel (ipc)",
        "engine control module",
        "engine control module (ecm)",
        "ipc",
        "ecm",
        "bcm",
        "tcm",
        "abs",
        "tpms",
    )

    def _fca_is_system_scan_page(self, blob: str = "") -> bool:
        """True when on the ECU list page (System Scan / System Name / State)."""
        low = blob or self._fca_adb_ui_blob()
        if "system scan" in low and "system name" in low:
            return True
        if "system name" in low and "state" in low and "equipped" in low:
            return True
        return False

    def _fca_collect_ecus(self) -> List[str]:
        """Return ECU display names from the System Scan table rows."""
        ecus: List[str] = []
        seen: set[str] = set()
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or "").strip()
                if not raw or len(raw) < 3:
                    continue
                low = raw.lower()
                if low in ("system name", "state", "equipped", "not equipped",
                           "system scan", "text correction", "oil maintenance reset"):
                    continue
                if "equipped" in low or "not configured" in low:
                    continue
                if any(kw in low for kw in self.ECU_KEYWORDS) or (
                    re.match(r"^[A-Z][\w\s()/-]{4,50}$", raw) and "(" in raw
                ):
                    canon = low.strip()
                    if canon not in seen:
                        seen.add(canon)
                        ecus.append(raw)
        except Exception:
            pass

        if not ecus:
            xml = self._ui_xml(timeout=2.0)
            for tag in re.findall(r"<node\b[^>]*>", xml or ""):
                text_m = re.search(r'text="([^"]*)"', tag)
                if not text_m:
                    continue
                raw = text_m.group(1).strip()
                low = raw.lower()
                if any(kw in low for kw in self.ECU_KEYWORDS):
                    canon = low.strip()
                    if canon not in seen:
                        seen.add(canon)
                        ecus.append(raw)
        return ecus

    def _fca_tap_ecu_row(self, ecu_name: str) -> bool:
        """Tap an ECU row by its display name on the System Scan page."""
        try:
            if adb.find_and_tap_text(self.serial, ecu_name, timeout=3):
                self._step(f"Tapped ECU row '{ecu_name}' (ADB text)", 0.72)
                return True
        except Exception:
            pass

        xml = self._ui_xml(timeout=1.5)
        box = self._fca_find_exact_text_bounds(xml, ecu_name)
        if box:
            l, t, r, b, name = box
            x, y = self._fca_tap_left_of_bounds(l, t, r, b)
            self._step(f"Tapped ECU row '{name}' at ({x},{y})", 0.72)
            return True

        hit = self._tap_label_from_xml(xml, (ecu_name,), min_y_ratio=0.08)
        if hit:
            self._step(f"Tapped ECU row '{hit[0]}' at {hit[1:]}", 0.72)
            return True

        self._step(f"Could not find ECU row '{ecu_name}'")
        return False

    def _fca_is_show_menu_page(self, blob: str = "") -> bool:
        """True when already inside an ECU Show Menu (single-ECU auto-enter)."""
        low = blob or self._fca_adb_ui_blob()
        if "reset service information" in low and "last service date" in low:
            return True
        if "show menu" in low and "reset service information" in low:
            return True
        return False

    def _fca_wait_system_scan_page(self, timeout: float = 30.0) -> bool:
        """Wait until we're back on the System Scan ECU list."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.raise_if_cancelled()
            blob = self._fca_adb_ui_blob()
            if self._fca_is_system_scan_page(blob):
                return True
            if self._u2_has_text("System Name", timeout=0.1) and self._u2_has_text("State", timeout=0.08):
                return True
            time.sleep(0.3)
        return False

    def _fca_go_back(self) -> None:
        """Press Android Back."""
        try:
            self.ensure_device().press("back")
        except Exception:
            adb.tap(self.serial, 0, 0)
            import subprocess
            subprocess.run(
                ["adb", "-s", self.serial, "shell", "input", "keyevent", "4"],
                timeout=5, capture_output=True,
            )
        time.sleep(0.4)

    def _fca_single_ecu_reset(
        self,
        ecu_name: str,
        timeout: float = 120.0,
        *,
        already_inside: bool = False,
    ) -> Dict[str, object]:
        """Perform oil reset on one ECU through Show Menu → reset → done.

        already_inside=True: tool auto-entered the only equipped ECU (e.g. IPC only)
        and we are already on Show Menu — skip tapping the ECU row.
        """
        out: Dict[str, object] = {"ok": False, "ecu": ecu_name, "error": None}
        self._step(
            f"ECU reset: {ecu_name}"
            + (" (already inside Show Menu — single ECU)" if already_inside else ""),
            0.70,
        )

        if not already_inside:
            if not self._fca_tap_ecu_row(ecu_name):
                out["error"] = f"Could not tap ECU '{ecu_name}'"
                return out
            time.sleep(0.8)

        deadline = time.time() + timeout
        last_hb = 0.0
        saw_done = False
        tapped_reset_service = False

        while time.time() < deadline:
            self.raise_if_cancelled()
            now = time.time()
            blob = self._fca_adb_ui_blob()

            # Check for final success message
            if RESET_DONE in blob or self._u2_has_text(
                "Service Information Have Been Reset", timeout=0.1
            ):
                saw_done = True
                self._step(f"'{ecu_name}' — Service Information Have Been Reset → OK", 0.85)
                self._fca_tap_confirm_ok()
                time.sleep(0.6)
                if already_inside:
                    # Single-ECU path: no System Scan list to return to
                    out["ok"] = True
                    self._step(f"ECU '{ecu_name}' reset complete (single-ECU path)", 0.86)
                    return out
                continue

            # Procedure End
            if "procedure end" in blob or self._u2_has_text("Procedure End", timeout=0.08):
                saw_done = True
                self._step(f"'{ecu_name}' — Procedure End → tapping Back", 0.84)
                self._fca_tap_button("", "Back")
                time.sleep(0.6)
                if already_inside:
                    out["ok"] = True
                    self._step(f"ECU '{ecu_name}' reset complete (Procedure End)", 0.86)
                    return out
                continue

            # Back on ECU list (multi-ECU path only)
            if self._fca_is_system_scan_page(blob) or (
                self._u2_has_text("System Name", timeout=0.08)
                and self._u2_has_text("Equipped", timeout=0.06)
            ):
                if already_inside:
                    # Unexpected list after single-ECU auto-enter — treat as done if reset finished
                    if saw_done:
                        out["ok"] = True
                        return out
                    time.sleep(0.3)
                    continue
                if saw_done:
                    self._step(f"ECU '{ecu_name}' reset complete — back on ECU list", 0.82)
                    out["ok"] = True
                    return out
                self._step(f"Back on ECU list unexpectedly — retapping '{ecu_name}'", 0.71)
                self._fca_tap_ecu_row(ecu_name)
                time.sleep(0.8)
                continue

            # Turn Ignition Key
            if "turn ignition key" in blob or self._u2_has_text("Turn Ignition Key", timeout=0.08):
                action = "OFF" if "off" in blob else ("ON" if " on" in blob or "key on" in blob else "?")
                self._step(
                    f"'{ecu_name}' — Turn Ignition Key {action} (notify engineer) → Continue",
                    0.79,
                )
                self._fca_tap_button("", "Continue")
                time.sleep(0.6)
                continue

            # Continue button
            if "continue" in blob or self._u2_has_text("Continue", timeout=0.1):
                self._step(f"'{ecu_name}' — tapping Continue", 0.78)
                self._fca_tap_button("", "Continue")
                time.sleep(0.5)
                continue

            # Show Menu: Reset Service Information + Last Service Date
            on_show_menu = self._fca_is_show_menu_page(blob) or (
                "last service date" in blob and "reset service information" in blob
            )
            if not tapped_reset_service and on_show_menu:
                self._step(f"'{ecu_name}' — Show Menu → tapping Reset Service Information", 0.73)
                tapped_reset_service = True
                try:
                    if adb.find_and_tap_text(self.serial, "Reset Service Information", timeout=3):
                        self._step(f"Tapped 'Reset Service Information' for {ecu_name}", 0.74)
                        time.sleep(0.6)
                        continue
                except Exception:
                    pass
                xml = self._ui_xml(timeout=1.2)
                hit = self._tap_label_from_xml(xml, ("Reset Service Information",), min_y_ratio=0.08)
                if hit:
                    self._step(f"Tapped 'Reset Service Information' (XML) for {ecu_name}", 0.74)
                    time.sleep(0.6)
                    continue

            # Oil Change DTC warning popup
            if "oil change" in blob and ("cancel" in blob or "dtc" in blob or "p1230" in blob):
                self._step(f"'{ecu_name}' — Oil Change warning → OK", 0.76)
                self._fca_tap_confirm_ok()
                time.sleep(0.6)
                continue

            # Generic OK/CANCEL popup (only if no Continue)
            if "cancel" in blob and "ok" in blob and "continue" not in blob:
                self._step(f"'{ecu_name}' — popup OK/CANCEL → tapping OK", 0.75)
                self._fca_tap_confirm_ok()
                time.sleep(0.6)
                continue

            if saw_done and ("back" in blob or self._u2_has_text("Back", timeout=0.08)):
                self._step(f"'{ecu_name}' — tapping Back", 0.82)
                self._fca_tap_button("", "Back")
                time.sleep(0.5)
                if already_inside:
                    out["ok"] = True
                    return out
                continue

            if self._u2_has_text("OK", timeout=0.1):
                self._fca_tap_confirm_ok()
                self._step(f"'{ecu_name}' — tapped OK", 0.75)
                time.sleep(0.5)
                continue

            if now - last_hb >= 5:
                remaining = int(deadline - now)
                self._step(f"'{ecu_name}' reset in progress… {remaining}s left", 0.74)
                last_hb = now
            time.sleep(0.3)

        if saw_done:
            out["ok"] = True
            self._step(f"ECU '{ecu_name}' reset done", 0.83)
            if not already_inside:
                self._fca_go_back()
                time.sleep(0.5)
            return out
        out["error"] = f"Timed out resetting ECU '{ecu_name}'"
        self._step(out["error"])
        return out

    def _fca_tap_button(self, blob: str, label: str) -> bool:
        """Tap a full-width bottom button (Continue / Back / OK) by label."""
        try:
            device = self.ensure_device()
            node = device(text=label)
            if node.exists(timeout=0.15):
                node.click()
                return True
        except Exception:
            pass
        try:
            if adb.find_and_tap_text(self.serial, label, timeout=2):
                return True
        except Exception:
            pass
        xml = self._ui_xml(timeout=1.0)
        hit = self._tap_label_from_xml(xml, (label,), min_y_ratio=0.15)
        if hit:
            return True
        w, h = self._window_size()
        self._adb_tap(int(w * 0.50), int(h * 0.82))
        return True

    def _fca_oil_ok_continue_until_done(self, timeout: float = 180.0) -> Dict[str, object]:
        """Oil reset after Oil Maintenance Reset tap.

        Two layouts:
        - Multi-ECU: System Scan list (IPC, ECM, …) → reset each, Back between them
        - Single-ECU: tool auto-enters the only equipped ECU Show Menu → reset that one
        """
        out: Dict[str, object] = {"ok": False, "error": None, "ecus_done": [], "ecus_failed": []}
        self._step(
            "Oil Maintenance Reset — waiting for ECU list or single-ECU Show Menu…",
            0.68,
        )

        deadline_scan = time.time() + 45.0
        on_scan_page = False
        on_show_menu = False

        while time.time() < deadline_scan:
            self.raise_if_cancelled()
            blob = self._fca_adb_ui_blob()

            # Single ECU only → tool opens Show Menu directly (IPC)
            if self._fca_is_show_menu_page(blob):
                on_show_menu = True
                self._step(
                    "Single ECU path — Show Menu opened (no System Scan list)",
                    0.70,
                )
                break

            if self._fca_is_system_scan_page(blob):
                on_scan_page = True
                break

            if RESET_DONE in blob:
                self._step("Service Information Have Been Reset — tapping OK", 0.88)
                self._fca_tap_confirm_ok()
                time.sleep(0.25)
                out["ok"] = True
                return out

            # ECU scan / connect prompts while discovering equipped modules
            if "continue" in blob:
                self._step("Tapped Continue (ECU scan / prompt)", 0.69)
                self._fca_tap_button(blob, "Continue")
                time.sleep(0.4)
                continue

            if "cancel" in blob and "ok" in blob:
                self._step("Tapped OK on prompt (while waiting for ECU page)", 0.69)
                self._fca_tap_confirm_ok()
                time.sleep(0.35)
                continue

            if self._u2_has_text("OK", timeout=0.08):
                self._fca_tap_confirm_ok()
                time.sleep(0.3)
                continue

            time.sleep(0.35)

        # ── Single-ECU auto-enter (Show Menu already open) ─────────
        if on_show_menu:
            ecu_label = "Instrument Panel Cluster (IPC)"
            blob = self._fca_adb_ui_blob()
            if "instrument pan" in blob or "ipc" in blob:
                ecu_label = "Instrument Panel Cluster (IPC)"
            elif "engine control" in blob or "ecm" in blob:
                ecu_label = "Engine Control Module (ECM)"
            result = self._fca_single_ecu_reset(
                ecu_label, timeout=120.0, already_inside=True
            )
            if result.get("ok"):
                out["ok"] = True
                out["ecus_done"].append(ecu_label)
                self._step(f"Single-ECU reset done ({ecu_label})", 0.90)
            else:
                out["error"] = result.get("error") or "Single-ECU reset failed"
                out["ecus_failed"].append(ecu_label)
                self._step(out["error"])
            return out

        if not on_scan_page:
            blob = self._fca_adb_ui_blob()
            if RESET_DONE in blob:
                self._fca_tap_confirm_ok()
                out["ok"] = True
                return out
            if self._fca_is_show_menu_page(blob):
                result = self._fca_single_ecu_reset(
                    "Instrument Panel Cluster (IPC)",
                    timeout=120.0,
                    already_inside=True,
                )
                out["ok"] = bool(result.get("ok"))
                if out["ok"]:
                    out["ecus_done"].append("Instrument Panel Cluster (IPC)")
                else:
                    out["error"] = result.get("error")
                return out
            out["error"] = "Did not reach System Scan ECU list or single-ECU Show Menu"
            self._step(out["error"])
            return out

        ecus = self._fca_collect_ecus()
        if not ecus:
            self._step("No ECUs found on System Scan — trying direct OK/Continue flow", 0.70)
            return self._fca_direct_ok_continue_loop(timeout=120.0)

        # One row on System Scan → same as single-ECU path after tap
        ipc_first = sorted(ecus, key=lambda e: (0 if "ipc" in e.lower() else 1))
        ecus = ipc_first
        self._step(f"Found {len(ecus)} ECU(s) to reset: {', '.join(ecus)}", 0.70)

        if len(ecus) == 1:
            self._step("Only 1 equipped ECU — reset that one then finish", 0.71)

        for idx, ecu in enumerate(ecus):
            self.raise_if_cancelled()
            progress = 0.70 + (0.18 * (idx / max(len(ecus), 1)))
            self._step(f"Resetting ECU {idx + 1}/{len(ecus)}: {ecu}", progress)

            if not self._fca_wait_system_scan_page(timeout=15):
                # Maybe already auto-entered Show Menu for the only ECU
                if self._fca_is_show_menu_page():
                    result = self._fca_single_ecu_reset(
                        ecu, timeout=120.0, already_inside=True
                    )
                    if result.get("ok"):
                        out["ecus_done"].append(ecu)
                        out["ok"] = True
                        self._step(f"ECU '{ecu}' ✓ reset done (auto-entered)", progress + 0.05)
                        return out
                self._step(f"Not on ECU list before '{ecu}' — pressing Back", 0.71)
                self._fca_go_back()
                time.sleep(0.5)
                if not self._fca_wait_system_scan_page(timeout=10):
                    if self._fca_is_show_menu_page():
                        result = self._fca_single_ecu_reset(
                            ecu, timeout=120.0, already_inside=True
                        )
                        if result.get("ok"):
                            out["ecus_done"].append(ecu)
                            out["ok"] = True
                            return out
                    self._step(f"Cannot return to ECU list for '{ecu}'")
                    out["ecus_failed"].append(ecu)
                    continue

            result = self._fca_single_ecu_reset(ecu, timeout=120.0)
            if result.get("ok"):
                out["ecus_done"].append(ecu)
                self._step(f"ECU '{ecu}' ✓ reset done", progress + 0.05)
            else:
                out["ecus_failed"].append(ecu)
                self._step(f"ECU '{ecu}' ✗ {result.get('error')}", progress)
                self._fca_go_back()
                time.sleep(0.5)

            # After a single-ECU list, no need to wait for more rows
            if len(ecus) == 1 and out["ecus_done"]:
                break

        if out["ecus_done"]:
            out["ok"] = True
            self._step(
                f"All ECU resets attempted — done: {len(out['ecus_done'])}, "
                f"failed: {len(out['ecus_failed'])}",
                0.90,
            )
        else:
            out["error"] = "No ECU resets succeeded"
            self._step(out["error"])
        return out

    def _fca_direct_ok_continue_loop(self, timeout: float = 120.0) -> Dict[str, object]:
        """Fallback: no ECU list detected, just OK/Continue until done."""
        out: Dict[str, object] = {"ok": False, "error": None}
        self._step("Direct OK/Continue loop (no ECU list)", 0.72)
        deadline = time.time() + timeout
        last_hb = 0.0
        while time.time() < deadline:
            self.raise_if_cancelled()
            blob = self._fca_adb_ui_blob()

            if RESET_DONE in blob:
                self._step("Service Information Have Been Reset — tapping OK", 0.88)
                self._fca_tap_confirm_ok()
                time.sleep(0.25)
                out["ok"] = True
                return out

            if "last service date" in blob or "last maintenance" in blob:
                self._fca_tap_confirm_ok()
                time.sleep(0.3)
                continue

            xml = self._ui_xml(timeout=1.2)
            xml_blob = (xml or "").lower()

            if "reset service information" in (blob + " " + xml_blob):
                try:
                    if adb.find_and_tap_text(self.serial, "Reset Service Information", timeout=3):
                        self._step("Tapped Reset Service Information", 0.77)
                        time.sleep(0.4)
                        continue
                except Exception:
                    pass

            cont = self._tap_label_from_xml(xml, ("Continue",), min_y_ratio=0.15)
            if cont:
                self._step("Tapped Continue", 0.78)
                time.sleep(0.35)
                continue

            if self._u2_has_text("OK", timeout=0.08):
                self._fca_tap_confirm_ok()
                time.sleep(0.3)
                continue

            now = time.time()
            if now - last_hb >= 6:
                self._step(f"Waiting for reset… {int(deadline - now)}s left", 0.76)
                last_hb = now
            time.sleep(0.25)

        out["error"] = "Timed out before 'Service Information Have Been Reset'"
        self._step(out["error"])
        return out

    def _fca_return_home(self) -> None:
        """Force-stop EURO LINK and relaunch — same as dashboard Hard Reset."""
        self._step("Hard reset — force-stop EURO LINK and reopen home", 0.93)
        reset = adb.hard_reset_x431_session(self.serial, relaunch=True)
        for step in reset.get("steps") or []:
            self._step(str(step), None)
        if reset.get("ok"):
            self._step("EURO LINK home after hard reset", 1.0)
        else:
            self._step("Hard reset finished with warnings", 0.95)
