"""VAG-focused Launch X431 EURO LINK workflows.

Scope (current phase):
  1. Identify vehicle (Intelligent Diagnose / Local Diagnose → VAG brand)
  2. Full system DTC read (High-speed Scan / Smart Detection)
  3. Save X431 Inspection Report and email via Gmail
     (default To: hotline.support@lkqbelgium.be; overridable per scan)
  4. Optional DTC clear / service reset (Oil / Brake / SAS / BMS)

Navigation is state-driven: read visible tablet UI text and advance to the
next logical control for Volkswagen / Audi / SEAT / Škoda / Cupra.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Isolate u2 RPC so a hung exists()/dump cannot freeze Start auto detection.
_U2_RPC = ThreadPoolExecutor(max_workers=2, thread_name_prefix="u2rpc")

from modules import adb_controller as adb
from modules.db import ensure_database, save_report, save_vin_audit, save_ticket, set_operator_context
from modules.engineer_session import DEFAULT_REPORT_EMAIL as REPORT_EMAIL
from modules.local_ocr_vision import LocalVisionEngine
from modules.x431_workflows import (
    HOME_AUTO_DIAGNOSE,
    HOME_MANUAL_DIAGNOSE,
    HOME_SERVICE,
    LaunchX431WorkflowEngine,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, Optional[float]], None]

INTELLIGENT_DIAGNOSE_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "assets" / "templates" / "intelligent_diagnose.png"
)
INTELLIGENT_DIAGNOSE_ICON_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "assets" / "templates" / "intelligent_diagnose_icon.png"
)

# EURO LINK home (1280×800): Intelligent Diagnose = LEFT of 3 large top tiles.
# Coordinates are last-resort only (name/icon preferred).
EUROLINK_INTELLIGENT_DIAGNOSE_POINTS: Sequence[Tuple[float, float]] = (
    (0.17, 0.30),  # left tile center
    (0.17, 0.24),  # cloud icon
    (0.17, 0.38),  # label "Intelligent Diagnose"
)
# Local Diagnose = tile to the RIGHT of Intelligent Diagnose on home.
LOCAL_DIAGNOSE_HOME_POINTS: Sequence[Tuple[float, float]] = (
    (0.38, 0.28),
    (0.42, 0.30),
    (0.38, 0.35),
    (0.45, 0.28),
)

DIAGNOSTIC_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "assets" / "templates" / "diagnostic.png"
)
DIAGNOSTIC_TILE_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "assets" / "templates" / "diagnostic_tile.png"
)

# AutoDetect Result: Diagnostic = LEFT white card (Scan History is right).
AUTODETECT_DIAGNOSTIC_POINTS: Sequence[Tuple[float, float]] = (
    (0.38, 0.42),
    (0.42, 0.40),
    (0.58, 0.40),
    (0.55, 0.42),
)

# Scan buttons on System Topology / System and Function footer.
# Prefer High-speed Scan when both exist; Smart Detection is the fallback.
HIGH_SPEED_SCAN_LABELS: Sequence[str] = (
    "High-speed Scan",
    "High speed Scan",
    "High-Speed Scan",
    "High-speed CAN",
)
SMART_DETECTION_LABELS: Sequence[str] = (
    "Smart Detection",
)
TOPOLOGY_SCAN_BUTTONS: Sequence[str] = tuple(HIGH_SPEED_SCAN_LABELS) + tuple(SMART_DETECTION_LABELS)
SCAN_BUTTON_LABELS: Sequence[str] = TOPOLOGY_SCAN_BUTTONS

# After full ECU scan: save report and email via Gmail.
# REPORT_EMAIL imported from engineer_session (hotline.support@lkqbelgium.be).
# 1280×800 fallbacks (text tap is preferred).
POINT_TOPOLOGY_REPORT = (0.42, 0.91)  # left of Clear All DTCs; prefer exact text tap
POINT_DIALOG_OK = (0.50, 0.72)
# Diagnostic Firewall Activated — large white OK, bottom-right of the warning page
POINT_FIREWALL_OK = (0.82, 0.82)
POINT_MORE_INFO_OK = (0.62, 0.78)
POINT_OTHER_SHARE = (0.70, 0.91)
POINT_SHARE_GMAIL = (0.50, 0.56)
# Gmail compose: blue Send triangle — middle of paperclip / Send / ⋮ (top-right).
POINT_GMAIL_SEND = (0.89, 0.055)
POINT_CLEAR_ALL_DTCS = (0.88, 0.91)
VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
VIN_FULL = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
# High-speed Scan / Smart Detection sit in the RIGHT footer (same row as Report),
# not the left VIN strip. Center taps (≈0.48) miss the button on many Renault layouts.
HIGH_SPEED_SCAN_POINTS: Sequence[Tuple[float, float]] = (
    (0.72, 0.90),
    (0.78, 0.90),
    (0.65, 0.90),
    (0.84, 0.90),
    (0.70, 0.86),
)

# VAG brand labels as they commonly appear on EURO LINK Local Diagnose / AutoDetect.
VAG_BRANDS: Sequence[str] = (
    "Volkswagen",
    "VW",
    "Audi",
    "SEAT",
    "Seat",
    "Skoda",
    "Škoda",
    "SKODA",
    "Cupra",
    "CUPRA",
    "VAG",
)

# Search terms / tile labels for Local Diagnose manual brand pick.
# Extended catalog lives in brand_catalog; VAG entries kept for compatibility.
from modules.brand_catalog import (
    LOCAL_DIAGNOSE_SEARCH,
    has_fca_oil_reset,
    has_full_workflow,
    has_renault_auto_search,
    has_toyota_auto_search,
    local_diagnose_search_terms,
)

BRAND_SEARCH_TERMS: Dict[str, Sequence[str]] = dict(LOCAL_DIAGNOSE_SEARCH)

# After identification, VAG menus expose these scan entry points.
VAG_SCAN_ENTRY: Sequence[str] = (
    "Health Report",
    "System Scan",
    "Automatic Scan",
    "Auto Scan",
    "Full System Scan",
    "Quick Test",
    "Scan DTCs",
    "Read Fault Code",
    "Read DTCs",
    "Fault Code",
)

VAG_CLEAR_ENTRY: Sequence[str] = (
    "Clear DTC",
    "Clear Fault Memory",
    "Clear Fault Code",
    "Erase Fault Code",
    "Erase Codes",
    "Clear Codes",
    "Clear Memory",
    "Erase DTCs",
)

# Service Function labels on VAG (home Service Function or in-diagnosis Special Function).
VAG_SERVICE_RESETS: Dict[str, Sequence[str]] = {
    "oil": (
        "Oil Lamp Reset",
        "Oil Maintenance Reset",
        "Oil Reset",
        "Service Reset",
        "Oil Service Reset",
        "Maintenance Light Reset",
    ),
    "brake": (
        "EPB",
        "Electronic Parking Brake",
        "Brake Pad Reset",
        "Brake Reset",
        "Parking Brake",
    ),
    "sas": (
        "Steering Angle",
        "Steering Angle Reset",
        "SAS Reset",
        "Steering Angle Sensor",
    ),
    "bms": (
        "Battery Matching",
        "Battery Reset",
        "BMS Reset",
        "Battery Registration",
        "Battery Adaption",
        "Battery Adaptation",
    ),
}


class VAGWorkflowEngine(LaunchX431WorkflowEngine):
    """EURO LINK automation restricted to VAG DTC + service-reset tasks."""

    def __init__(
        self,
        serial: str,
        callback: Optional[ProgressCallback] = None,
        vision: Optional[LocalVisionEngine] = None,
        preferred_brand: str = "Volkswagen",
        cancel_check: Optional[Callable[[], bool]] = None,
        engineer: str = "",
        report_email: str = "",
    ) -> None:
        super().__init__(
            serial,
            callback=callback,
            vision=vision,
            engineer=engineer,
            report_email=report_email,
        )
        self.preferred_brand = preferred_brand
        self.detected_vin: str = "UNKNOWN"
        self.detected_make: str = ""
        self.detected_model: str = ""
        self.cancel_check = cancel_check
        self._u2_failed = False
        self._win_wh: Tuple[int, int] = (1280, 800)
        ensure_database()
        set_operator_context(self.engineer, self.report_email)

    def cancelled(self) -> bool:
        try:
            return bool(self.cancel_check and self.cancel_check())
        except Exception:
            return False

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise RuntimeError("Stopped by user")

    def ensure_device(self):
        """Connect u2 with a hard timeout; ADB taps still work if u2 is down."""
        if self.device is not None:
            return self.device
        if self._u2_failed:
            raise RuntimeError("uiautomator2 unavailable this session")
        self._step("Connecting uiautomator2 session", None)
        try:
            self.device = adb.connect_u2(self.serial, reconnect=False)
            self._step("uiautomator2 connected", None)
            return self.device
        except Exception as exc:
            self._u2_failed = True
            self._step(f"uiautomator2 unavailable ({exc}) — using ADB taps only")
            raise

    def _click_by_text(self, labels: Sequence[str], timeout: float = 4.0) -> Optional[str]:
        """Tap a label — ADB first when u2 is down (Local Diagnose fallback path)."""
        # Prefer ADB whenever u2 already failed this session (parent would raise).
        if self._u2_failed or self.device is None:
            for label in labels:
                try:
                    if adb.find_and_tap_text(self.serial, label, timeout=max(2, int(timeout))):
                        self._step(f"Clicked via ADB: '{label}'")
                        return label
                except Exception:
                    continue
            # If u2 never connected yet, try once then fall back to ADB again
            if not self._u2_failed:
                try:
                    return LaunchX431WorkflowEngine._click_by_text(self, labels, timeout=timeout)
                except Exception as exc:
                    self._u2_failed = True
                    self._step(f"u2 click skipped ({exc}) — ADB only")
            for label in labels:
                try:
                    if adb.find_and_tap_text(self.serial, label, timeout=2):
                        self._step(f"Clicked via ADB: '{label}'")
                        return label
                except Exception:
                    continue
            return None
        try:
            return LaunchX431WorkflowEngine._click_by_text(self, labels, timeout=timeout)
        except Exception as exc:
            self._u2_failed = True
            self._step(f"u2 click failed ({exc}) — retrying ADB")
            for label in labels:
                try:
                    if adb.find_and_tap_text(self.serial, label, timeout=2):
                        self._step(f"Clicked via ADB: '{label}'")
                        return label
                except Exception:
                    continue
            return None

    # ------------------------------------------------------------------ perception

    def visible_texts(self) -> List[str]:
        """Return non-empty text / content-desc nodes currently on screen."""
        xml = self._ui_xml(timeout=2.0)
        if not xml:
            return []
        texts = re.findall(r'text="([^"]+)"', xml)
        texts.extend(re.findall(r'content-desc="([^"]+)"', xml))
        return [t.strip() for t in texts if t and t.strip()]

    def _ui_xml(self, timeout: float = 2.0) -> str:
        """One UI dump via u2 (bounded). Empty string on timeout."""
        try:
            device = self.ensure_device()
        except Exception:
            return ""
        return self._u2_rpc(device.dump_hierarchy, timeout=timeout, default="") or ""

    def _tap_label_from_xml(
        self,
        xml: str,
        labels: Sequence[str],
        min_y_ratio: float = 0.62,
        max_x_ratio: float = 1.0,
        min_x_ratio: float = 0.0,
    ) -> Optional[Tuple[str, int, int]]:
        """Tap a labeled control from a UI dump. Optional x/y filters (sidebar vs footer)."""
        hits = self._label_hits(
            xml,
            labels,
            min_y_ratio=min_y_ratio,
            max_x_ratio=max_x_ratio,
            min_x_ratio=min_x_ratio,
        )
        if not hits:
            return None
        hits.sort(key=lambda item: item[0], reverse=True)
        _area, label, x, y = hits[0]
        self._adb_tap(x, y)
        return (label, x, y)

    def _label_hits(
        self,
        xml: str,
        labels: Sequence[str],
        min_y_ratio: float = 0.0,
        max_x_ratio: float = 1.0,
        min_x_ratio: float = 0.0,
    ) -> List[Tuple[int, str, int, int]]:
        """Return (area, label, x, y) for matching nodes."""
        if not xml:
            return []
        w, h = self._window_size()
        min_y = int(h * min_y_ratio)
        max_x = int(w * max_x_ratio)
        min_x = int(w * min_x_ratio)
        hits: List[Tuple[int, str, int, int]] = []
        for tag in re.findall(r"<node\b[^>]*>", xml):
            text = re.search(r'text="([^"]*)"', tag)
            desc = re.search(r'content-desc="([^"]*)"', tag)
            bounds = re.search(r"bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"", tag)
            if not bounds:
                continue
            blob = f"{text.group(1) if text else ''} {desc.group(1) if desc else ''}".strip()
            if not blob:
                continue
            low = blob.lower()
            matched = None
            for label in labels:
                if label.lower() in low:
                    matched = label
                    break
            if not matched:
                continue
            l, t, r, b = (int(bounds.group(i)) for i in range(1, 5))
            x, y = (l + r) // 2, (t + b) // 2
            if y < min_y or x > max_x or x < min_x:
                continue
            area = max(1, (r - l) * (b - t))
            hits.append((area, matched, x, y))
        return hits

    def tap_ok_not_cancel(self, xml: Optional[str] = None) -> bool:
        """Tap the red OK (right), never CANCEL."""
        xml = xml if xml is not None else self._ui_xml(timeout=1.6)
        hits = self._label_hits(xml, ("OK",), min_y_ratio=0.28)
        if hits:
            hits.sort(key=lambda item: item[2], reverse=True)
            _area, _label, x, y = hits[0]
            self._adb_tap(x, y)
            return True
        if self._u2_has_text("OK", timeout=0.08):
            w, h = self._window_size()
            self._adb_tap(int(w * 0.62), int(h * 0.72))
            return True
        return False

    def is_diagnostic_firewall(self, blob: str = "") -> bool:
        """True on VAG 'Diagnostic Firewall Activated' (hood-open) warning."""
        low = (blob or "").lower()
        if not low:
            if self._u2_has_text(
                "Diagnostic Firewall Activated", "Diagnostic Firewall", timeout=0.06
            ):
                return True
            if self._u2_has_text("Hood To Be Open", "Hood Latched Closed", timeout=0.05):
                return True
            low = self._adb_screen_blob()
        if "firewall" in low and "diagnos" in low:
            return True
        if "hood to be open" in low or "hood latched" in low:
            return True
        return False

    def dismiss_diagnostic_firewall(self) -> bool:
        """If the VAG Diagnostic Firewall page is up, capture VIN and tap OK.

        Can appear before Topology, or just before/after High-speed Scan.
        """
        blob = self._adb_screen_blob()
        if not self.is_diagnostic_firewall(blob):
            return False

        # VIN is on the bottom-left of this page (e.g. VIN WVGZZZ5NZKW347266)
        m = re.search(r"VIN\s*[:：]?\s*([A-HJ-NPR-Z0-9]{17})", blob, re.IGNORECASE)
        if not m:
            m = VIN_RE.search((blob or "").upper())
        if m:
            vin = m.group(1).upper()
            if VIN_FULL.match(vin):
                self.detected_vin = vin
                self._step(f"Diagnostic Firewall — VIN {vin}", 0.93)

        self._step("Diagnostic Firewall Activated — tapping OK", 0.93)
        w, h = self._window_size()
        tapped = False
        try:
            candidates: List[Tuple[int, int, int]] = []
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                if raw.upper() != "OK":
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                x, y = (l + r) // 2, (t + b) // 2
                if y < int(h * 0.55) or x < int(w * 0.45):
                    continue
                candidates.append((x, y, (r - l) * (b - t)))
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                x, y, _a = candidates[0]
                self._adb_tap(x, y)
                tapped = True
                self._step(f"Tapped Firewall OK at ({x},{y})")
        except Exception as exc:
            self._step(f"Firewall OK lookup skip: {exc}")

        if not tapped:
            try:
                if adb.find_and_tap_text(self.serial, "OK", timeout=2):
                    tapped = True
                    self._step("Tapped Firewall OK (ADB text)")
            except Exception:
                pass

        if not tapped:
            x, y = int(w * POINT_FIREWALL_OK[0]), int(h * POINT_FIREWALL_OK[1])
            self._adb_tap(x, y)
            tapped = True
            self._step(f"Tapped Firewall OK (layout) at ({x},{y})")

        time.sleep(0.45)
        return True

    def screen_contains(self, *needles: str) -> bool:
        blob = " | ".join(self.visible_texts()).lower()
        return any(n.lower() in blob for n in needles)

    def is_eurolink_home(self) -> bool:
        """True when the EURO LINK V8 home tile grid is visible."""
        return self.is_home_ready()

    def ensure_eurolink_home(self, max_wait: float = 14.0) -> bool:
        """Return to EURO LINK home (back / relaunch) before tapping home tiles."""
        if self.is_autodetect_result() or self.is_identification_processing():
            return True
        if self.is_eurolink_home():
            return True

        self._step("Not on EURO LINK home — navigating back", 0.08)
        for _ in range(5):
            if self.is_eurolink_home():
                return True
            self.press_android_back()
            time.sleep(0.55)

        if self.is_eurolink_home():
            return True

        self._step("Relaunching EURO LINK to reach home", 0.1)
        try:
            msg = adb.launch_x431(self.serial)
            self._step(msg, 0.12)
        except Exception as exc:
            self._step(f"EURO LINK launch failed: {exc}")
            return False

        deadline = time.time() + max_wait
        while time.time() < deadline:
            if self.is_eurolink_home():
                self._step("EURO LINK home ready")
                return True
            time.sleep(0.45)
        return self.is_eurolink_home()

    def is_autodetect_result(self, blob: Optional[str] = None) -> bool:
        """True when AutoDetect Result (VIN/Diagnostic) page is showing."""
        if blob is not None:
            return self._blob_is_autodetect(blob)
        return self.autodetect_result_visible()

    def _blob_is_autodetect(self, blob: str) -> bool:
        low = (blob or "").lower()
        if "autodetect result" in low or "auto detect result" in low:
            return True
        if "scan history" in low and "diagnostic" in low:
            return True
        if "vehicle information" in low and "diagnostic" in low:
            return True
        if "vin:" in low and "make:" in low:
            return True
        return False

    def autodetect_result_visible(self) -> bool:
        """Cheap u2 checks for AutoDetect Result — title is not always readable."""
        if self._u2_has_text("AutoDetect Result", "Scan History", timeout=0.05):
            return True
        if self._u2_has_text("Auto Detect Result", "Vehicle Information", timeout=0.05):
            return True
        if self._u2_has_text("Diagnostic", timeout=0.05) and not self._u2_has_text(
            "Intelligent Diagnose", timeout=0.04
        ):
            return True
        return False

    def _adb_ui_texts(self) -> List[str]:
        """Visible labels via ADB uiautomator dump (works when u2 misses the title)."""
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

    def is_identification_processing(self, blob: Optional[str] = None) -> bool:
        """True on Intelligent Vehicle Identification / Processing screen."""
        if blob is not None:
            low = blob.lower()
            markers = (
                "intelligent vehicle identification",
                "processing,please wait",
                "processing, please wait",
                "connect vci",
                "read vin",
                "decode vin",
                "select make",
                "select brand",
            )
            return any(m in low for m in markers)
        if self.is_select_make_dialog():
            return True
        return self._u2_has_text(
            "Intelligent Vehicle Identification",
            "Connect VCI",
            "Read VIN",
            "Decode VIN",
            "Processing,please wait",
            "Processing, please wait",
            timeout=0.18,
        )

    def is_select_make_dialog(self) -> bool:
        """True when Intelligent Diagnose asks to confirm Peugeot vs Citroen (etc.)."""
        return self._u2_has_text("Select Make", "Select Brand", timeout=0.05) or self._u2_has_text(
            "Diagnostics for", timeout=0.05
        )

    def _select_make_aliases(self) -> List[str]:
        """Brand labels as they appear on Select Make cards, preferred name first."""
        preferred = (self.preferred_brand or "").strip()
        aliases: List[str] = []
        seen: set[str] = set()
        for term in (preferred, *self.brand_search_terms(preferred or None)):
            key = (term or "").strip()
            if not key:
                continue
            low = key.lower()
            if low in seen:
                continue
            seen.add(low)
            aliases.append(key)
        aliases.sort(key=lambda item: (0 if item == preferred else 1, -len(item)))
        return aliases

    def _tap_u2_text(self, label: str, *, contains: bool = False) -> bool:
        """Tap a visible u2 text node without dump_hierarchy."""
        try:
            device = self.ensure_device()
        except Exception:
            return False
        node = device(textContains=label) if contains else device(text=label)

        def _exists(n=node):
            return bool(n.exists(timeout=0.05))

        if not self._u2_rpc(_exists, timeout=0.14, default=False):
            return False
        return self._tap_node_center(node) is not None

    def handle_select_make_dialog(self) -> bool:
        """Tap the operator-chosen brand on Select Make, then Continue/OK if shown.

        Intelligent Diagnose sometimes cannot uniquely decode the VIN family
        (Peugeot/Citroen, Hyundai/Kia, VW/Audi, …) and shows two make cards
        before AutoDetect Result. Always pick ``preferred_brand``.
        """
        if not self.is_select_make_dialog():
            return False

        aliases = self._select_make_aliases()
        brand = aliases[0] if aliases else (self.preferred_brand or "brand")
        self._step(f"Select Make dialog — tapping {brand}", 0.72)

        tapped: Optional[str] = None
        for term in aliases:
            if self._tap_u2_text(term, contains=False):
                tapped = term
                break
            if len(term) > 3 and self._tap_u2_text(term, contains=True):
                tapped = term
                break

        if not tapped:
            xml = self._ui_xml(timeout=1.6)
            hit = self._tap_label_from_xml(xml, aliases, min_y_ratio=0.18, max_x_ratio=1.0)
            if hit:
                tapped = hit[0]

        if not tapped:
            self._step(f"Select Make visible but '{brand}' card was not found", 0.72)
            return False

        self._step(f"Tapped Select Make card: {tapped}", 0.74)
        time.sleep(0.35)
        self._confirm_select_make_continue()
        return True

    def _confirm_select_make_continue(self) -> None:
        """If Select Make still wants confirmation, tap Continue / OK / Next."""
        deadline = time.time() + 1.8
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self.autodetect_result_visible():
                return
            # Cards can linger; only wait for Continue while the title is still up.
            if not self._u2_has_text("Select Make", "Select Brand", timeout=0.05):
                return
            for label in ("Continue", "OK", "Confirm", "Next"):
                if self._tap_u2_text(label, contains=False):
                    self._step(f"Tapped {label} after Select Make", 0.75)
                    time.sleep(0.25)
                    return
            time.sleep(0.12)


    def extract_vin_from_screen(self) -> Optional[str]:
        """Pull a 17-char VIN from AutoDetect / Topology / vehicle info UI."""
        texts = list(self.visible_texts() or [])
        try:
            texts.extend(self._adb_ui_texts())
        except Exception:
            pass
        blob = " ".join(texts)
        # Prefer explicit "VIN: XXXXX" (AutoDetect Result / Topology bottom-left).
        m = re.search(r"VIN\s*[:：]\s*([A-HJ-NPR-Z0-9]{17})", blob, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        for text in texts:
            m = VIN_RE.search((text or "").upper())
            if m:
                return m.group(1)
        # Topology footer often shows bare VIN bottom-left without "VIN:" prefix.
        footer = self.extract_vin_topology_footer()
        if footer:
            return footer
        return None

    def extract_vin_topology_footer(self) -> Optional[str]:
        """VIN on System and Function / Topology bottom-left strip."""
        w, h = self._window_size()
        max_x = int(w * 0.42)
        min_y = int(h * 0.72)
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                m = VIN_RE.search(raw.upper())
                if not m:
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    # Accept bare VIN text even without bounds if unique
                    return m.group(1)
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                cx, cy = (l + r) // 2, (t + b) // 2
                if cx <= max_x and cy >= min_y:
                    return m.group(1)
                # Also accept left-half anywhere (some builds put VIN mid-left)
                if cx <= max_x and m:
                    return m.group(1)
        except Exception as exc:
            self._step(f"Topology VIN footer lookup skip: {exc}")
        return None

    def extract_vin_from_gmail_attachment(self) -> Optional[str]:
        """Best-effort VIN from Diagnostic Report PDF name on Gmail compose."""
        blob = self._adb_screen_blob()
        # e.g. Renault_VF1VJ0000XXXXX821135529.pdf
        m = re.search(
            r"(?:renault|dacia|vw|audi|seat|skoda|fiat|[\w]+)_([a-hj-npr-z0-9]{17})",
            blob,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).upper()
        m = VIN_RE.search(blob.upper())
        if m:
            return m.group(1)
        return None

    def capture_and_save_vin_audit(
        self,
        *,
        source: str,
        make: Optional[str] = None,
        model: Optional[str] = None,
        software: str = "",
    ) -> str:
        """Refresh VIN from screen if needed and persist for Admin audit."""
        vin = (self.detected_vin or "").strip().upper()
        if not vin or vin == "UNKNOWN" or not VIN_FULL.match(vin):
            vin = (
                self.extract_vin_from_screen()
                or self.extract_vin_topology_footer()
                or self.extract_vin_from_gmail_attachment()
                or ""
            )
        if vin and VIN_FULL.match(vin):
            self.detected_vin = vin
        else:
            vin = vin or self.detected_vin or "UNKNOWN"

        make_val = make or self.detected_make or self.preferred_brand or ""
        model_val = model or self.detected_model or ""
        try:
            save_vin_audit(
                serial=self.serial,
                brand_selected=self.preferred_brand,
                make_detected=make_val,
                vin=vin,
                software=software or "",
                source=source,
                model=model_val,
            )
            if vin and vin != "UNKNOWN":
                save_ticket(
                    vin,
                    make_val,
                    model_val,
                    f"VIN audit ({source})",
                    serial=self.serial,
                )
            self._step(f"VIN saved for audit · {vin} · source={source}")
        except Exception as exc:
            self._step(f"VIN audit save failed: {exc}")
        return vin

    def extract_make_from_screen(self) -> Optional[str]:
        """Read Make from AutoDetect Result (e.g. ``Make: VW``)."""
        texts = self.visible_texts()
        blob = " ".join(texts)
        m = re.search(r"Make\s*[:：]\s*([A-Za-z0-9\-]+)", blob, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return self._detect_vag_make_on_screen()

    def extract_model_from_screen(self) -> Optional[str]:
        """Read Model from AutoDetect Result (e.g. ``Model: Golf`` / ``Vehicle Model``)."""
        texts = self.visible_texts()
        blob = " ".join(texts)

        patterns = (
            r"(?:Vehicle\s+)?Model\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9 \-/\.]{0,40})",
            r"Type\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9 \-/\.]{0,40})",
        )
        skip = {
            "selection",
            "year",
            "information",
            "result",
            "diagnose",
            "diagnostic",
            "system",
            "full system",
            "series",
            "unknown",
            "n/a",
            "na",
            "--",
            "—",
        }
        for pattern in patterns:
            m = re.search(pattern, blob, re.IGNORECASE)
            if not m:
                continue
            value = re.sub(r"\s+", " ", m.group(1)).strip(" .:-")
            # Truncate if trailing UI labels leaked into the capture.
            value = re.split(
                r"\b(?:VIN|Make|Year|Software|Diagnostic|Vehicle|Engine|Displacement)\b",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" .:-")
            if value and value.lower() not in skip and len(value) >= 2:
                return value

        # Label and value on adjacent text nodes: ["Model", "Golf VII", ...]
        for i, text in enumerate(texts):
            label = text.strip().lower().rstrip(":：")
            if label not in {"model", "vehicle model", "type"}:
                continue
            if i + 1 >= len(texts):
                continue
            value = texts[i + 1].strip()
            if (
                value
                and value.lower() not in skip
                and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.upper())
                and len(value) < 60
            ):
                return value
        return None

    def extract_software_from_screen(self) -> str:
        """Best-effort software package line (e.g. VW Series V29.18 Full System)."""
        for text in self.visible_texts():
            if re.search(r"(Series|Full System|V\d+\.\d+)", text, re.IGNORECASE):
                if len(text) < 120:
                    return text.strip()
        return ""

    def extract_system_function_mode(self) -> Dict[str, str]:
        """Read diagnostic mode breadcrumb on System and Function (e.g. ``VW V29.18 >``).

        Returns keys: ``mode`` (full breadcrumb like ``VW V29.18``), ``make``, ``version``.
        """
        out = {"mode": "", "make": "", "version": ""}
        texts = self.visible_texts()
        blob = " ".join(texts)

        # Prefer exact breadcrumb text nodes: "VW V29.18 >" / "RENAULT V44.02 >"
        brand_alt = (
            r"(?:VW|Volkswagen|Audi|SEAT|Seat|Skoda|Škoda|SKODA|Cupra|CUPRA|"
            r"Renault|RENAULT|Dacia|DACIA)"
        )
        pattern = rf"\b({brand_alt})\s+(V?\d+\.\d+)\s*>?"
        for text in texts:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                make = m.group(1).strip()
                version = m.group(2).strip()
                if not version.upper().startswith("V"):
                    version = f"V{version}"
                mode = f"{make} {version}"
                out["mode"] = mode
                out["make"] = make
                out["version"] = version
                return out

        m = re.search(pattern, blob, re.IGNORECASE)
        if m:
            make = m.group(1).strip()
            version = m.group(2).strip()
            if not version.upper().startswith("V"):
                version = f"V{version}"
            out["mode"] = f"{make} {version}"
            out["make"] = make
            out["version"] = version
        return out

    def _parse_fields_from_texts(self, texts: List[str]) -> Dict[str, str]:
        """Parse VIN / Make / Model / Software from one text list (no extra dumps)."""
        blob = " ".join(texts)
        out = {"vin": "", "make": "", "model": "", "software": ""}

        m = re.search(r"VIN\s*[:：]\s*([A-HJ-NPR-Z0-9]{17})", blob, re.IGNORECASE)
        if m:
            out["vin"] = m.group(1).upper()
        else:
            for text in texts:
                m2 = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", text.upper())
                if m2:
                    out["vin"] = m2.group(1)
                    break

        m = re.search(r"Make\s*[:：]\s*([A-Za-z0-9\-]+)", blob, re.IGNORECASE)
        if m:
            out["make"] = m.group(1).strip()

        m = re.search(
            r"(?:Vehicle\s+)?Model\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9 \-/\.]{0,40})",
            blob,
            re.IGNORECASE,
        )
        if m:
            value = re.sub(r"\s+", " ", m.group(1)).strip(" .:-")
            value = re.split(
                r"\b(?:VIN|Make|Year|Software|Diagnostic|Vehicle|Engine)\b",
                value,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" .:-")
            if value and len(value) >= 2:
                out["model"] = value

        for text in texts:
            if re.search(r"(Series|Full System|V\d+\.\d+)", text, re.IGNORECASE) and len(text) < 120:
                out["software"] = text.strip()
                break
        return out

    def _u2_rpc(self, fn, timeout: float = 0.8, default=None):
        """Run a u2 call with a hard wall-clock timeout (exists() can hang forever)."""
        fut = _U2_RPC.submit(fn)
        try:
            return fut.result(timeout=max(0.08, float(timeout)))
        except FuturesTimeout:
            logger.debug("u2 RPC timed out after %.2fs", timeout)
            return default
        except Exception:
            return default

    def _u2_has_text(self, *labels: str, timeout: float = 0.12) -> bool:
        """Snapshot existence check — do not wait for the widget to appear."""
        try:
            device = self.ensure_device()
        except Exception:
            return False
        cap = min(0.06, max(0.03, float(timeout)))
        for label in labels[:2]:
            def _exists(lbl=label):
                return bool(device(text=lbl).exists(timeout=cap))

            if self._u2_rpc(_exists, timeout=cap + 0.08, default=False):
                return True
        return False

    def _u2_has_any(self, labels: Sequence[str], timeout: float = 0.25) -> bool:
        return self._u2_has_text(*labels, timeout=timeout)

    def _window_size(self) -> Tuple[int, int]:
        """EURO LINK is 1280×800 — skip a u2 round-trip on the hot path."""
        return self._win_wh

    def _adb_tap(self, x: int, y: int) -> None:
        adb.tap(self.serial, int(x), int(y))

    def _tap_node_center(self, node, nudge_up: int = 0) -> Optional[Tuple[int, int]]:
        """Tap a u2 node's center. Never call node.info/click without a hard timeout."""
        info = self._u2_rpc(lambda: node.info, timeout=0.25, default=None) or {}
        bounds = info.get("bounds") or {}
        if isinstance(bounds, dict) and "left" in bounds:
            x = (int(bounds["left"]) + int(bounds["right"])) // 2
            y = (int(bounds["top"]) + int(bounds["bottom"])) // 2
            y = max(20, y - int(nudge_up))
            self._adb_tap(x, y)
            return (x, y)
        clicked = self._u2_rpc(lambda: (node.click() or True), timeout=0.3, default=False)
        if clicked:
            return (-1, -1)
        return None

    def _confirm_twice(self, predicate, settle: float = 0.35) -> bool:
        """Require the same positive check twice to avoid flaky UI dumps."""
        if not predicate():
            return False
        time.sleep(settle)
        return bool(predicate())

    def is_home_ready(self) -> bool:
        """True if Intelligent Diagnose tile is visible (u2 or ADB)."""
        if self._u2_has_text("Intelligent Diagnose", timeout=0.12):
            return True
        try:
            blob = " ".join(self._adb_ui_texts()).lower()
        except Exception:
            blob = ""
        return "intelligent diagnose" in blob and "enter the model name" not in blob

    def wait_home_ready(self, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_home_ready():
                return True
            time.sleep(0.15)
        return self.is_home_ready()

    def identification_started(self) -> bool:
        return self._u2_has_text("Connect VCI", "AutoDetect Result", timeout=0.12)

    def wait_identification_started(self, timeout: float = 1.2) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self.identification_started():
                return True
            time.sleep(0.12)
        return self.identification_started()

    def preflight_device(self) -> None:
        """Connect u2 if possible; skip dialog hunting (too slow)."""
        self._step("Preflight: tablet session", 0.03)
        try:
            self.ensure_device()
        except Exception:
            return


    def wait_autodetect_result(self, timeout: float = 45.0) -> Dict[str, object]:
        """Wait for AutoDetect Result, then tap Diagnostic.

        If Local Diagnose appears instead (or AutoDetect never shows), return early
        so the caller can select the brand and continue the same report flow.
        """
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
        self._step(
            "Waiting for AutoDetect Result (fallback to Local Diagnose if it does not appear)…",
            0.6,
        )

        while time.time() < deadline:
            self.raise_if_cancelled()
            remaining = int(deadline - time.time())
            now = time.time()

            if self.autodetect_result_visible():
                return self._advance_from_autodetect(info)

            if select_make_taps < 3 and self.is_select_make_dialog():
                if self.handle_select_make_dialog():
                    info["saw_processing"] = True
                select_make_taps += 1
                time.sleep(0.2)
                continue

            # Local Diagnose instead of AutoDetect — switch immediately (any brand)
            if self.is_local_diagnose_page() or self._u2_has_text(
                "Enter the model name", timeout=0.06
            ):
                info["saw_local_diagnose"] = True
                info["error"] = "AutoDetect not shown — Local Diagnose visible, switching now"
                self._step(info["error"], 0.35)
                return info

            if now - last_adb_check >= 2.0 and (now - t_start) >= 1.0:
                last_adb_check = now
                blob = " ".join(self._adb_ui_texts()).lower()
                if self._blob_is_autodetect(blob):
                    self._step("AutoDetect Result confirmed (ADB dump)", 0.84)
                    return self._advance_from_autodetect(info)
                if self._blob_is_local_diagnose(blob):
                    info["saw_local_diagnose"] = True
                    info["error"] = "AutoDetect not shown — Local Diagnose visible, switching now"
                    self._step(info["error"], 0.35)
                    return info
                if any(m in blob for m in ("connect vci", "decode vin", "read vin", "please wait")):
                    info["saw_processing"] = True

            if not info["saw_processing"]:
                if self._u2_has_text("Connect VCI", "Decode VIN", timeout=0.05):
                    info["saw_processing"] = True
                    self._step("Processing VIN (Connect VCI / Read VIN)…", 0.68)
                elif home_retaps < 1 and (now - t_start) > 0.7:
                    if self._u2_has_text("Intelligent Diagnose", timeout=0.05) and not self._u2_has_text(
                        "Diagnostic", timeout=0.04
                    ):
                        self._step("Still on home — one more Intelligent Diagnose tap", 0.62)
                        home_retaps += 1
                        w, h = self._window_size()
                        self._adb_tap(int(w * 0.17), int(h * 0.28))

            if now - last_heartbeat >= 5:
                self._step(
                    f"Still waiting for AutoDetect… {remaining}s left "
                    f"(will continue on Local Diagnose + {self.preferred_brand} if it opens)",
                    0.7,
                )
                last_heartbeat = now
            time.sleep(0.12)

        # Timed out — tablet typically opens Local Diagnose by itself. Do not go home.
        info["saw_local_diagnose"] = True
        info["error"] = (
            f"AutoDetect Result not reached after {int(timeout)}s — "
            f"continuing on Local Diagnose ({self.preferred_brand})"
        )
        self._step(info["error"], 0.35)
        return info

    def _advance_from_autodetect(self, info: Dict[str, object]) -> Dict[str, object]:
        """Confirm AutoDetect Result, capture VIN for audit, then tap Diagnostic."""
        self._step("AutoDetect Result page confirmed — reading VIN for audit", 0.84)
        try:
            texts = list(self.visible_texts() or [])
            texts.extend(self._adb_ui_texts())
        except Exception:
            texts = list(self.visible_texts() or [])
        fields = self._parse_fields_from_texts(texts)
        vin = fields.get("vin") or self.extract_vin_from_screen() or ""
        make = fields.get("make") or self.preferred_brand
        model = fields.get("model") or ""
        software = fields.get("software") or ""
        if vin:
            self.detected_vin = vin
            info["vin"] = vin
        if make:
            self.detected_make = make
            info["make"] = make
        if model:
            self.detected_model = model
            info["model"] = model
        if software:
            info["software"] = software
        self.capture_and_save_vin_audit(
            source="autodetect_result",
            make=make,
            model=model,
            software=software,
        )

        self._step("Tapping Diagnostic now", 0.85)
        diag = self.tap_diagnostic()
        info["diagnostic_tapped"] = bool(diag.get("ok"))
        info["diagnostic_method"] = diag.get("method")
        info["ok"] = True
        info["make"] = make or self.preferred_brand
        return info

    def tap_diagnostic(self) -> Dict[str, object]:
        """Tap Diagnostic on AutoDetect Result — text first, then left-card coords."""
        result: Dict[str, object] = {"ok": False, "method": None, "error": None, "point": None}
        self._step("Tapping Diagnostic…", 0.91)
        if self._tap_u2_text("Diagnostic"):
            result.update(ok=True, method="u2")
            self._step("Tapped Diagnostic (text)", 0.92)
            time.sleep(0.4)
            self.dismiss_diagnostic_firewall()
            return result
        try:
            if adb.find_and_tap_text(self.serial, "Diagnostic", timeout=2):
                result.update(ok=True, method="adb_text")
                self._step("Tapped Diagnostic (ADB text)", 0.92)
                time.sleep(0.4)
                self.dismiss_diagnostic_firewall()
                return result
        except Exception as exc:
            self._step(f"ADB Diagnostic tap skip: {exc}")
        w, h = self._window_size()
        x, y = int(w * AUTODETECT_DIAGNOSTIC_POINTS[0][0]), int(h * AUTODETECT_DIAGNOSTIC_POINTS[0][1])
        self._adb_tap(x, y)
        result.update(ok=True, method="layout", point=(x, y))
        self._step(f"Tapped Diagnostic (left card) at ({x},{y})", 0.92)
        time.sleep(0.4)
        self.dismiss_diagnostic_firewall()
        return result

    # ------------------------------------------------------------------ Renault

    _RENAULT_MODEL_SKIP = frozenset(
        {
            "automatically search",
            "manually select",
            "special function",
            "adas calibration",
            "please enter keyword",
            "text correction",
            "show menu",
            "system information",
            "system and function",
            "system topology",
            "yes",
            "no",
            "ok",
            "cancel",
            "continue",
            "back",
            "renault",
            "dacia",
            "brand",
            "make",
            "vin",
            "sn",
            "vci",
            "home",
            "processing",
            "please wait",
            "processing, please wait",
            "end session",
            "end session.",
            "notes",
            "note",
            "report",
            "print",
            "exit",
            "demo",
            "help",
            "setting",
            "settings",
            "feedback",
            "high-speed scan",
            "smart detection",
            "select detection",
            "support sliding up and down",
            "support sliding",
            "normal",
            "abnormal",
            "scanned",
            "not scanned",
            "not configured",
        }
    )

    def is_renault_show_menu(self, blob: str = "") -> bool:
        """True on Renault/Dacia Show Menu (Automatically Search 2x2 grid)."""
        if blob:
            low = blob.lower()
            if "automatically search" in low and "manually select" in low:
                return True
            if "special function" in low and "adas calibration" in low:
                return True
        if self._u2_has_text("Automatically Search", timeout=0.08) and (
            self._u2_has_text("Manually Select", timeout=0.05)
            or self._u2_has_text("Special Function", timeout=0.05)
        ):
            return True
        if self._u2_has_text("Manually Select", timeout=0.05) and self._u2_has_text(
            "Special Function", timeout=0.04
        ):
            return True
        return False

    def is_renault_model_pick_page(self) -> bool:
        """True when Automatically Search has listed the identified Renault model."""
        if self.is_renault_show_menu() or self._renault_yes_no_popup():
            return False
        if self.is_system_function_page():
            return False
        if not self._u2_has_text("Please enter keyword", timeout=0.06):
            return False
        if self._u2_has_text("Manually Select", timeout=0.05):
            return False
        return has_renault_auto_search(self.preferred_brand) or self._u2_has_text(
            "Automatically Search", timeout=0.05
        )

    def _renault_yes_no_popup(self, blob: str = "") -> bool:
        """True when a System Information YES/NO confirm is on screen."""
        if self._u2_has_text("YES", timeout=0.06) and (
            self._u2_has_text("NO", timeout=0.05)
            or self._u2_has_text("System Information", timeout=0.05)
        ):
            return True
        if blob:
            low = blob.lower()
            if "yes" in low and "no" in low and (
                "system information" in low
                or "start system scan" in low
                or "ignition" in low
            ):
                return True
        return False

    def tap_yes_not_no(self) -> bool:
        """Tap the red YES (right), never NO."""
        if self._tap_u2_text("YES", contains=False):
            return True
        try:
            if adb.find_and_tap_text(self.serial, "YES", timeout=2):
                return True
        except Exception:
            pass
        xml = self._ui_xml(timeout=1.4)
        hits = self._label_hits(xml, ("YES",), min_y_ratio=0.28)
        if hits:
            hits.sort(key=lambda item: item[2], reverse=True)
            _area, _label, x, y = hits[0]
            self._adb_tap(x, y)
            return True
        w, h = self._window_size()
        self._adb_tap(int(w * 0.62), int(h * 0.62))
        return True

    def _renault_still_searching(self, blob: str = "") -> bool:
        """True while Automatically Search is still reading / identifying the model."""
        if self._u2_has_text("Searching", timeout=0.05):
            return True
        if self._u2_has_text("Please wait", "Please Wait", timeout=0.05):
            return True
        if self._u2_has_text("Processing", timeout=0.05):
            return True
        if self._u2_has_text("Reading", timeout=0.05):
            return True
        if self._u2_has_text("Identifying", timeout=0.05):
            return True
        low = (blob or "").lower()
        return bool(
            re.search(
                r"\b(searching|please wait|processing|identifying|being identified|"
                r"reading|connecting|decode|communication)\b",
                low,
            )
        )

    def tap_automatically_search(self) -> bool:
        """Tap Automatically Search on Renault Show Menu (top-left tile)."""
        if self._tap_u2_text("Automatically Search", contains=False):
            self._step("Tapped Automatically Search (text)", 0.885)
            return True
        try:
            if adb.find_and_tap_text(self.serial, "Automatically Search", timeout=2):
                self._step("Tapped Automatically Search (ADB text)", 0.885)
                return True
        except Exception as exc:
            self._step(f"ADB Automatically Search skip: {exc}")
        xml = self._ui_xml(timeout=1.4)
        hit = self._tap_label_from_xml(
            xml, ("Automatically Search",), min_y_ratio=0.16, max_x_ratio=0.55
        )
        if hit:
            self._step(f"Tapped Automatically Search at ({hit[1]},{hit[2]})", 0.885)
            return True
        w, h = self._window_size()
        x, y = int(w * 0.28), int(h * 0.42)
        self._adb_tap(x, y)
        self._step(f"Tapped Automatically Search (top-left tile) at ({x},{y})", 0.885)
        return True

    def _is_renault_model_label(self, raw: str) -> bool:
        """True for vehicle model rows only — never END SESSION / Notes / chrome."""
        text = (raw or "").strip()
        if not text or len(text) < 3 or len(text) > 80:
            return False
        low = text.lower().strip(" .")
        if low in self._RENAULT_MODEL_SKIP:
            return False
        if any(
            bad in low
            for bad in (
                "end session",
                "session",
                "notes",
                "report",
                "print",
                "exit",
                "keyword",
                "please wait",
                "processing",
                "system information",
                "text correction",
                "support sliding",
                "sliding up",
                "not scanned",
                "not configured",
            )
        ):
            return False
        if low.startswith("renault v") or low.startswith("dacia v"):
            return False
        if re.match(r"^(brand|make|vin|sn|model)\b", low):
            return False
        if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", text.upper()):
            return False
        if re.fullmatch(r"\d{8,}", text):
            return False
        if re.search(r"\d+\.\d+\s*v\b", low):
            return False
        if "please enter" in low:
            return False
        # Prefer real model rows: CAPTUR/QM3/… or similar alnum names
        if "/" in text:
            return True
        # Reject known chrome buttons even if not exact skip-list match
        if re.fullmatch(r"end\s+session\.?", low):
            return False
        if not re.search(r"[A-Za-z]", text):
            return False
        return len(text) >= 4

    def tap_renault_identified_model(
        self, elements: Optional[List[Dict[str, str]]] = None
    ) -> Optional[str]:
        """Tap the identified Renault model row (e.g. CAPTUR/QM3/KABIN). Never END SESSION."""
        w, h = self._window_size()
        min_y, max_y = int(h * 0.18), int(h * 0.78)
        candidates: List[Tuple[int, int, int, int, str]] = []

        if elements is None:
            try:
                elements = adb.get_ui_elements(self.serial)
            except Exception:
                elements = []

        for el in elements or []:
            blob = f"{el.get('text') or ''} {el.get('content_desc') or ''}".strip()
            if not self._is_renault_model_label(blob):
                continue
            match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
            if not match:
                continue
            l, t, r, b = (int(match.group(i)) for i in range(1, 5))
            x, y = (l + r) // 2, (t + b) // 2
            if y < min_y or y > max_y:
                continue
            area = max(1, (r - l) * (b - t))
            # Prefer slash model names; demote chrome-like short labels
            slash_bonus = 500_000 if "/" in blob else 0
            candidates.append((area + slash_bonus, x, y, t, blob))

        if not candidates:
            xml = self._ui_xml(timeout=1.2)
            if xml:
                for tag in re.findall(r"<node\b[^>]*>", xml):
                    text_m = re.search(r'text="([^"]*)"', tag)
                    desc_m = re.search(r'content-desc="([^"]*)"', tag)
                    bounds = re.search(r"bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"", tag)
                    if not bounds:
                        continue
                    blob = (
                        f"{text_m.group(1) if text_m else ''} "
                        f"{desc_m.group(1) if desc_m else ''}"
                    ).strip()
                    if not self._is_renault_model_label(blob):
                        continue
                    l, t, r, b = (int(bounds.group(i)) for i in range(1, 5))
                    x, y = (l + r) // 2, (t + b) // 2
                    if y < min_y or y > max_y:
                        continue
                    area = max(1, (r - l) * (b - t))
                    slash_bonus = 500_000 if "/" in blob else 0
                    candidates.append((area + slash_bonus, x, y, t, blob))

        if not candidates:
            return None
        texts = [str(c[4]) for c in candidates]
        fields = self._parse_fields_from_texts(texts)
        if fields.get("vin"):
            self.detected_vin = fields["vin"]
        # Prefer slash models; never pick END SESSION-like leftovers
        candidates.sort(key=lambda item: (-item[0], item[3]))
        for _score, x, y, _t, label in candidates:
            low = label.lower()
            if "end session" in low or low in ("notes", "note", "report"):
                continue
            self._adb_tap(x, y)
            return label
        return None

    def advance_renault_after_diagnostic(self, timeout: float = 300.0) -> Dict[str, object]:
        """Show Menu → Automatically Search → YES → YES → tap identified model.

        Keep waiting while Automatically Search is still running. Poll until the
        model row appears, then continue. Never tap END SESSION / Notes.
        """
        info: Dict[str, object] = {
            "ok": False,
            "model": "",
            "vin": "",
            "make": self.preferred_brand,
            "error": None,
        }
        # Soft deadline; extended while search UI is still active.
        deadline = time.time() + max(120.0, float(timeout))
        hard_deadline = time.time() + 600.0  # absolute safety cap (10 min)
        last_hb = 0.0
        last_adb = 0.0
        search_taps = 0
        yes_taps = 0
        model_taps = 0
        diag_retaps = 0
        local_retries = 0
        t_start = time.time()
        search_started = False
        self._step("Renault/Dacia Show Menu — tapping Automatically Search", 0.88)

        while time.time() < min(deadline, hard_deadline):
            self.raise_if_cancelled()
            now = time.time()
            blob = ""
            elements: List[Dict[str, str]] = []

            if self.is_system_function_page():
                info["ok"] = True
                if info.get("model"):
                    self.detected_model = str(info["model"])
                self._step("Renault — System and Function reached", 0.92)
                return info

            if diag_retaps < 2 and self.autodetect_result_visible() and not self.is_renault_show_menu():
                self._step("Still on AutoDetect Result — tapping Diagnostic again", 0.87)
                self.tap_diagnostic()
                diag_retaps += 1
                time.sleep(0.6)
                continue

            if local_retries < 2 and self.is_local_diagnose_page():
                self._step(
                    f"Local Diagnose instead of AutoDetect — searching {self.preferred_brand}",
                    0.86,
                )
                pick = self.search_and_select_brand(self.preferred_brand)
                local_retries += 1
                if pick.get("ok"):
                    self._step(
                        f"Tapped {pick.get('tapped') or self.preferred_brand} — waiting for Show Menu",
                        0.87,
                    )
                    time.sleep(0.8)
                continue

            adb_interval = 0.7 if yes_taps else 1.4
            if now - last_adb >= adb_interval and (now - t_start) >= 0.6:
                last_adb = now
                try:
                    elements = adb.get_ui_elements(self.serial)
                except Exception:
                    elements = []
                blob = " ".join(
                    (el.get("text") or el.get("content_desc") or "") for el in elements
                ).lower()

            if yes_taps < 6 and self._renault_yes_no_popup(blob):
                yes_taps += 1
                if yes_taps == 1:
                    self._step("System Information — tapping YES (start automatic scan)", 0.89)
                elif yes_taps == 2:
                    self._step("System Information — tapping YES (ignition confirm)", 0.90)
                else:
                    self._step(f"System Information — tapping YES ({yes_taps})", 0.90)
                self.tap_yes_not_no()
                time.sleep(0.45)
                search_started = True
                deadline = max(deadline, time.time() + 240.0)
                last_adb = 0.0
                continue

            searching = bool(self._renault_still_searching(blob))
            # While Automatically Search is still running, keep waiting for the model
            if yes_taps > 0 and searching and model_taps < 1:
                search_started = True
                deadline = max(deadline, time.time() + 90.0)
                if now - last_hb >= 5:
                    elapsed = int(now - t_start)
                    self._step(
                        f"Automatically Search still running — waiting for model… {elapsed}s",
                        0.905,
                    )
                    last_hb = now
                time.sleep(0.35)
                continue

            if (
                model_taps < 1
                and yes_taps > 0
                and not self._renault_yes_no_popup(blob)
                and elements
            ):
                model = self.tap_renault_identified_model(elements)
                if model and "end session" not in model.lower() and model.lower() not in (
                    "notes",
                    "note",
                    "report",
                ):
                    if "support sliding" in model.lower() or "sliding up" in model.lower():
                        info["ok"] = True
                        self._step(
                            "Topology page already visible after search — continuing to scan",
                            0.92,
                        )
                        return info
                    model_taps += 1
                    info["model"] = model
                    self.detected_model = model
                    if self.detected_vin and self.detected_vin != "UNKNOWN":
                        info["vin"] = self.detected_vin
                    self._step(f"Renault model identified — tapping {model}", 0.91)
                    time.sleep(0.5)
                    if self.is_system_function_page():
                        info["ok"] = True
                        self._step("Renault — System and Function reached after model tap", 0.92)
                        return info
                    last_adb = 0.0
                    continue
                if model:
                    self._step(
                        f"Ignored non-model label '{model}' — still waiting for model row",
                        0.905,
                    )

            if model_taps >= 1:
                if self.is_system_function_page():
                    info["ok"] = True
                    self._step("Renault — System and Function reached", 0.92)
                    return info
                if blob and (
                    "support sliding" in blob
                    or "system and function" in blob
                    or "high-speed scan" in blob
                    or "smart detection" in blob
                ):
                    info["ok"] = True
                    self._step("Renault — Topology markers seen — continuing to scan", 0.92)
                    return info

            show_menu = self.is_renault_show_menu(blob)
            force_search = (
                search_taps == 0
                and yes_taps == 0
                and (now - t_start) >= 1.5
                and not self._renault_yes_no_popup()
                and not self.is_renault_model_pick_page()
                and not self.autodetect_result_visible()
                and not self.is_local_diagnose_page()
            )
            if yes_taps == 0 and search_taps < 3 and (show_menu or force_search):
                search_taps += 1
                self._step("Tapping Automatically Search", 0.885)
                self.tap_automatically_search()
                time.sleep(0.5)
                continue

            if now - last_hb >= 5:
                elapsed = int(now - t_start)
                if search_started and model_taps < 1:
                    self._step(
                        f"Waiting for Renault model to appear… {elapsed}s "
                        f"(keeps waiting while search runs)",
                        0.905,
                    )
                elif model_taps and not self.is_system_function_page():
                    self._step(
                        f"Model tapped ({info.get('model')}) — checking for System and Function…",
                        0.91,
                    )
                else:
                    self._step(f"Checking Renault menu / model… {elapsed}s", 0.89)
                last_hb = now
            time.sleep(0.25)

        if info.get("model") and self.is_system_function_page():
            info["ok"] = True
            return info
        waited = int(time.time() - t_start)
        if info.get("model"):
            info["ok"] = True
            self._step(
                f"Renault model '{info['model']}' tapped — continuing (topology next)",
                0.92,
            )
            return info
        info["error"] = (
            "Renault model not displayed "
            f"(waited {waited}s). Search may have failed — check VCI / ignition."
        )
        self._step(info["error"])
        return info

    # ------------------------------------------------------------------ Toyota / Lexus

    def is_toyota_show_menu(self, blob: str = "") -> bool:
        """True on Toyota/Lexus Show Menu (Automatic Search + Manual Select grid)."""
        if blob:
            low = blob.lower()
            # Prefer "automatic search" (Toyota) over Renault's "automatically search".
            if "automatic search" in low and "automatically search" not in low:
                if any(
                    k in low
                    for k in (
                        "manual select",
                        "set area",
                        "diagnostic history",
                        "menu help",
                        "show menu",
                    )
                ):
                    return True
        if self._u2_has_text("Automatic Search (Europe and Other)", timeout=0.08):
            return True
        if self._u2_has_text("Automatic Search", timeout=0.08) and (
            self._u2_has_text("Manual Select", timeout=0.05)
            or self._u2_has_text("Set Area", timeout=0.05)
            or self._u2_has_text("Diagnostic History", timeout=0.05)
        ):
            return True
        # Avoid Renault wording
        if self._u2_has_text("Automatically Search", timeout=0.05):
            return False
        return False

    def tap_toyota_automatic_search(self) -> bool:
        """Tap Automatic Search (Europe and Other) on Toyota/Lexus Show Menu."""
        labels = (
            "Automatic Search (Europe and Other)",
            "Automatic Search",
        )
        for label in labels:
            if self._tap_u2_text(label, contains=False):
                self._step(f"Tapped {label} (text)", 0.885)
                return True
            try:
                if adb.find_and_tap_text(self.serial, label, timeout=2):
                    self._step(f"Tapped {label} (ADB text)", 0.885)
                    return True
            except Exception as exc:
                self._step(f"ADB {label} skip: {exc}")

        # Contains match for truncated / wrapped tile text.
        if self._tap_u2_text("Automatic Search", contains=True):
            self._step("Tapped Automatic Search (contains)", 0.885)
            return True

        xml = self._ui_xml(timeout=1.4)
        hit = self._tap_label_from_xml(
            xml,
            ("Automatic Search (Europe and Other)", "Automatic Search", "Europe and Other"),
            min_y_ratio=0.16,
            max_x_ratio=0.55,
        )
        if hit:
            self._step(f"Tapped Automatic Search at ({hit[1]},{hit[2]})", 0.885)
            return True

        # Top-left tile on the Show Menu grid (matches tablet layout).
        w, h = self._window_size()
        x, y = int(w * 0.28), int(h * 0.38)
        self._adb_tap(x, y)
        self._step(f"Tapped Automatic Search (top-left tile) at ({x},{y})", 0.885)
        return True

    def advance_toyota_after_diagnostic(self, timeout: float = 90.0) -> Dict[str, object]:
        """After Diagnostic: wait Show Menu → tap Automatic Search → hand off to Topology."""
        info: Dict[str, object] = {"ok": False, "tapped": False, "error": None}
        deadline = time.time() + timeout
        last_hb = 0.0
        tapped = False
        self._step(
            "Toyota/Lexus — waiting for Show Menu → Automatic Search (Europe and Other)",
            0.87,
        )

        while time.time() < deadline:
            self.raise_if_cancelled()
            now = time.time()
            remaining = int(deadline - now)

            if self.dismiss_diagnostic_firewall():
                continue

            if self.is_system_function_page():
                info["ok"] = True
                if tapped:
                    self._step("Topology reached after Automatic Search", 0.92)
                else:
                    self._step("Already on Topology — Automatic Search not needed", 0.92)
                return info

            try:
                blob = " ".join(self._adb_ui_texts())
            except Exception:
                blob = ""

            on_menu = self.is_toyota_show_menu(blob)
            if on_menu and not tapped:
                self._step("Show Menu — tapping Automatic Search (Europe and Other)", 0.88)
                self.tap_toyota_automatic_search()
                tapped = True
                info["tapped"] = True
                time.sleep(0.8)
                continue

            if tapped and not on_menu:
                # Left Show Menu; Topology / processing may follow.
                info["ok"] = True
                self._step(
                    "Automatic Search tapped — continuing toward Topology",
                    0.90,
                )
                return info

            if now - last_hb >= 4:
                if tapped:
                    self._step(
                        f"Waiting after Automatic Search… {remaining}s left",
                        0.89,
                    )
                else:
                    self._step(
                        f"Waiting for Toyota Show Menu… {remaining}s left",
                        0.88,
                    )
                last_hb = now
            time.sleep(0.2)

        if tapped:
            info["ok"] = True
            self._step(
                "Automatic Search tapped — Topology wait continues next",
                0.90,
            )
            return info
        info["error"] = (
            "Toyota/Lexus Show Menu / Automatic Search not reached after Diagnostic"
        )
        self._step(info["error"])
        return info

    def is_system_function_page(self) -> bool:
        """True on System and Function / Topology (High-speed Scan or Smart Detection)."""
        # Firewall warning sits in front of Topology — do not treat it as the scan page
        if self.is_diagnostic_firewall():
            return False
        if self._u2_has_text("System and Function", "System Topology", timeout=0.08):
            return True
        if self._u2_has_text("High-speed Scan", "Smart Detection", timeout=0.08):
            return True
        # Renault/VAG topology often shows this legend before scan buttons resolve in u2
        if self._u2_has_text("Support sliding up and down", timeout=0.08):
            return True
        try:
            blob = " ".join(self._adb_ui_texts()).lower()
        except Exception:
            blob = ""
        if not blob:
            return False
        if "system and function" in blob or "system topology" in blob:
            return True
        if "high-speed scan" in blob or "smart detection" in blob:
            return True
        if "support sliding" in blob and (
            "normal" in blob or "scanned" in blob or "vin" in blob
        ):
            return True
        return False

    def _is_fca_vehicle_id_popup(self, blob: str = "") -> bool:
        """Fiat/Stellantis vehicle ID modal after Diagnostic (CANCEL/OK)."""
        low = (blob or "").lower()
        if not low:
            try:
                low = " ".join(self._adb_ui_texts()).lower()
            except Exception:
                low = ""
        if not low:
            return False
        markers = (
            "model name",
            "please record",
            "identified wrong vehicle",
            "car code",
            "body name",
            "model description",
        )
        if "cancel" not in low:
            return False
        if any(m in low for m in markers):
            return True
        if "vin" in low and "model" in low and ("fiat" in low or "jeep" in low):
            return True
        return False

    def _is_fca_reading_wait(self, blob: str = "") -> bool:
        """Please Wait… reading after Fiat OK (not SGW unlock)."""
        low = (blob or "").lower()
        if "secure gateway" in low:
            return False
        return "please wait" in low

    def _fca_sgw_result_from_blob(self, blob: str) -> Optional[str]:
        low = (blob or "").lower()
        if "unlocked successfully" in low or "secure gateway unlocked" in low:
            return "success"
        if any(
            m in low
            for m in (
                "unlock failed",
                "unlocked failed",
                "unlock unsuccessful",
                "failed to unlock",
                "unable to unlock",
            )
        ):
            return "failed"
        return None

    def wait_system_topology(self, timeout: float = 45.0) -> Dict[str, object]:
        """Poll until System and Function, then tap High-speed Scan or Smart Detection.

        For Fiat/FCA: after Diagnostic wait for vehicle ID → OK → Please Wait reading
        → (SGW Success/Fail OK if shown) → Topology.
        """
        info: Dict[str, object] = {
            "ok": False,
            "vin": "",
            "mode": "",
            "make": "",
            "version": "",
            "software": "",
            "model": "",
            "scan_tapped": False,
            "scan_button": None,
            "saw_local_diagnose": False,
            "error": None,
        }
        # FCA cars need longer: Fiat ID + reading (+ optional SGW) before Topology.
        if has_fca_oil_reset(self.preferred_brand):
            timeout = max(float(timeout), 240.0)
        deadline = time.time() + timeout
        last_heartbeat = 0.0
        last_adb = 0.0
        diag_retaps = 0
        select_make_taps = 0
        renault_tried = False
        toyota_tried = False
        fiat_ok_tapped = False
        sgw_ok_tapped = False
        screen_blob = ""
        self._step(
            "Checking for System Topology / System and Function "
            "(Fiat ID OK → reading if shown)…",
            0.93,
        )

        while time.time() < deadline:
            self.raise_if_cancelled()
            remaining = int(deadline - time.time())
            now = time.time()

            if self.dismiss_diagnostic_firewall():
                last_adb = 0.0
                continue

            if now - last_adb >= 0.7:
                last_adb = now
                try:
                    screen_blob = " ".join(self._adb_ui_texts()).lower()
                except Exception:
                    screen_blob = ""

            on_topo = self.is_system_function_page()
            if not on_topo and screen_blob:
                on_topo = bool(
                    "system and function" in screen_blob
                    or "system topology" in screen_blob
                    or "high-speed scan" in screen_blob
                    or "smart detection" in screen_blob
                    or ("support sliding" in screen_blob and "vin" in screen_blob)
                )

            if on_topo:
                self._step(
                    "System and Function ready — High-speed Scan if present, else Smart Detection",
                    0.94,
                )
                xml = self._ui_xml(timeout=2.0)
                texts: List[str] = []
                if xml:
                    texts = [t.strip() for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]
                    texts.extend(
                        t.strip() for t in re.findall(r'content-desc="([^"]+)"', xml) if t.strip()
                    )
                    ident = self.extract_topology_identity(texts)
                    prev_model = str(info.get("model") or "")
                    prev_vin = str(info.get("vin") or "")
                    info.update(ident)
                    if not info.get("model") and prev_model:
                        info["model"] = prev_model
                    if not info.get("vin") and prev_vin:
                        info["vin"] = prev_vin
                    if ident.get("vin") or ident.get("mode") or info.get("model"):
                        self._step(
                            "Topology identity"
                            + (f" · VIN {info.get('vin')}" if info.get("vin") else "")
                            + (f" · model {info.get('model')}" if info.get("model") else "")
                            + (f" · mode {ident.get('mode')}" if ident.get("mode") else ""),
                            0.945,
                        )
                    if not info.get("vin"):
                        footer_vin = self.extract_vin_topology_footer() or self.extract_vin_from_screen()
                        if footer_vin:
                            info["vin"] = footer_vin
                            self.detected_vin = footer_vin
                            self._step(f"Topology bottom-left VIN: {footer_vin}", 0.946)
                    if info.get("vin"):
                        self.capture_and_save_vin_audit(
                            source="system_and_function_topology",
                            make=str(info.get("make") or "") or None,
                            model=str(info.get("model") or "") or None,
                            software=str(info.get("mode") or info.get("software") or ""),
                        )
                scan = self.start_topology_ecu_scan(xml=xml)
                info["ok"] = True
                info["scan_tapped"] = bool(scan.get("ok"))
                info["scan_button"] = scan.get("button")
                return info

            # Fiat/FCA: vehicle ID (pic1) → OK → Please Wait reading (pic2) → Topology / SGW
            if not fiat_ok_tapped and self._is_fca_vehicle_id_popup(screen_blob):
                self._step("Fiat vehicle ID popup — tapping OK", 0.91)
                self._tap_ok_preferred()
                fiat_ok_tapped = True
                deadline = max(deadline, time.time() + 180.0)
                time.sleep(0.5)
                last_adb = 0.0
                continue

            sgw_result = self._fca_sgw_result_from_blob(screen_blob)
            if sgw_result and not sgw_ok_tapped:
                label = (
                    "Secure Gateway Unlocked Successfully"
                    if sgw_result == "success"
                    else "Secure Gateway Unlock Failed"
                )
                self._step(f"{label} — tapping OK", 0.92)
                self._tap_ok_preferred()
                sgw_ok_tapped = True
                deadline = max(deadline, time.time() + 90.0)
                time.sleep(0.55)
                last_adb = 0.0
                continue

            if fiat_ok_tapped and self._is_fca_reading_wait(screen_blob):
                deadline = max(deadline, time.time() + 90.0)
                if now - last_heartbeat >= 5:
                    self._step(
                        f"Reading vehicle (Please Wait…)… {remaining}s left — next Topology or SGW",
                        0.92,
                    )
                    last_heartbeat = now
                time.sleep(0.3)
                continue

            if (
                fiat_ok_tapped
                and not sgw_ok_tapped
                and "secure gateway" in screen_blob
                and "unlocked successfully" not in screen_blob
            ):
                deadline = max(deadline, time.time() + 120.0)
                if now - last_heartbeat >= 6:
                    self._step(
                        f"SGW unlocking… waiting for Success/Fail ({remaining}s left)",
                        0.92,
                    )
                    last_heartbeat = now
                time.sleep(0.35)
                continue

            if not renault_tried and (
                self.is_renault_show_menu()
                or self._renault_yes_no_popup()
                or self.is_renault_model_pick_page()
            ):
                adv = self.advance_renault_after_diagnostic(timeout=300)
                renault_tried = True
                if adv.get("model"):
                    info["model"] = str(adv["model"])
                if adv.get("vin"):
                    info["vin"] = str(adv["vin"])
                if not adv.get("ok") and adv.get("error"):
                    info["error"] = adv["error"]
                    return info
                # Model tapped — Topology often opens immediately; keep checking, don't idle
                deadline = max(deadline, time.time() + 30)
                last_adb = 0.0
                continue

            if (
                not toyota_tried
                and has_toyota_auto_search(self.preferred_brand)
                and self.is_toyota_show_menu()
            ):
                adv = self.advance_toyota_after_diagnostic(timeout=90)
                toyota_tried = True
                if not adv.get("ok") and adv.get("error"):
                    info["error"] = adv["error"]
                    return info
                deadline = max(deadline, time.time() + 30)
                last_adb = 0.0
                continue

            if select_make_taps < 2 and self.is_select_make_dialog():
                self.handle_select_make_dialog()
                select_make_taps += 1
                time.sleep(0.2)
                continue
            if self._u2_has_text("Enter the model name", timeout=0.05):
                info["saw_local_diagnose"] = True
                info["error"] = "Bounced to Local Diagnose — switching to brand select"
                self._step(info["error"], 0.36)
                return info
            # Do not retap Diagnostic while Fiat ID / reading / SGW is on screen
            if (
                diag_retaps < 2
                and not fiat_ok_tapped
                and not self._is_fca_vehicle_id_popup(screen_blob)
                and self.autodetect_result_visible()
            ):
                self._step("Still on AutoDetect Result — tapping Diagnostic again", 0.91)
                self.tap_diagnostic()
                diag_retaps += 1
            if now - last_heartbeat >= 4:
                hint = "Topology"
                if self._is_fca_vehicle_id_popup(screen_blob):
                    hint = "Fiat vehicle ID"
                elif self._is_fca_reading_wait(screen_blob):
                    hint = "Please Wait / reading"
                elif "secure gateway" in screen_blob:
                    hint = "SGW"
                self._step(f"Waiting for {hint}… {remaining}s left", 0.93)
                last_heartbeat = now
            time.sleep(0.15)

        info["error"] = "Timed out waiting for System Topology page after Diagnostic"
        self._step(info["error"])
        return info

    def extract_topology_identity(self, texts: List[str]) -> Dict[str, str]:
        """VIN bottom-left; model/mode top-left under System and Function."""
        out = {"vin": "", "make": "", "model": "", "mode": "", "version": "", "software": ""}
        fields = self._parse_fields_from_texts(texts)
        mode_info = self.extract_system_function_mode_from_texts(texts)
        out["vin"] = fields.get("vin") or ""
        out["make"] = mode_info.get("make") or fields.get("make") or ""
        out["mode"] = mode_info.get("mode") or ""
        out["version"] = mode_info.get("version") or ""
        out["software"] = out["mode"] or fields.get("software") or ""
        out["model"] = fields.get("model") or ""

        skip = {
            "system and function",
            "system topology",
            "high-speed scan",
            "high-speed can",
            "smart detection",
            "gateway scan",
            "diagnostic",
            "scan history",
            "vin",
            "make",
            "model",
        }
        title_idx = None
        for i, text in enumerate(texts):
            low = text.strip().lower()
            if low in {"system and function", "system topology"}:
                title_idx = i
                break
        if title_idx is not None:
            for nxt in texts[title_idx + 1 : title_idx + 8]:
                value = nxt.strip().rstrip(">").strip()
                if not value or len(value) < 2 or len(value) > 48:
                    continue
                low = value.lower()
                if low in skip or "scan" in low:
                    continue
                if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.upper()):
                    if not out["vin"]:
                        out["vin"] = value.upper()
                    continue
                brand_ver = re.match(
                    r"^(VW|Volkswagen|Audi|SEAT|Seat|Skoda|Škoda|SKODA|Cupra|CUPRA)\s+(V?\d+\.\d+)$",
                    value,
                    re.IGNORECASE,
                )
                if brand_ver:
                    make = brand_ver.group(1).strip()
                    version = brand_ver.group(2).strip()
                    if not version.upper().startswith("V"):
                        version = f"V{version}"
                    if not out["mode"]:
                        out["mode"] = f"{make} {version}"
                        out["make"] = make
                        out["version"] = version
                        out["software"] = out["mode"]
                    continue
                if not out["model"]:
                    out["model"] = value
                    break

        if out["vin"]:
            self.detected_vin = out["vin"]
        if out["make"]:
            self.detected_make = out["make"]
        if out["model"]:
            self.detected_model = out["model"]
        return out

    def extract_system_function_mode_from_texts(self, texts: List[str]) -> Dict[str, str]:
        """Same as extract_system_function_mode but reuses an existing text dump."""
        out = {"mode": "", "make": "", "version": ""}
        blob = " ".join(texts)
        brand_alt = (
            r"(?:VW|Volkswagen|Audi|SEAT|Seat|Skoda|Škoda|SKODA|Cupra|CUPRA|"
            r"Renault|RENAULT|Dacia|DACIA)"
        )
        pattern = rf"\b({brand_alt})\s+(V?\d+\.\d+)\s*>?"
        for text in texts:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                make = m.group(1).strip()
                version = m.group(2).strip()
                if not version.upper().startswith("V"):
                    version = f"V{version}"
                out["mode"] = f"{make} {version}"
                out["make"] = make
                out["version"] = version
                return out
        m = re.search(pattern, blob, re.IGNORECASE)
        if m:
            make = m.group(1).strip()
            version = m.group(2).strip()
            if not version.upper().startswith("V"):
                version = f"V{version}"
            out["mode"] = f"{make} {version}"
            out["make"] = make
            out["version"] = version
        return out

    def start_topology_ecu_scan(self, xml: Optional[str] = None) -> Dict[str, object]:
        """Tap High-speed Scan if it exists, otherwise Smart Detection.

        Prefer ADB text bounds (works when u2 is unavailable). Footer buttons are
        on the RIGHT — never use center/VIN-column fallbacks.
        """
        result: Dict[str, object] = {"ok": False, "button": None, "error": None, "point": None}
        w, h = self._window_size()
        self.dismiss_diagnostic_firewall()

        # 1) ADB text search first (reliable when u2 is down)
        for label in ("High-speed Scan", "High speed Scan", "High-Speed Scan"):
            try:
                if adb.find_and_tap_text(self.serial, label, timeout=3):
                    result.update(ok=True, button="High-speed Scan", method="adb_text")
                    self._step(f"Tapped High-speed Scan (ADB text '{label}')", 0.95)
                    time.sleep(0.35)
                    self.dismiss_diagnostic_firewall()
                    return result
            except Exception as exc:
                self._step(f"ADB High-speed Scan skip: {exc}")

        # 2) XML dump — prefer labels in the lower/right footer
        xml = xml if xml is not None else self._ui_xml(timeout=2.2)
        hit = self._tap_label_from_xml(
            xml,
            HIGH_SPEED_SCAN_LABELS,
            min_y_ratio=0.55,
            min_x_ratio=0.35,
        )
        if hit:
            label, x, y = hit
            result.update(ok=True, button=label, point=(x, y), method="dump_bounds")
            self._step(f"Tapped High-speed Scan '{label}' at ({x},{y})", 0.95)
            time.sleep(0.35)
            self.dismiss_diagnostic_firewall()
            return result

        if self._tap_u2_text("High-speed Scan"):
            result.update(ok=True, button="High-speed Scan", method="u2")
            self._step("Tapped High-speed Scan (u2)", 0.95)
            time.sleep(0.35)
            self.dismiss_diagnostic_firewall()
            return result

        # 3) Smart Detection fallback (same right footer row)
        try:
            if adb.find_and_tap_text(self.serial, "Smart Detection", timeout=3):
                result.update(ok=True, button="Smart Detection", method="adb_text")
                self._step("High-speed Scan not found — tapped Smart Detection (ADB)", 0.95)
                time.sleep(0.35)
                self.dismiss_diagnostic_firewall()
                return result
        except Exception:
            pass

        hit = self._tap_label_from_xml(
            xml,
            SMART_DETECTION_LABELS,
            min_y_ratio=0.55,
            min_x_ratio=0.35,
        )
        if hit:
            label, x, y = hit
            result.update(ok=True, button=label, point=(x, y), method="dump_bounds")
            self._step(f"High-speed Scan not present — tapped Smart Detection at ({x},{y})", 0.95)
            return result

        if self._tap_u2_text("Smart Detection"):
            result.update(ok=True, button="Smart Detection", method="u2")
            self._step("High-speed Scan not present — tapped Smart Detection (u2)", 0.95)
            return result

        # 4) ADB dump of footer nodes — find any scan label by contains
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = f"{el.get('text') or ''} {el.get('content_desc') or ''}".strip()
                low = raw.lower()
                if "high-speed" in low or "high speed" in low or "smart detection" in low:
                    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                    if not match:
                        continue
                    l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                    x, y = (l + r) // 2, (t + b) // 2
                    if y < int(h * 0.55) or x < int(w * 0.30):
                        continue
                    self._adb_tap(x, y)
                    button = "High-speed Scan" if "high" in low else "Smart Detection"
                    result.update(ok=True, button=button, point=(x, y), method="adb_bounds")
                    self._step(f"Tapped '{raw}' at ({x},{y})", 0.95)
                    return result
        except Exception as exc:
            self._step(f"ADB footer scan lookup skip: {exc}")

        # 5) Right-footer layout only (never center / VIN column)
        self._step(
            "Scan button not in dump — tapping RIGHT footer (High-speed Scan area)",
            0.95,
        )
        for fx, fy in HIGH_SPEED_SCAN_POINTS[:3]:
            x, y = int(w * fx), int(h * fy)
            self._adb_tap(x, y)
            self._step(f"Tapped right-footer scan candidate at ({x},{y})", 0.952)
            time.sleep(0.35)
            # Stop early if button text disappeared (scan likely started)
            still_there = False
            try:
                blob = " ".join(self._adb_ui_texts()).lower()
                still_there = "high-speed scan" in blob or "smart detection" in blob
            except Exception:
                still_there = True
            if not still_there:
                result.update(
                    ok=True,
                    button="High-speed Scan",
                    point=(x, y),
                    method="layout_right",
                )
                self._step(f"Scan started after right-footer tap at ({x},{y})", 0.955)
                return result

        x, y = int(w * HIGH_SPEED_SCAN_POINTS[0][0]), int(h * HIGH_SPEED_SCAN_POINTS[0][1])
        result.update(ok=True, button="High-speed Scan", point=(x, y), method="layout_right")
        self._step(f"Tapped right-footer scan fallback at ({x},{y})", 0.955)
        return result

    def _tap_text_now(
        self,
        label: str,
        fallback: Optional[Tuple[float, float]] = None,
        description: Optional[str] = None,
    ) -> bool:
        """Tap a labeled control. Prefer ADB fallback coords — u2 node.info can hang."""
        if fallback:
            w, h = self._window_size()
            self._adb_tap(int(w * fallback[0]), int(h * fallback[1]))
            return True
        try:
            device = self.ensure_device()
            if description:
                node = device(description=description)
                if self._u2_rpc(lambda: node.exists(timeout=0.1), timeout=0.28, default=False):
                    pt = self._tap_node_center(node)
                    return pt is not None
            node = device(text=label)
            if self._u2_rpc(lambda: node.exists(timeout=0.1), timeout=0.28, default=False):
                pt = self._tap_node_center(node)
                return pt is not None
        except Exception:
            pass
        return False

    def _wait_text(self, *labels: str, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self._u2_has_text(*labels, timeout=0.05):
                return True
            time.sleep(0.12)
        return False

    def _topology_scan_buttons_visible(self) -> bool:
        """True when High-speed Scan or Smart Detection is still on the Topology footer."""
        if self._u2_has_text("High-speed Scan", "Smart Detection", timeout=0.08):
            return True
        try:
            blob = " ".join(self._adb_ui_texts()).lower()
        except Exception:
            return False
        return "high-speed scan" in blob or "high speed scan" in blob or "smart detection" in blob

    def _ecu_scan_done_visible(self, blob: str = "") -> bool:
        """True when Report + Clear All DTCs (or Clear DTC) are on the footer — scan finished."""
        if self._u2_has_text("Report", timeout=0.1) and (
            self._u2_has_text("Clear All DTCs", timeout=0.08)
            or self._u2_has_text("Clear DTC", timeout=0.08)
            or self._u2_has_text("Compare Results", timeout=0.06)
        ):
            return True
        low = (blob or "").lower()
        if not low:
            try:
                low = " ".join(self._adb_ui_texts()).lower()
            except Exception:
                return False
        has_report = bool(re.search(r"\breport\b", low)) and "report information" not in low
        has_clear = (
            "clear all dtcs" in low
            or "clear all dtc" in low
            or "clear dtc" in low
            or "compare results" in low
        )
        return has_report and has_clear

    def wait_ecu_scan_complete(self, timeout: float = 600.0) -> Dict[str, object]:
        """Keep waiting until Report + Clear All DTCs are visible (scan done).

        Duration can be short or long — only the footer buttons matter. Soft deadline
        is extended while the scan is still running.
        """
        info: Dict[str, object] = {"ok": False, "error": None}
        self._step(
            "ECU scan running — waiting until Report + Clear All DTCs are visible…",
            0.96,
        )
        deadline = time.time() + max(300.0, float(timeout))
        hard_deadline = time.time() + 1200.0  # 20 min absolute max
        last_hb = 0.0
        last_adb = 0.0
        scan_retaps = 0
        t_start = time.time()
        blob = ""

        while time.time() < min(deadline, hard_deadline):
            self.raise_if_cancelled()
            now = time.time()

            if now - last_adb >= 1.2:
                last_adb = now
                try:
                    blob = " ".join(self._adb_ui_texts()).lower()
                except Exception:
                    blob = ""

            if self.is_diagnostic_firewall(blob) or self.dismiss_diagnostic_firewall():
                last_adb = 0.0
                continue

            if self._ecu_scan_done_visible(blob):
                self._step(
                    "ECU scan complete — Report + Clear All DTCs visible",
                    0.97,
                )
                info["ok"] = True
                return info

            # Early window: if scan buttons still there, scan never started — retap
            if (
                scan_retaps < 6
                and (now - t_start) < 30
                and self._topology_scan_buttons_visible()
            ):
                scan_retaps += 1
                self._step(
                    f"Scan not started — retapping High-speed Scan / Smart Detection ({scan_retaps})",
                    0.95,
                )
                self.start_topology_ecu_scan()
                time.sleep(0.5)
                last_adb = 0.0
                continue

            # Still scanning — keep waiting (extend soft deadline)
            deadline = max(deadline, time.time() + 120.0)
            if now - last_hb >= 8:
                elapsed = int(now - t_start)
                self._step(
                    f"Still scanning ECUs… {elapsed}s — waiting for Report + Clear All DTCs",
                    0.96,
                )
                last_hb = now
            time.sleep(0.35)

        info["error"] = (
            "Timed out waiting for Report + Clear All DTCs "
            f"(waited {int(time.time() - t_start)}s)"
        )
        self._step(info["error"])
        return info

    def tap_topology_report(self) -> Dict[str, object]:
        """Tap the Topology footer Report button only — never Clear All DTCs / scan buttons.

        Exact label 'Report' (not Health Report / Diagnostic Report). Prefer ADB bounds.
        """
        out: Dict[str, object] = {"ok": False, "point": None, "method": None, "error": None}
        w, h = self._window_size()
        min_y = int(h * 0.72)
        max_y = int(h * 0.98)
        # Keep away from far-right Clear All DTCs and far-left VIN strip
        min_x = int(w * 0.28)
        max_x = int(w * 0.78)

        def _is_exact_report(raw: str) -> bool:
            t = (raw or "").strip()
            if t.lower() != "report":
                return False
            return True

        candidates: List[Tuple[int, int, int, str]] = []
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                if not _is_exact_report(raw):
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                x, y = (l + r) // 2, (t + b) // 2
                if y < min_y or y > max_y or x < min_x or x > max_x:
                    continue
                area = max(1, (r - l) * (b - t))
                candidates.append((area, x, y, raw))
        except Exception as exc:
            self._step(f"ADB Report lookup skip: {exc}")

        if candidates:
            # Prefer the smallest exact 'Report' node in the footer (button label, not a wide row)
            candidates.sort(key=lambda item: item[0])
            _area, x, y, raw = candidates[0]
            self._adb_tap(x, y)
            out.update(ok=True, point=(x, y), method="adb_exact")
            self._step(f"Tapped footer Report at ({x},{y})", 0.972)
            return out

        # XML dump exact match
        xml = self._ui_xml(timeout=2.0)
        exact_hits = []
        if xml:
            for tag in re.findall(r"<node\b[^>]*>", xml):
                text_m = re.search(r'text="([^"]*)"', tag)
                desc_m = re.search(r'content-desc="([^"]*)"', tag)
                bounds = re.search(r"bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"", tag)
                if not bounds:
                    continue
                raw = (text_m.group(1) if text_m else "").strip() or (
                    desc_m.group(1) if desc_m else ""
                ).strip()
                if not _is_exact_report(raw):
                    continue
                l, t, r, b = (int(bounds.group(i)) for i in range(1, 5))
                x, y = (l + r) // 2, (t + b) // 2
                if y < min_y or x < min_x or x > max_x:
                    continue
                exact_hits.append((max(1, (r - l) * (b - t)), x, y))
        if exact_hits:
            exact_hits.sort(key=lambda item: item[0])
            _a, x, y = exact_hits[0]
            self._adb_tap(x, y)
            out.update(ok=True, point=(x, y), method="xml_exact")
            self._step(f"Tapped footer Report (XML) at ({x},{y})", 0.972)
            return out

        # ADB text search for exact Report
        try:
            if adb.find_and_tap_text(self.serial, "Report", timeout=3):
                # Verify we didn't land on Clear All DTCs popup — soft check
                out.update(ok=True, method="adb_text")
                self._step("Tapped Report (ADB text)", 0.972)
                return out
        except Exception:
            pass

        # Layout: Report sits LEFT of Clear All DTCs on the footer row
        x, y = int(w * 0.42), int(h * 0.91)
        self._adb_tap(x, y)
        out.update(ok=True, point=(x, y), method="layout")
        self._step(f"Tapped Report (layout left-of-Clear) at ({x},{y})", 0.972)
        return out

    def _adb_screen_blob(self) -> str:
        try:
            return " ".join(self._adb_ui_texts()).lower()
        except Exception:
            return ""

    def _wait_screen_any(self, *needles: str, timeout: float = 20.0) -> bool:
        """Poll until any needle appears (u2 or ADB dump)."""
        deadline = time.time() + timeout
        lowered = tuple(n.lower() for n in needles if n)
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self._u2_has_text(*needles[:2], timeout=0.08):
                return True
            blob = self._adb_screen_blob()
            if any(n in blob for n in lowered):
                return True
            time.sleep(0.25)
        return False

    def _tap_label_adb_or_layout(
        self,
        label: str,
        fallback: Optional[Tuple[float, float]] = None,
        *,
        min_y_ratio: float = 0.0,
    ) -> bool:
        """Tap an exact/contains label via ADB text, XML, then optional layout."""
        try:
            if adb.find_and_tap_text(self.serial, label, timeout=3):
                self._step(f"Tapped '{label}' (ADB text)")
                return True
        except Exception:
            pass
        xml = self._ui_xml(timeout=1.5)
        hit = self._tap_label_from_xml(xml, (label,), min_y_ratio=min_y_ratio)
        if hit:
            self._step(f"Tapped '{label}' (XML) at {hit[1:]}")
            return True
        if fallback:
            w, h = self._window_size()
            x, y = int(w * fallback[0]), int(h * fallback[1])
            self._adb_tap(x, y)
            self._step(f"Tapped '{label}' (layout) at ({x},{y})")
            return True
        return False

    def send_scan_report_via_gmail(self, to_email: Optional[str] = None) -> Dict[str, object]:
        """Exact post-scan report flow:

        1. Tap Report (Topology footer)
        2. Report Information → OK
        3. More Information → OK
        4. X431 Inspection Report → Other Share
        5. Share sheet → Gmail
        6. Compose → recipient → Send
        7. Back to System and Function / Topology
        """
        dest = (to_email or getattr(self, "report_email", "") or REPORT_EMAIL).strip() or REPORT_EMAIL
        out: Dict[str, object] = {
            "ok": False,
            "emailed_to": dest,
            "engineer": getattr(self, "engineer", "") or "",
            "error": None,
            "returned_to_topology": False,
        }

        # ── 1) Report ──────────────────────────────────────────────
        self._step("Step 1/7 — Tap Report (Topology footer)", 0.972)
        tapped = self.tap_topology_report()
        if not tapped.get("ok"):
            out["error"] = tapped.get("error") or "Could not tap Report"
            self._step(out["error"])
            return out
        time.sleep(0.55)

        if not self._wait_screen_any("Report Information", "More Information", timeout=10):
            self._step("Report popup not seen — retrying Report tap", 0.973)
            self.tap_topology_report()
            time.sleep(0.6)

        # ── 2) Report Information → OK ─────────────────────────────
        self._step("Step 2/7 — Report Information → OK", 0.978)
        if not self._wait_screen_any("Report Information", timeout=14):
            blob = self._adb_screen_blob()
            # Some builds skip straight to More Information
            if "more information" not in blob:
                out["error"] = "Step 2 failed: Report Information popup did not appear"
                self._step(out["error"])
                return out
        else:
            self._tap_ok_preferred()
            time.sleep(0.4)

        # ── 3) More Information → OK ───────────────────────────────
        self._step("Step 3/7 — More Information → OK", 0.982)
        if self._wait_screen_any("More Information", timeout=14):
            self._tap_ok_preferred()
            time.sleep(0.45)
        else:
            blob = self._adb_screen_blob()
            if "more information" in blob:
                self._tap_ok_preferred()
                time.sleep(0.4)
            else:
                self._step("More Information not shown — continuing to Inspection Report", 0.983)

        # ── 4) Other Share on X431 Inspection Report ───────────────
        self._step("Step 4/7 — X431 Inspection Report → Other Share", 0.988)
        if not self._wait_screen_any(
            "Other Share", "X431 Inspection Report", "Inspection Report", timeout=25
        ):
            out["error"] = "Step 4 failed: X431 Inspection Report / Other Share not visible"
            self._step(out["error"])
            return out
        if not self._tap_label_adb_or_layout(
            "Other Share", POINT_OTHER_SHARE, min_y_ratio=0.55
        ):
            out["error"] = "Step 4 failed: could not tap Other Share"
            self._step(out["error"])
            return out
        time.sleep(0.5)

        # ── 5) Gmail on share sheet ────────────────────────────────
        self._step("Step 5/7 — Share sheet → Gmail", 0.992)
        if not self._wait_screen_any("Gmail", timeout=14):
            out["error"] = "Step 5 failed: Gmail not on share sheet"
            self._step(out["error"])
            return out
        if not self._tap_label_adb_or_layout("Gmail", POINT_SHARE_GMAIL, min_y_ratio=0.20):
            out["error"] = "Step 5 failed: could not tap Gmail"
            self._step(out["error"])
            return out
        time.sleep(0.25)

        # ── 6) Compose + To + Send (must leave compose before success) ──
        self._step("Step 6/7 — Gmail compose → To → Send", 0.996)
        if not self._wait_gmail_compose(timeout=14):
            blob = self._adb_screen_blob()
            if not self._gmail_compose_visible(blob):
                out["error"] = "Step 6 failed: Gmail compose screen did not open"
                self._step(out["error"])
                return out

        # VIN audit: use cached VIN — skip slow dump on the hot path
        if self.detected_vin and self.detected_vin != "UNKNOWN":
            out["vin"] = self.detected_vin
        else:
            try:
                out["vin"] = self.capture_and_save_vin_audit(source="gmail_report_compose")
            except Exception:
                out["vin"] = self.detected_vin or "UNKNOWN"

        to_email = dest
        out["emailed_to"] = to_email
        self._step(f"Step 6b — Fill To: {to_email}", 0.996)
        filled = self._ensure_gmail_recipient(to_email)
        if not filled:
            self._step("To not confirmed — retrying once")
            filled = self._ensure_gmail_recipient(to_email)
        blob = self._adb_screen_blob()
        self._dismiss_invalid_email_dialog(blob)
        if blob and not self._gmail_to_is_correct_fast(to_email, blob):
            out["error"] = f"Gmail To field does not show {to_email} — not sending"
            self._step(out["error"])
            return out

        self._gmail_dismiss_overlays()
        if not self._gmail_send_and_confirm(to_email, timeout=24.0):
            out["error"] = "Gmail Send did not complete — tablet still on compose"
            self._step(out["error"])
            return out

        # ── 7) Back to Topology (only after send is confirmed) ─────
        self._step("Step 7/7 — Back to System and Function / Topology", 0.998)
        back = self.return_to_system_topology_after_email(timeout=22)
        out["returned_to_topology"] = bool(back)
        out["ok"] = True
        if back:
            self._step(f"Report emailed to {to_email} — back on Topology", 1.0)
        else:
            self._step(
                f"Report emailed to {to_email} — leave Inspection Report with tablet Back if needed",
                1.0,
            )
        return out

    def _tap_ok_preferred(self) -> bool:
        """Tap OK via ADB text / bounds — avoid blind layout when possible."""
        try:
            if adb.find_and_tap_text(self.serial, "OK", timeout=3):
                self._step("Tapped OK (ADB text)")
                return True
        except Exception:
            pass
        xml = self._ui_xml(timeout=1.4)
        if self.tap_ok_not_cancel(xml):
            self._step("Tapped OK (XML / layout)")
            return True
        self._tap_text_now("OK", POINT_DIALOG_OK)
        self._step("Tapped OK (fixed layout)")
        return True

    def press_android_back(self) -> None:
        """Tablet nav-bar Back (KEYCODE_BACK)."""
        try:
            adb._run_adb(
                ["-s", self.serial, "shell", "input", "keyevent", "KEYCODE_BACK"],
                timeout=3,
            )
        except Exception:
            try:
                self.ensure_device().press("back")
            except Exception:
                pass

    def is_system_function_topology(self) -> bool:
        """True on System and Function / Topology (footer Report / Clear All DTCs)."""
        if self._u2_has_text("X431 Inspection Report", timeout=0.05):
            return False
        if self._u2_has_text("System Topology", "System and Function", timeout=0.08):
            return True
        if self._u2_has_text("Clear All DTCs", timeout=0.08):
            return True
        blob = self._adb_screen_blob()
        if "x431 inspection report" in blob or "other share" in blob:
            return False
        if "system and function" in blob or "system topology" in blob:
            return True
        if "clear all dtcs" in blob and "report" in blob:
            return True
        return False

    def return_to_system_topology_after_email(self, timeout: float = 28.0) -> bool:
        """After Gmail send: land on Inspection Report → Back → Topology."""
        self._step("Waiting for X431 Inspection Report after send…", 0.997)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self.is_system_function_topology():
                self._step("Already on System and Function / Topology", 1.0)
                return True
            blob = self._adb_screen_blob()
            if "x431 inspection report" in blob or "other share" in blob:
                break
            if self._gmail_compose_visible(blob):
                self._step("Still in Gmail compose — send not confirmed, not backing out of draft")
                return False
            if "inbox" in blob or "conversation" in blob or "gmail" in blob:
                self._step("Gmail after send — tapping Back toward Inspection Report")
                self.press_android_back()
                time.sleep(0.55)
                continue
            time.sleep(0.2)

        if self.is_system_function_topology():
            return True

        self._step("On Inspection Report — tapping tablet Back", 0.998)
        self.press_android_back()
        time.sleep(0.45)

        for _ in range(4):
            if self.is_system_function_topology() or self._ecu_scan_done_visible():
                self._step("Back on System and Function / Topology", 1.0)
                return True
            blob = self._adb_screen_blob()
            if "clear all dtcs" in blob and "report" in blob:
                self._step("Back on System and Function / Topology", 1.0)
                return True
            if "x431 inspection report" in blob or "gmail" in blob:
                self._step("Still not on Topology — one more Back")
                self.press_android_back()
                time.sleep(0.45)
                continue
            time.sleep(0.3)

        ok = self.is_system_function_topology() or self._ecu_scan_done_visible()
        if ok:
            self._step("Back on System and Function / Topology", 1.0)
        return ok

    def require_system_function_topology(self) -> Optional[str]:
        """Return an error string if the tablet is not on Topology."""
        if self.is_system_function_topology():
            return None
        return (
            "Tablet is not on System and Function / Topology. "
            "Open that page first (after a scan), then use this button."
        )

    def resend_topology_report(self, to_email: Optional[str] = None) -> Dict[str, object]:
        """Dashboard Report button: Topology → report workflow → email → Topology."""
        err = self.require_system_function_topology()
        if err:
            self._step(err)
            return {"ok": False, "error": err}
        dest = (to_email or self.report_email or REPORT_EMAIL).strip() or REPORT_EMAIL
        return self.send_scan_report_via_gmail(dest)

    def clear_all_dtcs(self) -> Dict[str, object]:
        """Dashboard Clear All DTCs: only taps when already on Topology."""
        out: Dict[str, object] = {"ok": False, "error": None}
        err = self.require_system_function_topology()
        if err:
            self._step(err)
            out["error"] = err
            return out
        self._step("Tapping Clear All DTCs…", 0.5)
        if not self._tap_label_adb_or_layout(
            "Clear All DTCs", POINT_CLEAR_ALL_DTCS, min_y_ratio=0.70
        ):
            self._tap_text_now("Clear All DTCs", POINT_CLEAR_ALL_DTCS)
        time.sleep(0.25)
        for label in ("OK", "Confirm", "Yes"):
            blob = self._adb_screen_blob()
            if label.lower() in blob or self._u2_has_text(label, timeout=0.1):
                self._step(f"Confirming Clear All DTCs ({label})")
                self._tap_ok_preferred() if label == "OK" else self._tap_label_adb_or_layout(
                    label, POINT_DIALOG_OK
                )
                time.sleep(0.2)
                break
        out["ok"] = True
        self._step("Clear All DTCs tapped", 1.0)
        return out

    def _gmail_compose_visible(self, blob: str = "") -> bool:
        """True while Gmail compose (draft) is on screen — not Inspection Report / Topology."""
        low = (blob or self._adb_screen_blob()).lower()
        if not low:
            return False
        if "x431 inspection report" in low or "other share" in low:
            return False
        if "system and function" in low or "system topology" in low:
            return False
        if "clear all dtcs" in low and "report" in low and "diagnostic report" not in low:
            return False
        if any(x in low for x in ("reply all", "reply-all", "inbox", "primary", "archive")):
            return False
        if "is invalid" in low:
            return True
        if "diagnostic report" in low:
            return True
        if "compose" in low:
            return True
        if "gmail" in low and "to" in low and ("from" in low or "subject" in low):
            return True
        if "send" in low and "@" in low and "inbox" not in low:
            return True
        return False

    def _gmail_left_compose(self, blob: str = "") -> bool:
        """True when compose is gone after a successful send (report, topology, or inbox)."""
        low = (blob or self._adb_screen_blob()).lower()
        if not low:
            return False
        if self._gmail_compose_visible(low):
            return False
        if "x431 inspection report" in low or "other share" in low:
            return True
        if "system and function" in low or "system topology" in low:
            return True
        if "clear all dtcs" in low and "report" in low:
            return True
        if any(x in low for x in ("inbox", "primary", "reply all", "archive")):
            return True
        return False

    def _gmail_dismiss_overlays(self) -> None:
        """Close suggestion list / IME so the Send arrow is tappable."""
        w, h = self._window_size()
        self._adb_tap(int(w * 0.50), int(h * 0.40))
        time.sleep(0.2)
        try:
            self._adb_input("input", "keyevent", "111")  # KEYCODE_ESCAPE
        except Exception:
            pass
        time.sleep(0.12)

    def _gmail_send_and_confirm(self, to_email: str, timeout: float = 24.0) -> bool:
        """Tap Send and wait until compose actually closes. Retry if it stays open."""
        self._step("Tapping Gmail Send — waiting until compose closes", 0.997)
        self._tap_gmail_send()
        send_taps = 1
        deadline = time.time() + timeout
        last_tap = time.time()
        while time.time() < deadline:
            self.raise_if_cancelled()
            blob = self._adb_screen_blob()
            if self._dismiss_invalid_email_dialog(blob):
                self._ensure_gmail_recipient(to_email)
                self._gmail_dismiss_overlays()
                self._tap_gmail_send()
                send_taps += 1
                last_tap = time.time()
                time.sleep(1.0)
                continue
            if self._gmail_left_compose(blob):
                self._step("Gmail compose closed — send confirmed", 0.997)
                return True
            still = self._gmail_compose_visible(blob)
            if send_taps < 5 and (time.time() - last_tap) >= 1.6 and (still or not blob):
                self._step(f"Still on compose — Send retry {send_taps + 1}/5")
                self._gmail_dismiss_overlays()
                self._tap_gmail_send()
                send_taps += 1
                last_tap = time.time()
            time.sleep(0.45)
        blob = self._adb_screen_blob()
        if self._gmail_left_compose(blob):
            self._step("Gmail compose closed — send confirmed", 0.997)
            return True
        self._step("Gmail still on compose after Send retries — not marking emailed")
        return False

    def _wait_gmail_compose(self, timeout: float = 14.0) -> bool:
        """Wait until Gmail compose is actually on screen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self._u2_has_text("Diagnostic Report", timeout=0.04):
                return True
            blob = self._adb_screen_blob()
            if self._gmail_compose_visible(blob):
                return True
            time.sleep(0.2)
        return False

    def _gmail_to_is_correct_fast(self, to_email: str, blob: str = "") -> bool:
        """Cheap To check from one blob (no extra dumps)."""
        want = (to_email or REPORT_EMAIL).strip().lower()
        low = (blob or self._adb_screen_blob()).lower()
        if "%40" in low or "is invalid" in low:
            return False
        if want and want in low:
            return True
        if want == REPORT_EMAIL.lower():
            if "hotline.support@lkqbelgium.be" in low:
                return True
            if "hotline.support" in low and "lkqbelgium" in low:
                return True
            if "lkq support" in low:
                return True
        return False

    def _dismiss_invalid_email_dialog(self, blob: str = "") -> bool:
        """Dismiss Gmail 'address … is invalid' popup if present."""
        low = (blob or "").lower() or self._adb_screen_blob()
        if "is invalid" not in low and "%40" not in low:
            return False
        self._step("Invalid To dialog — OK")
        try:
            adb.find_and_tap_text(self.serial, "OK", timeout=1)
        except Exception:
            w, h = self._window_size()
            self._adb_tap(int(w * 0.62), int(h * 0.58))
        time.sleep(0.12)
        return True

    def _tap_correct_gmail_suggestion(self, to_email: str) -> bool:
        """Tap Suggestions row for hotline only — never yayra / diagnostics / You."""
        want = (to_email or REPORT_EMAIL).strip().lower()
        local = want.split("@", 1)[0].lower()
        w, h = self._window_size()
        min_y, max_y = int(h * 0.18), int(h * 0.72)
        best: Optional[Tuple[int, int, int, str]] = None
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip()
                low = raw.lower()
                if not low or "%40" in low:
                    continue
                if any(
                    bad in low
                    for bad in ("yayra", "osias", "sergoynediag", "diagnostics@")
                ) and want not in low:
                    continue
                if low in {"you", "suggestions", "to", "from", "lkq support"}:
                    # Prefer the email line, not the name-only label
                    if "@" not in low:
                        continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                x, y = (l + r) // 2, (t + b) // 2
                if y < min_y or y > max_y:
                    continue
                score = 0
                if want and want in low:
                    score = 100
                elif want == REPORT_EMAIL.lower() and "hotline.support@lkqbelgium.be" in low:
                    score = 100
                elif want == REPORT_EMAIL.lower() and "hotline.support" in low and "lkqbelgium" in low:
                    score = 90
                elif local and local in low and "@" in low and want.split("@")[-1] in low:
                    score = 80
                else:
                    continue
                if "@" in low:
                    score += 10
                if best is None or score > best[0]:
                    best = (score, x, y, raw)
        except Exception:
            return False
        if not best:
            return False
        _s, x, y, raw = best
        self._adb_tap(x, y)
        self._step(f"Tapped hotline suggestion: {raw[:70]}")
        time.sleep(0.12)
        return True

    def _commit_gmail_to_with_enter(self) -> None:
        """Enter commits To — Send arrow turns blue."""
        try:
            adb._run_adb(
                ["-s", self.serial, "shell", "input", "keyevent", "66"],
                timeout=2,
            )
        except Exception:
            pass
        time.sleep(0.18)

    def _adb_input(self, *parts: str, timeout: int = 4) -> None:
        adb._run_adb(["-s", self.serial, "shell", *parts], timeout=timeout)

    def _adb_type_email(self, email: str) -> bool:
        """Type the full address in segments so ADB does not truncate or print %40.

        hotline + . + support + @ + lkqbelgium + . + be
        """
        email = (email or "").strip()
        if not email:
            return False
        # Split on . and @ so each `input text` is a simple alnum token
        tokens: List[str] = []
        buf = ""
        for ch in email:
            if ch in ".@":
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
            else:
                buf += ch
        if buf:
            tokens.append(buf)

        try:
            for tok in tokens:
                if tok == ".":
                    self._adb_input("input", "keyevent", "56")  # KEYCODE_PERIOD
                elif tok == "@":
                    self._adb_input("input", "keyevent", "77")  # KEYCODE_AT
                else:
                    self._adb_input("input", "text", tok)
                time.sleep(0.04)
            self._step(f"Typed To: {email}")
            return True
        except Exception as exc:
            self._step(f"ADB type email failed: {exc}")
            return False

    def _focus_gmail_to_field(self) -> None:
        """Tap To field — right of the To label (into the value)."""
        w, h = self._window_size()
        self._adb_tap(int(w * 0.32), int(h * 0.16))
        time.sleep(0.12)

    def _clear_gmail_to_field(self) -> None:
        """Clear leftover typed text (keep existing LKQ Support chip if possible)."""
        try:
            # Move to end, delete residual typed chars (not 50 round-trips)
            self._adb_input("input", "keyevent", "KEYCODE_MOVE_END")
            for _ in range(12):
                self._adb_input("input", "keyevent", "67")  # DEL
        except Exception:
            pass

    def _ensure_gmail_recipient(self, to_email: str) -> bool:
        """Fill To, commit the chip, confirm the address is on screen."""
        to_email = (to_email or REPORT_EMAIL).strip() or REPORT_EMAIL
        blob = self._adb_screen_blob()
        self._dismiss_invalid_email_dialog(blob)

        already = self._gmail_to_is_correct_fast(to_email, blob)
        if already and "%40" not in (blob or "").lower():
            self._step(f"To already has {to_email} — committing chip")
            self._commit_gmail_to_with_enter()
            return True

        self._step(f"Filling To: {to_email}")
        self._focus_gmail_to_field()
        time.sleep(0.15)
        self._clear_gmail_to_field()
        if not self._adb_type_email(to_email):
            self._commit_gmail_to_with_enter()
            return False

        time.sleep(0.45)
        if not self._tap_correct_gmail_suggestion(to_email):
            self._commit_gmail_to_with_enter()
        else:
            time.sleep(0.15)
            self._commit_gmail_to_with_enter()
        time.sleep(0.35)
        blob = self._adb_screen_blob()
        self._dismiss_invalid_email_dialog(blob)
        if not blob:
            self._step("UI dump empty after To fill — will try Send anyway")
            return True
        ok = self._gmail_to_is_correct_fast(to_email, blob)
        if ok:
            self._step(f"To confirmed: {to_email}")
        else:
            self._step(f"To not confirmed after typing {to_email}")
        return ok

    def _tap_gmail_send(self) -> None:
        """Tap the Send paper-plane in the top action bar — not paperclip, not ⋮."""
        w, h = self._window_size()
        tapped = False
        try:
            for el in adb.get_ui_elements(self.serial):
                desc = (el.get("content_desc") or "").strip().lower()
                text = (el.get("text") or "").strip().lower()
                rid = (el.get("resource_id") or "").lower()
                hay = f"{desc} {text} {rid}"
                if "send" not in hay:
                    continue
                if any(bad in hay for bad in ("sender", "resend", "sending", "newsletter")):
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                x, y = (l + r) // 2, (t + b) // 2
                if y > int(h * 0.22):
                    continue
                if x < int(w * 0.58):
                    continue
                self._adb_tap(x, y)
                self._step(f"Tapped Gmail Send at ({x},{y})")
                tapped = True
                break
        except Exception:
            pass
        if not tapped:
            x, y = int(w * POINT_GMAIL_SEND[0]), int(h * POINT_GMAIL_SEND[1])
            self._adb_tap(x, y)
            self._step(f"Tapped Gmail Send (layout) at ({x},{y})")
            time.sleep(0.15)
            self._adb_tap(int(w * 0.91), int(h * 0.055))
            self._adb_tap(int(w * 0.86), int(h * 0.06))
        time.sleep(0.35)

    def handle_common_dialogs(self, max_rounds: int = 4) -> List[str]:
        """Dismiss VAG Diagnostic Firewall first, then generic Launch popups."""
        dismissed: List[str] = []
        if self.dismiss_diagnostic_firewall():
            dismissed.append("Diagnostic Firewall OK")
        try:
            more = super().handle_common_dialogs(max_rounds=max_rounds)
            dismissed.extend(more or [])
        except Exception:
            pass
        return dismissed

    def dismiss_until_stable(self, rounds: int = 5) -> None:
        for _ in range(rounds):
            if self.dismiss_diagnostic_firewall():
                time.sleep(0.35)
                continue
            hit = self.handle_common_dialogs(max_rounds=2)
            if not hit:
                break
            time.sleep(0.4)

    def _foreground_package(self) -> str:
        """Fast ADB check for the current foreground package (no UI dump)."""
        try:
            proc = adb._run_adb(
                ["-s", self.serial, "shell", "dumpsys", "activity", "activities"],
                timeout=4,
            )
            out = proc.stdout or ""
            for key in ("mResumedActivity", "mFocusedActivity", "topResumedActivity"):
                for line in out.splitlines():
                    if key in line and "com.cnlaunch" in line:
                        m = re.search(r"(com\.[\w.]+)/", line)
                        if m:
                            return m.group(1)
        except Exception:
            pass
        try:
            cur = self.ensure_device().app_current() or {}
            return str(cur.get("package") or "")
        except Exception:
            return ""

    def ensure_app_open_fast(self) -> None:
        """Skip launch if home/mid-flow is already up — no extra waits."""
        try:
            self.ensure_device()
        except Exception as exc:
            self._step(f"u2 connect skipped: {exc} — continuing with ADB")
        if self._u2_has_text("Intelligent Diagnose", "AutoDetect Result", timeout=0.12):
            self._step("EURO LINK already open — skipping launch")
            return
        self._step("Opening EURO LINK (ADB)", 0.08)
        try:
            msg = adb.launch_x431(self.serial)
            self._step(msg, 0.12)
        except Exception as exc:
            self._step(f"Launch skipped: {exc}")
        time.sleep(0.35)

    def tap_intelligent_diagnose_fast(self) -> Dict[str, object]:
        """Tap Intelligent Diagnose immediately — one ADB tap, no confirm loops."""
        result: Dict[str, object] = {
            "ok": False,
            "method": None,
            "point": None,
            "error": None,
            "brand": self.preferred_brand,
        }
        self._step("Tapping Intelligent Diagnose…", 0.2)
        w, h = self._window_size()
        x, y = int(w * 0.17), int(h * 0.28)
        self._adb_tap(x, y)
        result.update(ok=True, method="layout", point=(x, y))
        self._step(f"Tapped Intelligent Diagnose (ADB) at ({x},{y})", 0.4)
        return result

    def _blob_is_local_diagnose(self, blob: str) -> bool:
        """True for the Local Diagnose brand-search page — not the home tile."""
        low = (blob or "").lower()
        if not low:
            return False
        if "autodetect result" in low or "auto detect result" in low:
            return False
        if "enter the model" in low or "please enter the model" in low:
            return True
        if "enter the vehicle" in low or "vinscan" in low:
            return True
        # Home grid shows both Intelligent Diagnose + Local Diagnose tiles.
        if "intelligent diagnose" in low:
            return False
        has_region = any(tab in low for tab in ("europe", "asia", "america", "china"))
        has_brand = any(
            name in low
            for name in ("volkswagen", "renault", "audi", "toyota", "ford", "bmw", "fiat")
        )
        if has_region and has_brand:
            return True
        if "passenger car" in low and has_brand:
            return True
        return False

    def is_local_diagnose_page(self) -> bool:
        """True when Local Diagnose brand grid / search is visible (not home tile)."""
        try:
            blob = " ".join(self._adb_ui_texts()).lower()
        except Exception:
            blob = ""
        if blob and self._blob_is_local_diagnose(blob):
            return True
        if self._u2_has_text("Enter the model name", timeout=0.08):
            return True
        if self._u2_has_text("VINScan Service", timeout=0.06):
            return True
        return False

    def is_eurolink_home_grid(self) -> bool:
        """True only on the home tile grid (Intelligent Diagnose + Local Diagnose)."""
        try:
            blob = " ".join(self._adb_ui_texts()).lower()
        except Exception:
            blob = ""
        if self._blob_is_local_diagnose(blob):
            return False
        if "intelligent diagnose" in blob and (
            "local diagnose" in blob or "service function" in blob
        ):
            return True
        return False

    def wait_for_local_diagnose(self, timeout: float = 20.0) -> bool:
        """After AutoDetect fails the tablet opens Local Diagnose itself — wait, do not go home."""
        if self.is_local_diagnose_page():
            self._step("Already on Local Diagnose — continuing from here", 0.22)
            return True
        self._step(
            "AutoDetect not shown — waiting for Local Diagnose (tablet opens it automatically)",
            0.2,
        )
        deadline = time.time() + timeout
        last_hb = 0.0
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self.is_local_diagnose_page():
                self._step("Local Diagnose is on screen — continuing (no home tap)", 0.22)
                return True
            now = time.time()
            if now - last_hb >= 4:
                self._step(
                    f"Waiting for Local Diagnose… {int(deadline - now)}s left",
                    0.21,
                )
                last_hb = now
            time.sleep(0.25)
        return self.is_local_diagnose_page()

    def ensure_local_diagnose(self) -> bool:
        """Stay on Local Diagnose when AutoDetect is skipped. Do not re-select it on home."""
        if self.wait_for_local_diagnose(timeout=18.0):
            return True
        if self.is_eurolink_home_grid():
            self._step("Still on EURO LINK home — tapping Local Diagnose tile", 0.2)
            return self.open_local_diagnose()
        # Dump often misses the page while it is already open — search on this screen.
        self._step(
            "Local Diagnose not confirmed in UI dump — continuing brand search on current screen",
            0.22,
        )
        return True

    def brand_search_terms(self, brand: Optional[str] = None) -> Sequence[str]:
        """Return Local Diagnose search/tile labels for the chosen brand."""
        key = (brand or self.preferred_brand or "Volkswagen").strip()
        return local_diagnose_search_terms(key)

    def open_local_diagnose(self) -> bool:
        """Open Local Diagnose from EURO LINK home — ADB-first (works without u2)."""
        if self.is_local_diagnose_page():
            self._step("Already on Local Diagnose")
            return True
        self.ensure_app_open_fast()
        self._step("Opening Local Diagnose (manual brand selection)", 0.2)

        # 1) ADB text tap (preferred when u2 is down)
        for label in HOME_MANUAL_DIAGNOSE:
            try:
                if adb.find_and_tap_text(self.serial, label, timeout=3):
                    self._step(f"Tapped '{label}' (ADB)", 0.21)
                    time.sleep(0.7)
                    if self.is_local_diagnose_page():
                        return True
            except Exception:
                continue

        # 2) Soft u2 / OCR path (may no-op when u2 failed)
        try:
            if self._click_by_text(HOME_MANUAL_DIAGNOSE, timeout=4):
                time.sleep(0.7)
                if self.is_local_diagnose_page():
                    return True
        except Exception as exc:
            self._step(f"Local Diagnose text click skipped: {exc}")

        # 3) Layout: tile to the right of Intelligent Diagnose
        w, h = self._window_size()
        for rx, ry in LOCAL_DIAGNOSE_HOME_POINTS:
            x, y = int(w * rx), int(h * ry)
            self._adb_tap(x, y)
            self._step(f"Tapped Local Diagnose (layout) at ({x},{y})", 0.21)
            time.sleep(0.75)
            if self.is_local_diagnose_page():
                return True

        self._step("Could not open Local Diagnose")
        return False

    def _focus_local_diagnose_search(self) -> None:
        """Tap the Local Diagnose search field (top-right)."""
        w, h = self._window_size()
        focused = False
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip().lower()
                rid = (el.get("resource_id") or "").lower()
                if (
                    "enter the model" not in raw
                    and "model name" not in raw
                    and "search" not in rid
                    and "edit" not in rid
                ):
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                y = (t + b) // 2
                if y > int(h * 0.22):
                    continue
                self._adb_tap((l + r) // 2, y)
                focused = True
                break
        except Exception:
            pass
        if not focused:
            self._adb_tap(int(w * 0.82), int(h * 0.08))
        time.sleep(0.12)

    def _clear_local_diagnose_search(self) -> None:
        """Always wipe the search bar — leftover brands from prior jobs stay there."""
        self._step("Clearing Local Diagnose search bar (old brand if saved)", 0.24)
        w, h = self._window_size()
        self._focus_local_diagnose_search()

        # Tap the field's trailing X / clear icon if present
        try:
            for el in adb.get_ui_elements(self.serial):
                raw = (el.get("text") or el.get("content_desc") or "").strip().lower()
                rid = (el.get("resource_id") or "").lower()
                if not any(k in f"{raw} {rid}" for k in ("clear", "delete", "close")):
                    continue
                match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
                if not match:
                    continue
                l, t, r, b = (int(match.group(i)) for i in range(1, 5))
                y = (t + b) // 2
                if y > int(h * 0.22):
                    continue
                self._adb_tap((l + r) // 2, y)
                time.sleep(0.1)
                break
        except Exception:
            pass
        # Trailing X on the search row (top-right)
        self._adb_tap(int(w * 0.96), int(h * 0.08))
        time.sleep(0.08)
        self._focus_local_diagnose_search()

        try:
            adb._run_adb(
                [
                    "-s",
                    self.serial,
                    "shell",
                    "sh",
                    "-c",
                    "input keyevent 123; "
                    "i=0; while [ $i -lt 40 ]; do input keyevent 67; i=$((i+1)); done",
                ],
                timeout=5,
            )
        except Exception:
            try:
                adb._run_adb(
                    ["-s", self.serial, "shell", "input", "keyevent", "KEYCODE_MOVE_END"],
                    timeout=2,
                )
                for _ in range(20):
                    adb._run_adb(
                        ["-s", self.serial, "shell", "input", "keyevent", "67"],
                        timeout=2,
                    )
            except Exception:
                pass
        time.sleep(0.12)

    def _type_search_text(self, text: str) -> bool:
        """Focus Local Diagnose search, clear leftover brand, then type ``text``."""
        return self._type_search_text_fast(text)

    def _type_search_text_fast(self, text: str) -> bool:
        """Clear search first, then type the selected brand via ADB."""
        self._clear_local_diagnose_search()
        safe = re.sub(r"[^A-Za-z0-9 \-_]", "", text).strip()
        if not safe:
            return False
        self._focus_local_diagnose_search()
        try:
            adb._run_adb(
                ["-s", self.serial, "shell", "input", "text", safe.replace(" ", "%s")],
                timeout=3,
            )
            time.sleep(0.35)
            return True
        except Exception as exc:
            self._step(f"Search type failed: {exc}")
            return False

    def _confirm_local_brand_ok(self) -> bool:
        """After tapping the brand icon, confirm with OK if a popup appears."""
        deadline = time.time() + 4.0
        while time.time() < deadline:
            self.raise_if_cancelled()
            blob = self._adb_screen_blob()
            if any(
                k in blob
                for k in (
                    "system and function",
                    "system topology",
                    "automatically search",
                    "show menu",
                    "full system",
                    "autodetect",
                )
            ):
                return True
            if "ok" in blob or "continue" in blob or "confirm" in blob:
                self._step("Brand confirm popup — tapping OK", 0.42)
                try:
                    if adb.find_and_tap_text(self.serial, "OK", timeout=2):
                        time.sleep(0.25)
                        return True
                except Exception:
                    pass
                try:
                    if adb.find_and_tap_text(self.serial, "Continue", timeout=1):
                        time.sleep(0.25)
                        return True
                except Exception:
                    pass
                self._tap_ok_preferred()
                time.sleep(0.25)
                return True
            time.sleep(0.15)
        return False

    def search_and_select_brand(self, brand: Optional[str] = None) -> Dict[str, object]:
        """Clear search → type selected brand → tap brand icon → OK."""
        result: Dict[str, object] = {
            "ok": False,
            "search": None,
            "tapped": None,
            "error": None,
        }
        terms = list(self.brand_search_terms(brand))
        if not terms:
            terms = [str(brand or self.preferred_brand or "")]
        primary = terms[0]
        result["search"] = primary
        self._step(f"Local Diagnose — clear search, then type {primary}", 0.25)

        if not self._type_search_text_fast(primary):
            result["error"] = "Could not type into Local Diagnose search bar"
            self._step(result["error"])
            return result

        time.sleep(0.4)

        # Tap the filtered brand icon / tile (exact label first)
        tapped = False
        for label in terms:
            try:
                if adb.find_and_tap_text(self.serial, label, timeout=3):
                    result.update(ok=True, tapped=label, method="adb_text")
                    self._step(f"Tapped brand icon (ADB): {label}", 0.4)
                    tapped = True
                    break
            except Exception:
                continue

        if not tapped and not self._u2_failed:
            try:
                device = self.ensure_device()
                for label in terms:
                    node = device(text=label)
                    exists = self._u2_rpc(
                        lambda n=node: n.exists(timeout=0.12), timeout=0.28, default=False
                    )
                    if exists:
                        pt = self._tap_node_center(node)
                        result.update(ok=True, tapped=label, point=pt, method="u2")
                        self._step(f"Tapped brand icon: {label}", 0.4)
                        tapped = True
                        break
            except Exception:
                self._u2_failed = True

        if not tapped:
            w, h = self._window_size()
            # First filtered brand icon after search is top-left of the grid
            x, y = int(w * 0.14), int(h * 0.30)
            self._adb_tap(x, y)
            result.update(ok=True, tapped=primary, method="layout")
            self._step(f"Tapped brand icon (layout) for {primary} at ({x},{y})", 0.4)
            tapped = True

        time.sleep(0.3)
        self._confirm_local_brand_ok()
        result["ok"] = True
        result["tapped"] = result.get("tapped") or primary
        return result

    def continue_after_local_brand(self, timeout: Optional[float] = None) -> str:
        """After brand icon + OK: wait for Diagnostic, topology, or brand Show Menu."""
        if timeout is None:
            if has_renault_auto_search(self.preferred_brand) or has_toyota_auto_search(
                self.preferred_brand
            ):
                timeout = 22.0
            else:
                timeout = 3.5
        deadline = time.time() + timeout
        ok_taps = 0
        while time.time() < deadline:
            self.raise_if_cancelled()
            if self.is_system_function_page():
                return "topology"
            if self.is_renault_show_menu() or self._renault_yes_no_popup():
                self._step("Renault Show Menu after Local Diagnose brand", 0.5)
                return "renault_menu"
            if self.is_toyota_show_menu():
                self._step("Toyota/Lexus Show Menu after Local Diagnose brand", 0.5)
                return "toyota_menu"
            if self.autodetect_result_visible():
                self.tap_diagnostic()
                return "diagnostic"
            try:
                blob = self._adb_screen_blob()
            except Exception:
                blob = ""
            if ok_taps < 3 and ("ok" in blob or "continue" in blob):
                if not any(
                    k in blob
                    for k in (
                        "automatically search",
                        "automatic search",
                        "system and function",
                        "gmail",
                    )
                ):
                    ok_taps += 1
                    self._step("Tapping OK after Local Diagnose brand", 0.45)
                    try:
                        adb.find_and_tap_text(self.serial, "OK", timeout=2)
                    except Exception:
                        self._tap_ok_preferred()
                    time.sleep(0.25)
                    continue
            if "full system" in blob:
                try:
                    if adb.find_and_tap_text(self.serial, "Full System", timeout=2):
                        self._step("Tapped Full System (ADB)", 0.5)
                except Exception:
                    pass
            time.sleep(0.12)
        return "wait_topology"

    def local_diagnose_brand_fallback(self) -> Dict[str, object]:
        """Continue from Local Diagnose (tablet opens it after AutoDetect fails)."""
        out: Dict[str, object] = {
            "ok": False,
            "method": "local_diagnose",
            "error": None,
            "tapped": None,
        }
        try:
            if not self.is_local_diagnose_page():
                if not self.ensure_local_diagnose():
                    out["error"] = "Could not continue on Local Diagnose for brand selection"
                    self._step(out["error"])
                    return out

            self._step(
                f"Local Diagnose — clear search, type {self.preferred_brand}, tap icon, OK",
                0.18,
            )
            pick = self.search_and_select_brand(self.preferred_brand)
            out["tapped"] = pick.get("tapped")
            if not pick.get("ok"):
                out["error"] = pick.get("error") or "Brand selection failed"
                return out

            out["ok"] = True
            self.detected_make = str(pick.get("tapped") or self.preferred_brand)
            self._step(f"Local Diagnose brand selected: {self.detected_make}", 0.45)
            wait = (
                22.0
                if (
                    has_renault_auto_search(self.preferred_brand)
                    or has_toyota_auto_search(self.preferred_brand)
                )
                else 3.5
            )
            out["next"] = self.continue_after_local_brand(timeout=wait)
            return out
        except Exception as exc:
            # Never surface raw u2 errors — keep ADB Local Diagnose path alive
            msg = str(exc)
            if "uiautomator2" in msg.lower():
                out["error"] = (
                    f"Local Diagnose fallback hit u2 gap — retry brand search via ADB ({msg})"
                )
                self._step(out["error"])
                self._u2_failed = True
                try:
                    pick = self.search_and_select_brand(self.preferred_brand)
                    if pick.get("ok"):
                        out["ok"] = True
                        out["error"] = None
                        out["tapped"] = pick.get("tapped")
                        self.detected_make = str(pick.get("tapped") or self.preferred_brand)
                        out["next"] = self.continue_after_local_brand()
                        return out
                except Exception as exc2:
                    out["error"] = f"Local Diagnose brand select failed: {exc2}"
                    self._step(out["error"])
                    return out
            out["error"] = msg
            self._step(f"local_diagnose_brand_fallback error: {exc}")
            return out

    def start_auto_detection(self, brand: Optional[str] = None) -> Dict[str, object]:
        """Brand-first auto detection through VIN audit + report email.

        Same process for any brand:
        1. Intelligent Diagnose → wait for AutoDetect Result
        2. If AutoDetect does not show → stay on Local Diagnose (tablet opens it) → select brand
        3. Diagnostic / Topology scan → report email
        """
        self.logs = []
        if brand:
            self.preferred_brand = brand
        self.detected_make = self.preferred_brand

        result: Dict[str, object] = {
            "ok": False,
            "brand": self.preferred_brand,
            "method": None,
            "point": None,
            "vin": None,
            "make": None,
            "model": None,
            "software": "",
            "diag_mode": "",
            "diagnostic_method": None,
            "scan_button": None,
            "emailed_to": None,
            "report_emailed": False,
            "error": None,
            **self.device_stamp(),
        }

        try:
            t0 = time.time()
            who = self.engineer or "—"
            dest = self.report_email or REPORT_EMAIL
            self._step(
                f"Start auto detection — {self.preferred_brand} · engineer={who} · report to {dest}",
                0.02,
            )
            if has_full_workflow(self.preferred_brand):
                self._step("Workflow: VAG full path (AutoDetect → Diagnostic → Topology scan)", 0.03)
            elif has_renault_auto_search(self.preferred_brand):
                self._step(
                    "Workflow: Renault path (Intelligent Diagnose or Local Diagnose search "
                    "→ Automatically Search → YES → YES → tap model → System and Function → "
                    "High-speed Scan or Smart Detection → report)",
                    0.03,
                )
            elif has_toyota_auto_search(self.preferred_brand):
                self._step(
                    "Workflow: Toyota/Lexus path (AutoDetect → Diagnostic → Show Menu → "
                    "Automatic Search (Europe and Other) → System and Function → "
                    "High-speed Scan or Smart Detection → report)",
                    0.03,
                )
            else:
                self._step(
                    f"Workflow: auto-detect + report for {self.preferred_brand} "
                    "(Intelligent Diagnose first; Local Diagnose brand select if AutoDetect fails)",
                    0.03,
                )
            self.raise_if_cancelled()
            self.preflight_device()
            self.raise_if_cancelled()
            self.ensure_app_open_fast()

            used_local = False
            autodetection_timeout = 45.0

            if self.autodetect_result_visible():
                self._step("Already on AutoDetect Result — reading VIN", 0.5)
                result["method"] = "already_on_result"
                detect = self.wait_autodetect_result(timeout=autodetection_timeout)
            elif self.is_local_diagnose_page():
                self._step("Local Diagnose on screen (no AutoDetect) — will search brand", 0.15)
                result["method"] = "local_diagnose_first"
                detect = {
                    "ok": False,
                    "saw_local_diagnose": True,
                    "error": "AutoDetect not shown — using Local Diagnose",
                }
            else:
                self._step("Tapping Intelligent Diagnose", 0.15)
                tap = self.tap_intelligent_diagnose_fast()
                result["method"] = tap.get("method")
                result["point"] = tap.get("point")
                detect = self.wait_autodetect_result(timeout=autodetection_timeout)

            vin = ""
            make = self.preferred_brand
            model = ""
            software = ""

            diag_done = False
            if detect.get("ok"):
                vin = str(detect.get("vin") or "")
                make = str(detect.get("make") or self.preferred_brand)
                model = str(detect.get("model") or "")
                software = str(detect.get("software") or "")
                result["diagnostic_method"] = detect.get("diagnostic_method")
                diag_done = bool(detect.get("diagnostic_tapped"))
            else:
                # Any brand: AutoDetect missed → Local Diagnose → select brand → continue
                self._step(
                    f"AutoDetect not reached — continuing on Local Diagnose for {self.preferred_brand}",
                    0.35,
                )
                fb = self.local_diagnose_brand_fallback()
                if not fb.get("ok"):
                    result["error"] = fb.get("error") or detect.get("error") or (
                        "AutoDetect failed and Local Diagnose brand select failed"
                    )
                    return result
                result["method"] = "local_diagnose_fallback"
                used_local = True
                make = self.detected_make or self.preferred_brand
                if fb.get("next") == "diagnostic":
                    diag_done = True
                    result["diagnostic_method"] = "layout"

            result["vin"] = vin or ""
            result["make"] = make
            result["model"] = model
            result["software"] = software
            self.detected_model = model
            self.detected_make = make

            if not diag_done and not used_local:
                self._step("Tapping Diagnostic…", 0.91)
                diag = self.tap_diagnostic()
                result["diagnostic_method"] = diag.get("method") or result.get("diagnostic_method")
            self.dismiss_diagnostic_firewall()

            if has_renault_auto_search(self.preferred_brand):
                self._step(
                    "Renault/Dacia — Automatically Search → YES → YES → model",
                    0.87,
                )
                time.sleep(0.7)
                adv = self.advance_renault_after_diagnostic(timeout=300)
                if adv.get("model"):
                    model = str(adv["model"])
                    result["model"] = model
                    self.detected_model = model
                if adv.get("vin"):
                    vin = str(adv["vin"])
                    result["vin"] = vin
                    self.detected_vin = vin
                if not adv.get("ok"):
                    result["error"] = adv.get("error") or (
                        "Renault Automatically Search / model identification failed"
                    )
                    return result

            if has_toyota_auto_search(self.preferred_brand):
                self._step(
                    "Toyota/Lexus — Show Menu → Automatic Search (Europe and Other)",
                    0.87,
                )
                time.sleep(0.5)
                adv = self.advance_toyota_after_diagnostic(timeout=90)
                if not adv.get("ok"):
                    result["error"] = adv.get("error") or (
                        "Toyota/Lexus Automatic Search failed after Diagnostic"
                    )
                    return result

            topo_timeout = 240.0 if has_fca_oil_reset(self.preferred_brand) else 45.0
            topo = self.wait_system_topology(timeout=topo_timeout)
            if not topo.get("ok") and (
                topo.get("saw_local_diagnose") or self.is_local_diagnose_page()
            ):
                self._step(
                    "After AutoDetect the tablet is on Local Diagnose — searching brand now",
                    0.36,
                )
                fb = self.local_diagnose_brand_fallback()
                if not fb.get("ok"):
                    result["error"] = fb.get("error") or topo.get("error")
                    return result
                result["method"] = "local_diagnose_fallback"
                used_local = True
                make = self.detected_make or self.preferred_brand
                if has_renault_auto_search(self.preferred_brand):
                    self._step(
                        "Renault/Dacia after Local Diagnose — Automatically Search → YES → YES → model",
                        0.87,
                    )
                    adv = self.advance_renault_after_diagnostic(timeout=300)
                    if adv.get("model"):
                        model = str(adv["model"])
                        result["model"] = model
                        self.detected_model = model
                    if adv.get("vin"):
                        vin = str(adv["vin"])
                        result["vin"] = vin
                        self.detected_vin = vin
                    if not adv.get("ok"):
                        result["error"] = adv.get("error") or (
                            "Renault Automatically Search / model identification failed"
                        )
                        return result
                if has_toyota_auto_search(self.preferred_brand):
                    self._step(
                        "Toyota/Lexus after Local Diagnose — Show Menu → Automatic Search",
                        0.87,
                    )
                    adv = self.advance_toyota_after_diagnostic(timeout=90)
                    if not adv.get("ok"):
                        result["error"] = adv.get("error") or (
                            "Toyota/Lexus Automatic Search failed after Local Diagnose"
                        )
                        return result
                topo = self.wait_system_topology(timeout=topo_timeout)
            if not topo.get("ok"):
                result["error"] = topo.get("error") or "System Topology not reached"
                return result

            if topo.get("vin"):
                vin = str(topo.get("vin"))
                result["vin"] = vin
                self.detected_vin = vin
            if topo.get("model"):
                model = str(topo.get("model"))
                result["model"] = model
                self.detected_model = model
            diag_mode = str(topo.get("mode") or topo.get("software") or "")
            if diag_mode:
                software = diag_mode
                result["software"] = software
                result["diag_mode"] = diag_mode
            if topo.get("make"):
                make = str(topo.get("make"))
                result["make"] = make
                self.detected_make = make

            result["vin"] = vin or "UNKNOWN"
            save_vin_audit(
                serial=self.serial,
                brand_selected=self.preferred_brand,
                make_detected=make,
                vin=vin or "UNKNOWN",
                software=software,
                source="system_and_function" if (diag_mode or topo.get("vin")) else (
                    "local_diagnose" if used_local else "intelligent_diagnose"
                ),
                model=model,
            )
            save_ticket(
                vin or "UNKNOWN",
                make,
                model,
                f"Mode: {diag_mode}" if diag_mode else (
                    "Local Diagnose" if used_local else "AutoDetect Success"
                ),
                serial=self.serial,
            )
            save_report(
                self.serial,
                "vag_autodetect_vin",
                "completed",
                f"brand={self.preferred_brand}; make={make}; model={model}; vin={vin or 'UNKNOWN'}; "
                f"method={result.get('method')}; software={software}",
                None,
            )
            self._step(
                f"Identity · Make: {make} · Model: {model or '—'} · VIN: {vin or 'UNKNOWN'} · Mode: {diag_mode or '—'}",
                0.945,
            )

            if topo.get("scan_tapped"):
                scan = {"ok": True, "button": topo.get("scan_button") or "High-speed Scan"}
            else:
                scan = self.start_topology_ecu_scan()
            self.dismiss_diagnostic_firewall()
            result["scan_button"] = scan.get("button")
            if not scan.get("ok"):
                result["error"] = scan.get("error") or "Failed to start ECU scan"
                return result

            save_report(
                self.serial,
                "vag_topology_ecu_scan",
                "started",
                f"vin={vin or 'UNKNOWN'}; make={make}; model={model}; mode={diag_mode}; "
                f"button={scan.get('button')}; method={result.get('method')}",
                None,
            )
            self._step(
                f"ECU scan started via '{scan.get('button')}' — waiting until all modules are read",
                0.955,
            )

            done = self.wait_ecu_scan_complete(timeout=600)
            if not done.get("ok"):
                result["error"] = done.get("error") or "ECU scan did not finish"
                return result

            mailed = self.send_scan_report_via_gmail()
            result["emailed_to"] = mailed.get("emailed_to") or self.report_email or REPORT_EMAIL
            result["report_emailed"] = bool(mailed.get("ok"))
            result["returned_to_topology"] = bool(mailed.get("returned_to_topology"))
            result["engineer"] = self.engineer
            if not mailed.get("ok"):
                result["error"] = mailed.get("error") or "Failed to email X431 report"
                result["ok"] = True
                save_report(
                    self.serial,
                    "vag_inspection_report_email",
                    "failed",
                    f"vin={vin or 'UNKNOWN'}; make={make}; model={model}; "
                    f"engineer={self.engineer or '—'}; to={result['emailed_to']}; "
                    f"error={result['error']}",
                    None,
                )
                self._step(f"Scan finished but email not confirmed: {result['error']}", 0.99)
                return result

            save_report(
                self.serial,
                "vag_inspection_report_email",
                "completed",
                f"vin={vin or 'UNKNOWN'}; make={make}; model={model}; "
                f"engineer={self.engineer or '—'}; to={result['emailed_to']}",
                None,
            )

            elapsed = round(time.time() - t0, 2)
            result["ok"] = True
            self._step(
                f"Full scan + report emailed to {result['emailed_to']} "
                f"· engineer={self.engineer or '—'} · total {elapsed}s",
                1.0,
            )
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"start_auto_detection error: {exc}")
        return result

    # Keep slower helper for later phases / recovery.
    def ensure_app_open(self) -> None:
        self.ensure_app_open_fast()

    def tap_intelligent_diagnose(self) -> Dict[str, object]:
        return self.tap_intelligent_diagnose_fast()

    # ------------------------------------------------------------------ identify

    def identify_vag(self, mode: str = "auto") -> Dict[str, object]:
        """Identify a VAG vehicle and enter the diagnostic software.

        Flow (auto):
          Intelligent Diagnose → AutoDetect Result → Diagnostic → (brand software)

        Flow (manual):
          Local Diagnose → VAG brand (VW/Audi/SEAT/Skoda/Cupra) → model if needed
        """
        self.logs = []
        self.ensure_device()
        self.open_x431()
        mode = (mode or "auto").lower().strip()
        result: Dict[str, object] = {
            "ok": False,
            "mode": mode,
            "vin": None,
            "make": None,
            "model": None,
            "error": None,
            **self.device_stamp(),
        }

        try:
            if mode == "manual":
                self._step("VAG manual: Local Diagnose", 0.1)
                if not self._click_by_text(HOME_MANUAL_DIAGNOSE, timeout=8):
                    raise RuntimeError("Local Diagnose not found")
                self.dismiss_until_stable()
                brand = self._select_vag_brand()
                if not brand:
                    raise RuntimeError("No VAG brand found in Local Diagnose list")
                self.detected_make = brand
                result["make"] = brand
                self.dismiss_until_stable()
            else:
                if not self.screen_contains("AutoDetect Result", "Vehicle Information"):
                    self._step("VAG auto: Intelligent Diagnose (text + icon)", 0.1)
                    tap = self.tap_intelligent_diagnose()
                    if not tap.get("ok"):
                        raise RuntimeError(tap.get("error") or "Intelligent Diagnose not found")
                self.dismiss_until_stable()
                self._step("Waiting for AutoDetect Result", 0.25)
                self._wait_for_any(
                    ("AutoDetect Result", "Vehicle Information", "Diagnostic", "VIN"),
                    timeout=45,
                )
                vin = self.extract_vin_from_screen()
                if vin:
                    self.detected_vin = vin
                    result["vin"] = vin
                    self._step(f"VIN detected: {vin}", 0.35)
                make = self.extract_make_from_screen() or self._detect_vag_make_on_screen()
                if make:
                    self.detected_make = make
                    result["make"] = make
                    self._step(f"Make detected: {make}", 0.4)
                else:
                    result["make"] = self.preferred_brand
                    self.detected_make = self.preferred_brand

                model = self.extract_model_from_screen()
                if model:
                    self.detected_model = model
                    result["model"] = model
                    self._step(f"Model detected: {model}", 0.42)

                if vin:
                    save_ticket(
                        vin,
                        str(result.get("make") or ""),
                        str(result.get("model") or ""),
                        "Identified",
                        serial=self.serial,
                    )

                if self.screen_contains("AutoDetect Result", "Diagnostic"):
                    self._step("Entering Diagnostic from AutoDetect", 0.45)
                    if not self._click_by_text(("Diagnostic", "Diagnose", "Enter"), timeout=8):
                        raise RuntimeError("Diagnostic button not found on AutoDetect Result")
                    time.sleep(2.0)
                    self.dismiss_until_stable()

            # Enter / confirm VAG OEM software package if listed.
            self._enter_vag_software_if_present()
            self.dismiss_until_stable()

            # Success = we see scan/service/system menu items OR left AutoDetect.
            if self._wait_for_any(
                tuple(VAG_SCAN_ENTRY)
                + ("System Selection", "Special Function", "Service Function", "Diagnosis", "ECU"),
                timeout=40,
            ):
                result["ok"] = True
                self._step("VAG diagnostic menu ready", 0.6)
            else:
                # Soft-ok if we at least left home and entered a diagnose path.
                result["ok"] = True
                self._step("VAG identify finished (menu labels not confirmed)", 0.6)

            save_report(self.serial, "vag_identify", "completed", "\n".join(self.logs), None)
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"identify_vag error: {exc}")
            save_report(self.serial, "vag_identify", "failed", "\n".join(self.logs), None)
        return result

    def _select_vag_brand(self) -> Optional[str]:
        """Pick a VAG brand from Local Diagnose list (preferred first)."""
        ordered = [self.preferred_brand] + [b for b in VAG_BRANDS if b != self.preferred_brand]
        # Scroll a few times looking for brand.
        for _ in range(6):
            hit = self._click_by_text(tuple(ordered), timeout=2.5)
            if hit:
                return hit
            try:
                self.ensure_device().swipe_ext("up", scale=0.6)
            except Exception:
                adb.shell(self.serial, "input swipe 640 700 640 300 300")
            time.sleep(0.6)
        return None

    def _detect_vag_make_on_screen(self) -> Optional[str]:
        texts = self.visible_texts()
        blob = " ".join(texts)
        for brand in VAG_BRANDS:
            if re.search(rf"\b{re.escape(brand)}\b", blob, re.IGNORECASE):
                return brand
        return None

    def _enter_vag_software_if_present(self) -> None:
        """If a VAG series software row is shown, open it."""
        software_hints = (
            "Series",
            "Full System",
            "Volkswagen",
            "Audi",
            "SEAT",
            "Skoda",
            "Škoda",
            "Cupra",
            "Enter",
            "OK",
        )
        if self.screen_contains("Software", "Series", "Full System"):
            self._step("VAG software package visible — selecting", 0.5)
            self._click_by_text(software_hints, timeout=5)
            time.sleep(1.5)
            self.dismiss_until_stable()

    def _wait_for_any(self, labels: Sequence[str], timeout: float = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.handle_common_dialogs(max_rounds=1)
            if self._text_present(labels, timeout=0.8) or self.screen_contains(*labels):
                return True
            time.sleep(1.0)
        return False

    # ------------------------------------------------------------------ DTC

    def read_full_dtc(self, vin: Optional[str] = None, clear: bool = False) -> Dict[str, object]:
        """Full VAG system DTC read; optionally clear afterwards.

        Expected tablet path after identify:
          System Scan / Health Report / Automatic Scan → wait → (report) → Clear
        """
        self.logs = []
        self.ensure_device()
        vin_val = vin or self.detected_vin or "UNKNOWN"
        result: Dict[str, object] = {
            "ok": False,
            "dtcs": [],
            "cleared": False,
            "vin": vin_val,
            "error": None,
            **self.device_stamp(),
        }

        try:
            # Ensure we are inside a diagnose session; if on home, identify first.
            if self.screen_contains("Intelligent Diagnose", "Local Diagnose", "Service Function"):
                self._step("On EURO LINK home — running VAG identify first", 0.1)
                id_result = self.identify_vag(mode="auto")
                if not id_result.get("ok"):
                    raise RuntimeError(id_result.get("error") or "VAG identify failed")
                vin_val = str(id_result.get("vin") or vin_val)
                result["vin"] = vin_val

            self.dismiss_until_stable()
            self._step("Opening VAG full DTC scan entry", 0.55)
            if not self._click_by_text(VAG_SCAN_ENTRY, timeout=12):
                # Try Special Function / System Selection area then scan again.
                self._click_by_text(("System Selection", "Diagnosis", "Diagnose"), timeout=4)
                if not self._click_by_text(VAG_SCAN_ENTRY, timeout=8):
                    raise RuntimeError(
                        "Could not find VAG scan entry "
                        "(Health Report / System Scan / Read Fault Code)"
                    )

            self.dismiss_until_stable()
            self._step("Scanning — waiting for completion", 0.65)
            completed = self._wait_progress_complete(timeout=180)
            if completed:
                self._step("Scan completion indicator seen", 0.8)
            else:
                self._step("No 100% banner — capturing whatever is on screen", 0.8)

            # Some builds need an explicit Report / Fault Code view.
            self._click_by_text(("Report", "Fault Code", "DTC", "View Report", "Details"), timeout=4)
            time.sleep(1.0)
            self.dismiss_until_stable()

            dtcs = self._capture_and_store_dtcs(vin=vin_val)
            # Second capture after a short swipe in case list is long.
            try:
                self.ensure_device().swipe_ext("up", scale=0.5)
                time.sleep(0.8)
                more = self._capture_and_store_dtcs(vin=vin_val)
                seen = {d["code"] for d in dtcs}
                for item in more:
                    if item["code"] not in seen:
                        dtcs.append(item)
            except Exception:
                pass

            result["dtcs"] = dtcs
            self._step(f"DTC read complete — {len(dtcs)} code(s)", 0.88)

            if clear:
                self._step("Clearing VAG fault memory", 0.92)
                cleared = self._click_by_text(VAG_CLEAR_ENTRY, timeout=10)
                if cleared:
                    self.dismiss_until_stable()
                    self._click_by_text(("OK", "Confirm", "Yes", "Continue"), timeout=5)
                    self.dismiss_until_stable()
                    result["cleared"] = True
                    self._step(f"Clear triggered via '{cleared}'", 0.96)
                else:
                    self._step("Clear control not found after scan", 0.96)

            result["ok"] = True
            self._step("VAG DTC workflow completed", 1.0)
            report_dir = Path(__file__).resolve().parent.parent / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"vag_dtc_{int(time.time())}.txt"
            report_path.write_text(
                f"DEVICE={self.device_label()}\nSERIAL={self.serial}\n"
                f"VIN={vin_val}\nMAKE={self.detected_make}\nMODEL={self.detected_model}\n"
                + "\n".join(self.logs)
                + "\nDTCS="
                + str(dtcs)
                + "\n",
                encoding="utf-8",
            )
            save_report(self.serial, "vag_dtc_read_clear", "completed", "\n".join(self.logs), str(report_path))
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"read_full_dtc error: {exc}")
            save_report(self.serial, "vag_dtc_read_clear", "failed", "\n".join(self.logs), None)
        return result

    # ------------------------------------------------------------------ service

    def perform_vag_service_reset(self, reset_type: str) -> Dict[str, object]:
        """Run a VAG service reset from home Service Function (or in-session menu).

        Typical path:
          Service Function → Volkswagen/Audi/SEAT/Skoda → Oil/EPB/SAS/Battery → guided OK
        """
        self.logs = []
        self.ensure_device()
        key = (reset_type or "oil").strip().lower()
        aliases = VAG_SERVICE_RESETS.get(key)
        if not aliases:
            aliases = (reset_type, reset_type.replace("_", " ").title())
            key = key or "custom"

        result: Dict[str, object] = {"ok": False, "reset_type": key, "error": None, **self.device_stamp()}

        try:
            self.open_x431()
            # Prefer home Service Function tile for VAG quick services.
            if self.screen_contains("Service Function", "Intelligent Diagnose"):
                self._step("Opening home Service Function", 0.15)
                if not self._click_by_text(HOME_SERVICE, timeout=8):
                    raise RuntimeError("Service Function tile not found")
                self.dismiss_until_stable()
                # Brand pick inside Service Function.
                brand = self._click_by_text(tuple(VAG_BRANDS), timeout=6)
                if brand:
                    self._step(f"Service Function brand: {brand}", 0.3)
                    self.dismiss_until_stable()
            else:
                # Already inside diagnose — try Special/Service Function menu.
                self._step("Looking for in-session Special/Service Function", 0.15)
                self._click_by_text(
                    ("Special Function", "Service Function", "Service", "Maintenance"),
                    timeout=6,
                )
                self.dismiss_until_stable()

            self._step(f"Selecting reset type: {key}", 0.45)
            selected = None
            for _ in range(5):
                selected = self._click_by_text(aliases, timeout=3)
                if selected:
                    break
                try:
                    self.ensure_device().swipe_ext("up", scale=0.55)
                except Exception:
                    pass
                time.sleep(0.5)
            if not selected:
                raise RuntimeError(f"VAG reset option not found for '{key}'")

            self.dismiss_until_stable()
            self._step("Following guided reset confirmations", 0.7)
            for i in range(10):
                dismissed = self.handle_common_dialogs(max_rounds=2)
                advanced = self._click_by_text(
                    (
                        "Start",
                        "Continue",
                        "Next",
                        "Execute",
                        "OK",
                        "Confirm",
                        "Yes",
                        "Finish",
                        "Done",
                        "Complete",
                    ),
                    timeout=2.5,
                )
                if self.screen_contains("Success", "Completed", "Finished", "successful"):
                    self._step("Success indicator on screen", 0.9)
                    break
                if not dismissed and not advanced:
                    # Idle step — keep waiting a bit for ECU procedure.
                    time.sleep(1.2)
                    if i > 3:
                        break
                else:
                    time.sleep(0.8)

            result["ok"] = True
            self._step(f"VAG service reset '{key}' finished", 1.0)
            save_report(self.serial, f"vag_service_reset_{key}", "completed", "\n".join(self.logs), None)
        except Exception as exc:
            result["error"] = str(exc)
            self._step(f"perform_vag_service_reset error: {exc}")
            save_report(self.serial, f"vag_service_reset_{key}", "failed", "\n".join(self.logs), None)
        return result

    def run_task(self, task: str, **kwargs) -> Dict[str, object]:
        """Route VAG tasks: ``identify``, ``dtc_read``, ``dtc_clear``, ``service_reset``."""
        name = (task or "").strip().lower()
        if name in ("identify", "identify_vag", "auto_vin"):
            return self.identify_vag(mode=str(kwargs.get("mode") or "auto"))
        if name in ("dtc_read", "read_dtc", "full_dtc"):
            mode = str(kwargs.get("mode") or "auto")
            if kwargs.get("identify", True):
                id_result = self.identify_vag(mode=mode)
                if not id_result.get("ok"):
                    return id_result
            return self.read_full_dtc(vin=kwargs.get("vin"), clear=False)
        if name in ("dtc_clear", "clear_dtc", "full_dtc_clear"):
            mode = str(kwargs.get("mode") or "auto")
            if kwargs.get("identify", True):
                id_result = self.identify_vag(mode=mode)
                if not id_result.get("ok"):
                    return id_result
            return self.read_full_dtc(vin=kwargs.get("vin"), clear=True)
        if name in ("service_reset", "reset"):
            return self.perform_vag_service_reset(str(kwargs.get("reset_type") or "oil"))
        return {"ok": False, "error": f"Unknown VAG task: {task}"}
