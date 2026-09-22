"""Launch X431 full automated workflow .

Flow: Sync → choose brand → Intelligent Diagnose → wait processing →
AutoDetect Result → read VIN/Make → save for audit.
"""

from __future__ import annotations

import threading
import time

import streamlit as st

from modules.adb_controller import (
    capture_screenshot,
    connect_adb_wifi,
    connection_mode,
    disconnect_adb_wifi,
    find_and_tap_text,
    get_device_label,
    get_device_wifi_ip,
    hard_reset_x431_session,
    input_text,
    is_wifi_serial,
    keyevent,
    launch_x431,
    list_devices,
    set_device_alias,
    switch_usb_to_wifi,
    sync_adb_devices,
    tap_ratio,
)
from modules.brand_catalog import (
    brand_group_names,
    brands_in_group,
    has_fca_oil_reset,
    has_full_workflow,
    has_renault_auto_search,
    has_toyota_auto_search,
)
from modules.db import fetch_vin_audit
from modules.engineer_session import (
    DEFAULT_REPORT_EMAIL,
    normalize_report_email,
    resolve_engineer,
)
from modules.fca_workflows import FCAWorkflowEngine
from modules.scrcpy_mirror import mirror_status, start_mirror, stop_mirror
from modules.vag_workflows import VAGWorkflowEngine

st.set_page_config(page_title="Launch X431 — Auto Detect", layout="wide")
st.title("Launch X431 — Auto detection + VIN audit")
st.caption(
    "Multi-brand platform: all groups use **Intelligent Diagnose** first. "
    "VAG: full scan + report email. Renault/Dacia: Automatically Search + model pick. "
    "FCA (Fiat/Jeep/…): **Oil Maintenance Reset** (SGW). "
    "Scans are saved under the logged-in toolbox engineer. "
    "Sync ADB (USB or Wi-Fi), leave the tablet on EURO LINK home before starting."
)


def _format_adb_device(serial: str) -> str:
    label = get_device_label(serial)
    mode = "Wi-Fi" if is_wifi_serial(serial) else "USB"
    if label != serial:
        return f"{label} ({mode})"
    return f"{serial} ({mode})"


devices = list_devices() if not (st.session_state.get("autodetect_ctl") or {}).get("running") else (
    st.session_state.get("last_adb_devices") or []
)
if devices or "last_adb_devices" not in st.session_state:
    st.session_state["last_adb_devices"] = devices or st.session_state.get("last_adb_devices", [])

# --- 1) Sync --------------------------------------------------------------------
st.subheader("1. Sync tablet")
c_sync, c_dev = st.columns([1, 3])
with c_sync:
    if st.button("Sync ADB", type="primary", width="stretch"):
        result = sync_adb_devices()
        st.session_state["last_adb_devices"] = result.get("devices") or []
        if result.get("devices"):
            st.success(f"Found {len(result['devices'])} device(s)")
        else:
            st.error("No ADB device found")
        if result.get("raw"):
            st.code(result["raw"])

with c_dev:
    options = st.session_state.get("last_adb_devices") or []
    if not options:
        st.info("No device yet — plug USB and Sync, or use Wi-Fi connect below.")
        serial = ""
    else:
        serial = st.selectbox(
            "ADB device",
            options,
            index=0,
            format_func=_format_adb_device,
        )
        label = get_device_label(serial)
        mode = connection_mode(serial)
        st.write(f"Selected: `{label}` · **{mode.upper()}**")
        if label != serial:
            st.caption(f"Serial: `{serial}`")

        # Keep widget key in sync before the text_input is created (Streamlit rule).
        st.session_state["adb_label_serial"] = serial
        if st.session_state.get("adb_label_for_serial") != serial:
            st.session_state["adb_device_label"] = label
            st.session_state["adb_label_for_serial"] = serial

        def _save_adb_device_label() -> None:
            target = st.session_state.get("adb_label_serial") or ""
            raw = (st.session_state.get("adb_device_label") or "").strip() or "Launch_Osias"
            saved = set_device_alias(target, raw)
            st.session_state["adb_device_label"] = saved
            st.session_state["adb_label_for_serial"] = target
            st.session_state["adb_label_just_saved"] = saved

        st.text_input("Device label", key="adb_device_label")
        st.button(
            "Save label",
            on_click=_save_adb_device_label,
            width="stretch",
        )
        if saved_label := st.session_state.pop("adb_label_just_saved", None):
            st.success(f"Saved label: `{saved_label}`")

# --- Sidebar: live tablet + remote job control -------------------------------
# Ensure pause event exists early (workers also create it on Start).
if "autodetect_ctl" not in st.session_state:
    st.session_state["autodetect_ctl"] = {
        "running": False,
        "cancel": threading.Event(),
        "pause": threading.Event(),
        "logs": [],
        "progress": 0.0,
        "outcome": None,
        "error": None,
        "started_at": None,
        "brand": None,
        "serial": None,
        "current_step": None,
        "step_count": 0,
        "engineer": None,
        "report_email": None,
    }
elif "pause" not in st.session_state["autodetect_ctl"]:
    st.session_state["autodetect_ctl"]["pause"] = threading.Event()

_sb_ctl = st.session_state["autodetect_ctl"]


def _sidebar_flash(level: str, message: str) -> None:
    st.session_state["sidebar_ctl_flash"] = (level, message)


def _sidebar_do_tap_label(serial_id: str, label: str) -> None:
    ok = find_and_tap_text(serial_id, label, timeout=4)
    _sidebar_flash(
        "success" if ok else "error",
        f"Tapped «{label}»" if ok else f"«{label}» not found on screen",
    )


with st.sidebar:
    st.markdown("### Tablet screen")
    st.caption("Watch the tablet on your PC, or take over with the mouse.")
    mirror_serial = serial or ""
    status = mirror_status(mirror_serial)
    if not status.get("available"):
        st.warning("Live screen isn't available on this PC.")
    elif status.get("running"):
        st.success("Screen is open")

    sb1, sb2 = st.columns(2)
    with sb1:
        open_mirror = st.button(
            "Show screen",
            type="primary",
            width="stretch",
            disabled=not mirror_serial or not status.get("available"),
            help="Opens a live window of the tablet.",
            key="sb_open_scrcpy",
        )
    with sb2:
        close_mirror = st.button(
            "Close",
            width="stretch",
            disabled=not status.get("running"),
            key="sb_close_scrcpy",
        )

    wifi_opts = is_wifi_serial(mirror_serial) if mirror_serial else False
    stay = st.checkbox("Keep tablet awake", value=True, key="scrcpy_stay_awake")
    low_bw = st.checkbox(
        "Use less data (Wi-Fi)",
        value=wifi_opts,
        key="scrcpy_low_bw",
        help="Smoother when the tablet is on Wi-Fi rather than USB.",
    )

    if open_mirror and mirror_serial:
        kwargs = {
            "stay_awake": bool(stay),
            "bitrate": "4M" if low_bw else "8M",
            "max_size": 1024 if low_bw else 1920,
            "max_fps": 30 if low_bw else 60,
        }
        result = start_mirror(mirror_serial, **kwargs)
        if result.get("ok"):
            st.session_state["scrcpy_flash"] = ("success", "Tablet screen opened")
        else:
            st.session_state["scrcpy_flash"] = (
                "error",
                "Could not open the tablet screen",
            )
        st.rerun()

    if close_mirror:
        stop_mirror(mirror_serial)
        st.session_state["scrcpy_flash"] = ("success", "Tablet screen closed")
        st.rerun()

    flash_m = st.session_state.pop("scrcpy_flash", None)
    if flash_m:
        lvl, msg = flash_m
        (st.success if lvl == "success" else st.error)(msg)

    # --- Script / job control -----------------------------------------------
    st.markdown("##### Scan")
    running = bool(_sb_ctl.get("running"))
    paused = bool(_sb_ctl.get("pause") and _sb_ctl["pause"].is_set())
    if running and paused:
        st.warning("Paused — you can tap the tablet yourself")
    elif running:
        st.info("Scan running")
    else:
        st.caption("No scan running")

    jc1, jc2, jc3 = st.columns(3)
    with jc1:
        do_pause = st.button(
            "Pause",
            width="stretch",
            disabled=not running or paused,
            key="sb_pause",
            help="Pause the scan so you can tap the tablet yourself.",
        )
    with jc2:
        do_resume = st.button(
            "Resume",
            width="stretch",
            disabled=not running or not paused,
            key="sb_resume",
        )
    with jc3:
        do_stop_sb = st.button(
            "Stop",
            width="stretch",
            disabled=not running,
            key="sb_stop",
            help="Stop the current scan.",
        )

    if do_pause and _sb_ctl.get("pause") is not None:
        _sb_ctl["pause"].set()
        _sb_ctl["phase"] = "Paused — operator control"
        stamp = time.strftime("%H:%M:%S")
        _sb_ctl["logs"] = list(_sb_ctl.get("logs") or []) + [
            {"t": stamp, "msg": "Paused by operator — waiting for Resume"}
        ]
        _sb_ctl["current_step"] = "Paused by operator"
        st.rerun()
    if do_resume and _sb_ctl.get("pause") is not None:
        _sb_ctl["pause"].clear()
        stamp = time.strftime("%H:%M:%S")
        _sb_ctl["logs"] = list(_sb_ctl.get("logs") or []) + [
            {"t": stamp, "msg": "Resumed by operator"}
        ]
        _sb_ctl["current_step"] = "Resumed by operator"
        st.rerun()
    if do_stop_sb:
        _sb_ctl["cancel"].set()
        if _sb_ctl.get("pause") is not None:
            _sb_ctl["pause"].clear()
        st.warning("Stopping the scan…")

    # --- Remote taps (do the job) -------------------------------------------
    st.markdown("##### Tap on tablet")
    st.caption("If a scan is running, pause it first.")
    disabled_tap = not mirror_serial

    nav1, nav2, nav3 = st.columns(3)
    with nav1:
        if st.button("Back", width="stretch", disabled=disabled_tap, key="sb_back"):
            keyevent(mirror_serial, "KEYCODE_BACK")
            _sidebar_flash("success", "Back")
            st.rerun()
    with nav2:
        if st.button("Home", width="stretch", disabled=disabled_tap, key="sb_home"):
            keyevent(mirror_serial, "KEYCODE_HOME")
            _sidebar_flash("success", "Home")
            st.rerun()
    with nav3:
        if st.button("Recents", width="stretch", disabled=disabled_tap, key="sb_recents"):
            keyevent(mirror_serial, "KEYCODE_APP_SWITCH")
            _sidebar_flash("success", "Recents")
            st.rerun()

    ok_c1, ok_c2 = st.columns(2)
    with ok_c1:
        if st.button("OK", type="primary", width="stretch", disabled=disabled_tap, key="sb_ok"):
            _sidebar_do_tap_label(mirror_serial, "OK")
            st.rerun()
    with ok_c2:
        if st.button("Cancel", width="stretch", disabled=disabled_tap, key="sb_cancel"):
            _sidebar_do_tap_label(mirror_serial, "CANCEL")
            st.rerun()

    q_labels = [
        ("Diagnostic", "Diagnostic"),
        ("Continue", "Continue"),
        ("Smart Detection", "Smart Detection"),
        ("Common Special", "Common Special Function"),
        ("Oil Reset", "Oil Maintenance Reset"),
        ("Intelligent Diag", "Intelligent Diagnose"),
    ]
    qcols = st.columns(2)
    for i, (btn, label) in enumerate(q_labels):
        with qcols[i % 2]:
            if st.button(btn, width="stretch", disabled=disabled_tap, key=f"sb_q_{i}"):
                _sidebar_do_tap_label(mirror_serial, label)
                st.rerun()

    tap_text = st.text_input(
        "Tap label on screen",
        key="sb_custom_tap_text",
        placeholder="e.g. Oil Maintenance Reset",
        disabled=disabled_tap,
    )
    if st.button("Tap that label", width="stretch", disabled=disabled_tap or not (tap_text or "").strip(), key="sb_custom_tap"):
        _sidebar_do_tap_label(mirror_serial, (tap_text or "").strip())
        st.rerun()

    with st.expander("Tap by position / type text"):
        rx = st.slider("X %", 0, 100, 50, key="sb_tap_rx")
        ry = st.slider("Y %", 0, 100, 50, key="sb_tap_ry")
        if st.button("Tap position", width="stretch", disabled=disabled_tap, key="sb_tap_pct"):
            msg = tap_ratio(mirror_serial, rx / 100.0, ry / 100.0)
            _sidebar_flash("success", msg)
            st.rerun()
        typed = st.text_input("Type on tablet", key="sb_type_text", disabled=disabled_tap)
        if st.button("Send text", width="stretch", disabled=disabled_tap or not (typed or "").strip(), key="sb_send_text"):
            try:
                input_text(mirror_serial, typed or "")
                _sidebar_flash("success", "Text sent")
            except Exception as exc:
                _sidebar_flash("error", str(exc))
            st.rerun()

    st.markdown("##### App")
    app1, app2 = st.columns(2)
    with app1:
        if st.button("Launch EURO LINK", width="stretch", disabled=disabled_tap, key="sb_launch"):
            try:
                msg = launch_x431(mirror_serial)
                _sidebar_flash("success", msg)
            except Exception as exc:
                _sidebar_flash("error", str(exc))
            st.rerun()
    with app2:
        if st.button("Hard reset", width="stretch", disabled=disabled_tap, key="sb_hard"):
            _sb_ctl["cancel"].set()
            if _sb_ctl.get("pause") is not None:
                _sb_ctl["pause"].clear()
            reset_result = hard_reset_x431_session(mirror_serial, relaunch=True)
            _sidebar_flash(
                "success" if reset_result.get("ok") else "error",
                "Hard reset done" if reset_result.get("ok") else "Hard reset had errors",
            )
            st.rerun()

    flash_sb = st.session_state.pop("sidebar_ctl_flash", None)
    if flash_sb:
        lvl, msg = flash_sb
        (st.success if lvl == "success" else st.error)(msg)

    st.markdown("##### Snapshot")
    auto_prev = st.checkbox(
        "Keep updating",
        value=False,
        key="scrcpy_auto_preview",
        help="Shows still photos. Use Show screen for a live window.",
    )
    refresh_prev = st.button(
        "Take photo",
        width="stretch",
        disabled=not mirror_serial,
        key="sb_refresh_prev",
    )
    if mirror_serial and (refresh_prev or auto_prev):
        shot = capture_screenshot(mirror_serial)
        if shot and shot.exists():
            st.image(
                str(shot),
                caption=f"{get_device_label(mirror_serial)} · {time.strftime('%H:%M:%S')}",
                width="stretch",
            )
            if auto_prev:
                time.sleep(2.5)
                st.rerun()
        else:
            st.caption("Could not take a photo of the tablet.")
    elif not mirror_serial:
        st.caption("Connect a tablet first.")

    st.divider()
    st.caption("To take over: **Pause** → tap or use the screen → **Resume**.")

# --- 1b) Wi-Fi (same network) -------------------------------------------------
st.markdown("##### Wi-Fi connection (same network, no USB)")
st.caption(
    "PC and tablet must be on the same LAN. First time: connect USB once, then "
    "**Switch USB → Wi-Fi**. Later: enter the tablet IP and **Connect Wi-Fi** "
    "(re-enable with USB after a tablet reboot)."
)

# Apply pending IP before the text_input widget is created (Streamlit rule).
pending_wifi_host = st.session_state.pop("adb_wifi_host_pending", None)
if pending_wifi_host:
    st.session_state["adb_wifi_host"] = pending_wifi_host
elif "adb_wifi_host" not in st.session_state:
    wifi_ip_default = ""
    if serial and not is_wifi_serial(serial):
        wifi_ip_default = get_device_wifi_ip(serial) or ""
    elif serial and is_wifi_serial(serial):
        wifi_ip_default = serial.rsplit(":", 1)[0]
    if wifi_ip_default:
        st.session_state["adb_wifi_host"] = wifi_ip_default

w1, w2, w3, w4 = st.columns([2, 1, 1, 1])
with w1:
    wifi_host = st.text_input(
        "Tablet IP (or IP:5555)",
        key="adb_wifi_host",
        placeholder="192.168.1.50",
        help="Tablet Settings → About / Wi-Fi status, or auto-filled from USB.",
    )
with w2:
    do_switch = st.button(
        "Switch USB → Wi-Fi",
        width="stretch",
        disabled=not serial or is_wifi_serial(serial),
        help="Uses the selected USB device: enable TCP/IP, then connect over Wi-Fi.",
    )
with w3:
    do_wifi_connect = st.button("Connect Wi-Fi", type="primary", width="stretch")
with w4:
    do_wifi_disconnect = st.button("Disconnect Wi-Fi", width="stretch")

if do_switch and serial:
    with st.spinner("Switching selected USB tablet to Wi-Fi ADB…"):
        result = switch_usb_to_wifi(serial)
    st.session_state["last_adb_devices"] = result.get("devices") or list_devices()
    if result.get("ip"):
        st.session_state["adb_wifi_host_pending"] = result["ip"]
    if result.get("ok"):
        st.session_state["adb_wifi_flash"] = (
            "success",
            f"Wi-Fi connected: `{result.get('endpoint')}` — USB can be unplugged",
        )
    else:
        st.session_state["adb_wifi_flash"] = (
            "error",
            "USB → Wi-Fi switch failed\n" + "\n".join(result.get("steps") or []),
        )
    st.rerun()

# Show flash from previous Wi-Fi action (after rerun).
flash = st.session_state.pop("adb_wifi_flash", None)
if flash:
    level, message = flash
    if level == "success":
        st.success(message)
    else:
        st.error(message.split("\n", 1)[0])
        if "\n" in message:
            st.code(message.split("\n", 1)[1])

if do_wifi_connect:
    with st.spinner("Connecting over Wi-Fi…"):
        result = connect_adb_wifi(wifi_host or "")
    st.session_state["last_adb_devices"] = result.get("devices") or list_devices()
    if result.get("ok"):
        endpoint = result.get("endpoint") or ""
        if endpoint:
            st.session_state["adb_wifi_host_pending"] = endpoint.rsplit(":", 1)[0]
        st.session_state["adb_wifi_flash"] = ("success", f"Connected: `{endpoint}`")
    else:
        st.session_state["adb_wifi_flash"] = (
            "error",
            "Wi-Fi connect failed — check IP, same network, and that TCP/IP ADB is enabled\n"
            + "\n".join(result.get("steps") or []),
        )
    st.rerun()

if do_wifi_disconnect:
    target = None
    if serial and is_wifi_serial(serial):
        target = serial
    elif wifi_host:
        target = wifi_host
    result = disconnect_adb_wifi(target)
    st.session_state["last_adb_devices"] = result.get("devices") or list_devices()
    if result.get("ok"):
        st.session_state["adb_wifi_flash"] = ("success", "Wi-Fi ADB disconnected")
    else:
        st.session_state["adb_wifi_flash"] = (
            "error",
            "Disconnect finished with warnings\n" + "\n".join(result.get("steps") or []),
        )
    st.rerun()

# Workflows need a selected device
if not serial:
    options = st.session_state.get("last_adb_devices") or []
    if not options:
        st.warning("Connect a Launch tablet via USB or Wi-Fi first.")
        st.stop()
    serial = options[0]

# --- 2) Brand -------------------------------------------------------------------
st.subheader("2. Choose brand")
brand_col1, brand_col2 = st.columns(2)
with brand_col1:
    group = st.selectbox(
        "Brand group",
        brand_group_names(),
        index=0,
        help="Manufacturer family on EURO LINK. VAG has the full workflow today; others use the same Intelligent Diagnose first step.",
    )
with brand_col2:
    group_brands = list(brands_in_group(group))
    default_brand = st.session_state.get("selected_brand")
    if default_brand not in group_brands:
        default_brand = group_brands[0] if group_brands else "Volkswagen"
    brand = st.selectbox(
        "Brand",
        group_brands,
        index=group_brands.index(default_brand) if default_brand in group_brands else 0,
    )
st.session_state["selected_brand"] = brand
st.session_state["vag_brand"] = brand  # legacy key

if has_full_workflow(brand):
    st.success(
        f"**{brand}** — full VAG workflow: AutoDetect → Diagnostic → Topology scan → "
        f"Report → email (default `{DEFAULT_REPORT_EMAIL}`)."
    )
elif has_renault_auto_search(brand):
    st.success(
        f"**{brand}** — AutoDetect → Diagnostic, **or continue on Local Diagnose** if AutoDetect is skipped "
        "(tablet opens that page — no home tap) → "
        "**Automatically Search** → YES → YES → tap identified model → System and Function → "
        "**High-speed Scan** (or **Smart Detection**) → report email."
    )
elif has_toyota_auto_search(brand):
    st.success(
        f"**{brand}** — AutoDetect → Diagnostic → **Show Menu** → "
        "**Automatic Search (Europe and Other)** → System and Function / Topology → "
        "**High-speed Scan** (or **Smart Detection**) → report email."
    )
elif has_fca_oil_reset(brand):
    st.success(
        f"**{brand}** — FCA oil reset: Intelligent Diagnose → Diagnostic → OK (SGW) → "
        "Common Special Function → Oil Maintenance Reset → home."
    )
else:
    st.info(
        f"**{brand}** — auto-detect entry enabled (Intelligent Diagnose → AutoDetect Result). "
        "Brand-specific scan/service workflows will be added later; Local Diagnose fallback uses this brand name."
    )

# --- 2b) Engineer (toolbox login) + report recipient ---------------------------
st.subheader("2b. Engineer & report email")
st.caption(
    "Each scan is attached to the engineer who is logged in on the toolbox. "
    f"Leave report email empty or as `{DEFAULT_REPORT_EMAIL}` unless this report should go to someone else."
)
detected_engineer = resolve_engineer()
from_toolbox_login = bool(detected_engineer)
op_eng, op_mail = st.columns(2)
with op_eng:
    if from_toolbox_login:
        st.text_input(
            "Engineer",
            value=detected_engineer,
            disabled=True,
            help="Filled from toolbox login. This scan is stored under your account.",
        )
        engineer = detected_engineer
    else:
        engineer = (
            st.text_input(
                "Engineer",
                key="engineer_name",
                help="Toolbox login was not detected in this session. Enter your name so the scan is attributed to you.",
            )
            or ""
        ).strip()
with op_mail:
    if "report_email" not in st.session_state:
        st.session_state["report_email"] = DEFAULT_REPORT_EMAIL
    report_email_raw = st.text_input(
        "Report email",
        key="report_email",
        help=f"X431 Gmail To: address. Default is {DEFAULT_REPORT_EMAIL}.",
    )
report_email = normalize_report_email(report_email_raw)
typed_mail = (report_email_raw or "").strip()
if typed_mail and typed_mail.lower() != report_email.lower():
    st.warning(f"That address is not valid — reports will go to `{DEFAULT_REPORT_EMAIL}`.")
if from_toolbox_login:
    st.caption(f"Logged in as **{engineer}**")
elif not engineer:
    st.warning("Log in on the toolbox, or type your engineer name, before starting a scan.")

# --- 3) Start / Stop / Hard Reset ----------------------------------------------
st.subheader("3. Start auto detection")
st.caption(
    "If a run is stuck: click **Stop**, then **Hard Reset** (force-stops EURO LINK and reopens home). "
    "If the page is frozen, refresh the browser first, then Hard Reset."
)

# Thread-safe control block (mutable object held in session_state).
if "autodetect_ctl" not in st.session_state:
    st.session_state["autodetect_ctl"] = {
        "running": False,
        "cancel": threading.Event(),
        "pause": threading.Event(),
        "logs": [],  # list of {"t": iso-ish, "msg": str}
        "progress": 0.0,
        "outcome": None,
        "error": None,
        "started_at": None,
        "brand": None,
        "serial": None,
        "current_step": None,
        "step_count": 0,
        "engineer": None,
        "report_email": None,
    }
if "pause" not in st.session_state["autodetect_ctl"]:
    st.session_state["autodetect_ctl"]["pause"] = threading.Event()
ctl = st.session_state["autodetect_ctl"]


def _worker_cancel_check(control: dict) -> bool:
    """True when Stop was requested. While Pause is set, block until Resume/Stop."""
    pause = control.get("pause")
    cancel = control.get("cancel")
    while pause is not None and pause.is_set():
        if cancel is not None and cancel.is_set():
            return True
        time.sleep(0.35)
    return bool(cancel is not None and cancel.is_set())


def _phase_from_step(message: str, progress: float) -> str:
    """Human-readable phase label from the latest step text."""
    low = (message or "").lower()
    if "stopped by user" in low or "hard reset" in low:
        return "Stopped / reset"
    if "paused by operator" in low or low.startswith("paused"):
        return "Paused — operator control"
    if "preflight" in low or "connecting uiautomator" in low:
        return "1 · Connecting tablet"
    if "home" in low or "euro link" in low and "launch" in low:
        return "2 · Opening / confirming EURO LINK home"
    if "intelligent diagnose" in low:
        return "3 · Tapping Intelligent Diagnose"
    if "select make" in low:
        return "4 · Select Make confirmation"
    if "processing" in low or "identification" in low or "waiting for identification" in low:
        return "4 · Waiting for vehicle identification (VIN)"
    if "autodetect result" in low or "vin retrieved" in low or "identity saved" in low:
        return "5 · AutoDetect Result / VIN capture"
    if "diagnostic" in low:
        return "6 · Opening Diagnostic"
    if "automatically search" in low or "renault show menu" in low:
        return "6b · Renault Automatically Search"
    if "system information" in low or "ignition confirm" in low:
        return "6c · Renault YES confirm"
    if "renault model" in low or "model identification" in low:
        return "6d · Renault model identification"
    if "oil" in low or "fca" in low or "common special" in low or "sgw" in low:
        return "FCA · Oil Maintenance Reset"
    if "tapping tablet back" in low or "back on system" in low or "inspection report after send" in low:
        return "13 · Return to Topology"
    if "gmail" in low or "sending report" in low or "emailed" in low:
        return "12 · Email report via Gmail"
    if "other share" in low or "inspection report" in low:
        return "11 · Share inspection report"
    if "report information" in low or "more information" in low or "tapping report" in low:
        return "10 · Saving X431 report"
    if "scanning ecu" in low or "scan complete" in low or "waiting for ecu scan" in low:
        return "9 · Full ECU scan"
    if "high-speed" in low or "smart detection" in low or "ecu scan started" in low:
        return "8 · Starting ECU scan"
    if "topology" in low or "system and function" in low:
        return "7 · Waiting for System Topology"
    if progress >= 0.95:
        return "Done"
    if progress <= 0.05:
        return "Starting…"
    return "Running…"


def _fmt_elapsed(started_at: float | None) -> str:
    if not started_at:
        return "—"
    secs = max(0, int(time.time() - float(started_at)))
    m, s = divmod(secs, 60)
    return f"{m:02d}:{s:02d}"


def _log_lines(entries: list) -> list[str]:
    lines: list[str] = []
    for i, item in enumerate(entries or [], start=1):
        if isinstance(item, dict):
            ts = item.get("t") or ""
            msg = item.get("msg") or ""
            lines.append(f"{i:03d}  [{ts}]  {msg}")
        else:
            lines.append(f"{i:03d}  {item}")
    return lines

run_col, stop_col, reset_col, open_col = st.columns(4)
with run_col:
    start = st.button(
        "Start auto detection",
        type="primary",
        width="stretch",
        disabled=bool(ctl.get("running")) or not bool(engineer),
    )
with stop_col:
    stop = st.button(
        "Stop",
        width="stretch",
        disabled=not bool(ctl.get("running")),
        help="Request cancel of the current auto-detect thread.",
    )
with reset_col:
    hard_reset = st.button(
        "Hard Reset",
        width="stretch",
        help="Force-stop EURO LINK, clear stuck u2 helpers, relaunch home. Use after Stop or a frozen run.",
    )
with open_col:
    if st.button("Only open EURO LINK", width="stretch"):
        try:
            st.success(launch_x431(serial))
        except Exception as exc:
            st.error(str(exc))

start_fca_oil = False
if has_fca_oil_reset(brand):
    start_fca_oil = st.button(
        "Start Oil Maintenance Reset (FCA / SGW)",
        type="primary",
        width="stretch",
        disabled=bool(ctl.get("running")) or not bool(engineer),
        help="Fiat 500e-style: Diagnostic → OK confirm → SGW OK → Common Special Function → Oil reset → home.",
    )

if stop:
    ctl["cancel"].set()
    if ctl.get("pause") is not None:
        ctl["pause"].clear()
    st.warning("Stop requested — waiting for the current step to abort…")

if hard_reset:
    ctl["cancel"].set()
    if ctl.get("pause") is not None:
        ctl["pause"].clear()
    with st.spinner("Hard reset: force-stop EURO LINK and reopen home…"):
        reset_result = hard_reset_x431_session(serial, relaunch=True)
    stamp = time.strftime("%H:%M:%S")
    step_entries = [
        {"t": stamp, "msg": s} for s in (reset_result.get("steps") or [])
    ]
    ctl["running"] = False
    ctl["logs"] = step_entries
    ctl["progress"] = 0.0
    ctl["outcome"] = None
    ctl["error"] = None
    ctl["started_at"] = time.time()
    ctl["brand"] = brand
    ctl["serial"] = serial
    ctl["step_count"] = len(step_entries)
    ctl["current_step"] = (step_entries[-1]["msg"] if step_entries else "Hard reset")
    ctl["phase"] = "Stopped / reset"
    ctl["cancel"] = threading.Event()  # fresh cancel flag for next run
    ctl["pause"] = threading.Event()
    if reset_result.get("ok"):
        st.success("Hard reset done — EURO LINK should be on home. You can Start again.")
    else:
        st.error("Hard reset finished with errors — check the step log below.")




def _run_autodetect_worker(
    serial_id: str,
    brand_name: str,
    control: dict,
    engineer: str,
    report_email: str,
) -> None:
    """Background worker so Stop / Hard Reset can interrupt via cancel Event."""

    def emit(message: str, progress: float | None = None) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = {"t": stamp, "msg": message}
        control["logs"] = list(control.get("logs") or []) + [entry]
        control["current_step"] = message
        control["step_count"] = int(control.get("step_count") or 0) + 1
        control["phase"] = _phase_from_step(message, float(control.get("progress") or 0.0))
        if progress is not None:
            control["progress"] = min(max(float(progress), 0.0), 1.0)
            control["phase"] = _phase_from_step(message, control["progress"])

    def cancel_check() -> bool:
        return _worker_cancel_check(control)

    engine = VAGWorkflowEngine(
        serial_id,
        callback=emit,
        preferred_brand=brand_name,
        cancel_check=cancel_check,
        engineer=engineer,
        report_email=report_email,
    )
    try:
        outcome = engine.start_auto_detection(brand=brand_name)
        control["outcome"] = outcome
        # Prefer engine.logs (plain strings) merged as final timeline if richer.
        if engine.logs:
            # Keep timestamped entries already emitted; only append missing tail.
            existing = {e.get("msg") if isinstance(e, dict) else str(e) for e in (control.get("logs") or [])}
            for msg in engine.logs:
                if msg not in existing:
                    emit(msg, None)
        if outcome.get("error") and "Stopped by user" in str(outcome.get("error")):
            control["error"] = "Stopped by user"
            control["phase"] = "Stopped / reset"
        elif outcome.get("ok") and outcome.get("report_emailed"):
            control["error"] = None
            control["progress"] = 1.0
            control["phase"] = "Done"
            control["current_step"] = "Report emailed — back on System and Function / Topology"
        elif outcome.get("ok") and not outcome.get("report_emailed"):
            control["error"] = outcome.get("error") or "Scan finished but Gmail send was not confirmed"
            control["progress"] = 1.0
            control["phase"] = "Scan done — email not confirmed"
            control["current_step"] = control["error"]
        elif not outcome.get("ok"):
            control["error"] = outcome.get("error") or "Failed"
            control["phase"] = "Failed"
            control["current_step"] = control["error"]
        else:
            control["error"] = None
            control["progress"] = 1.0
            control["phase"] = "Done"
            control["current_step"] = "Report emailed — back on System and Function / Topology"
    except Exception as exc:
        control["error"] = str(exc)
        emit(f"ERROR: {exc}", None)
        control["outcome"] = {"ok": False, "error": str(exc)}
        control["phase"] = "Failed"
    finally:
        control["running"] = False


def _run_fca_oil_worker(
    serial_id: str,
    brand_name: str,
    control: dict,
    engineer: str,
    report_email: str,
) -> None:
    def emit(message: str, progress: float | None = None) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = {"t": stamp, "msg": message}
        control["logs"] = list(control.get("logs") or []) + [entry]
        control["current_step"] = message
        control["step_count"] = int(control.get("step_count") or 0) + 1
        control["phase"] = _phase_from_step(message, float(control.get("progress") or 0.0))
        if progress is not None:
            control["progress"] = min(max(float(progress), 0.0), 1.0)
            control["phase"] = _phase_from_step(message, control["progress"])

    def cancel_check() -> bool:
        return _worker_cancel_check(control)

    engine = FCAWorkflowEngine(
        serial_id,
        callback=emit,
        preferred_brand=brand_name,
        cancel_check=cancel_check,
        engineer=engineer,
        report_email=report_email,
    )
    try:
        outcome = engine.start_fca_oil_maintenance_reset(brand=brand_name)
        control["outcome"] = outcome
        if outcome.get("error") and "Stopped by user" in str(outcome.get("error")):
            control["error"] = "Stopped by user"
            control["phase"] = "Stopped / reset"
        elif not outcome.get("ok"):
            control["error"] = outcome.get("error") or "Failed"
            control["phase"] = "Failed"
            control["current_step"] = control["error"]
        else:
            control["error"] = None
            control["progress"] = 1.0
            control["phase"] = "Done"
            control["current_step"] = "FCA oil reset complete — home after hard reset"
    except Exception as exc:
        control["error"] = str(exc)
        emit(f"ERROR: {exc}", None)
        control["outcome"] = {"ok": False, "error": str(exc)}
        control["phase"] = "Failed"
    finally:
        control["running"] = False


if start and not ctl.get("running"):
    ctl["cancel"] = threading.Event()
    ctl["pause"] = threading.Event()
    ctl["running"] = True
    ctl["logs"] = []
    ctl["progress"] = 0.02
    ctl["outcome"] = None
    ctl["error"] = None
    ctl["started_at"] = time.time()
    ctl["brand"] = brand
    ctl["serial"] = serial
    ctl["engineer"] = engineer
    ctl["report_email"] = report_email
    ctl["current_step"] = f"Start auto detection — {brand}"
    ctl["step_count"] = 0
    ctl["phase"] = "Starting…"
    # Seed first visible step
    stamp = time.strftime("%H:%M:%S")
    ctl["logs"] = [{"t": stamp, "msg": f"Start auto detection — {brand} · engineer={engineer}"}]
    ctl["step_count"] = 1
    worker = threading.Thread(
        target=_run_autodetect_worker,
        args=(serial, brand, ctl, engineer, report_email),
        daemon=True,
        name="vag-autodetect",
    )
    worker.start()
    st.rerun()

if start_fca_oil and not ctl.get("running"):
    ctl["cancel"] = threading.Event()
    ctl["pause"] = threading.Event()
    ctl["running"] = True
    ctl["logs"] = []
    ctl["progress"] = 0.02
    ctl["outcome"] = None
    ctl["error"] = None
    ctl["started_at"] = time.time()
    ctl["brand"] = brand
    ctl["serial"] = serial
    ctl["engineer"] = engineer
    ctl["report_email"] = report_email
    ctl["current_step"] = f"FCA oil reset — {brand}"
    ctl["step_count"] = 1
    ctl["phase"] = "FCA · Oil Maintenance Reset"
    stamp = time.strftime("%H:%M:%S")
    ctl["logs"] = [{"t": stamp, "msg": f"FCA oil reset — {brand} · engineer={engineer}"}]
    threading.Thread(
        target=_run_fca_oil_worker,
        args=(serial, brand, ctl, engineer, report_email),
        daemon=True,
        name="fca-oil-reset",
    ).start()
    st.rerun()


def _run_topology_action_worker(
    serial_id: str,
    brand_name: str,
    control: dict,
    action: str,
    engineer: str,
    report_email: str,
) -> None:
    """Report / Clear All DTCs from System and Function / Topology."""

    def emit(message: str, progress: float | None = None) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = {"t": stamp, "msg": message}
        control["logs"] = list(control.get("logs") or []) + [entry]
        control["current_step"] = message
        control["step_count"] = int(control.get("step_count") or 0) + 1
        control["phase"] = _phase_from_step(message, float(control.get("progress") or 0.0))
        if progress is not None:
            control["progress"] = min(max(float(progress), 0.0), 1.0)
            control["phase"] = _phase_from_step(message, control["progress"])

    def cancel_check() -> bool:
        return _worker_cancel_check(control)

    engine = VAGWorkflowEngine(
        serial_id,
        callback=emit,
        preferred_brand=brand_name,
        cancel_check=cancel_check,
        engineer=engineer,
        report_email=report_email,
    )
    prev = dict(control.get("outcome") or {})
    try:
        if action == "report":
            outcome = engine.resend_topology_report()
        elif action == "clear_dtcs":
            outcome = engine.clear_all_dtcs()
        else:
            outcome = {"ok": False, "error": f"Unknown action: {action}"}
        merged = {**prev, **outcome}
        if prev.get("ok") and action == "clear_dtcs" and outcome.get("ok"):
            merged["ok"] = True
        control["outcome"] = merged
        if not outcome.get("ok"):
            control["error"] = outcome.get("error") or "Failed"
            control["phase"] = "Failed"
            control["current_step"] = control["error"]
        else:
            control["error"] = None
            control["progress"] = 1.0
            control["phase"] = "Done"
            if action == "report":
                control["current_step"] = "Report emailed — back on Topology"
            else:
                control["current_step"] = "Clear All DTCs done — still on Topology"
    except Exception as exc:
        control["error"] = str(exc)
        emit(f"ERROR: {exc}", None)
        control["phase"] = "Failed"
    finally:
        control["running"] = False


st.markdown("#### Topology actions")
st.caption(
    "Use only while the tablet shows **System and Function / Topology** "
    "(after a full scan, or after a report email). "
    "**Report** repeats save → Gmail → Back to Topology. "
    "**Clear All DTCs** taps the bottom-right tablet button."
)
act_report, act_clear = st.columns(2)
with act_report:
    do_report = st.button(
        "Report",
        width="stretch",
        disabled=bool(ctl.get("running")) or not bool(serial) or not bool(engineer),
        help="Must be on System and Function / Topology. Runs Report → email → Back.",
    )
with act_clear:
    do_clear = st.button(
        "Clear All DTCs",
        width="stretch",
        disabled=bool(ctl.get("running")) or not bool(serial),
        help="Must be on System and Function / Topology. Taps Clear All DTCs bottom-right.",
    )


def _start_topology_action(action: str, label: str) -> None:
    ctl["cancel"] = threading.Event()
    ctl["pause"] = threading.Event()
    ctl["running"] = True
    ctl["error"] = None
    ctl["progress"] = 0.2
    ctl["started_at"] = ctl.get("started_at") or time.time()
    ctl["brand"] = brand
    ctl["serial"] = serial
    ctl["engineer"] = engineer
    ctl["report_email"] = report_email
    ctl["current_step"] = label
    ctl["phase"] = label
    stamp = time.strftime("%H:%M:%S")
    ctl["logs"] = list(ctl.get("logs") or []) + [{"t": stamp, "msg": label}]
    ctl["step_count"] = int(ctl.get("step_count") or 0) + 1
    threading.Thread(
        target=_run_topology_action_worker,
        args=(serial, brand, ctl, action, engineer, report_email),
        daemon=True,
        name=f"vag-{action}",
    ).start()


if do_report and not ctl.get("running"):
    _start_topology_action("report", "Report from System and Function / Topology")
    st.rerun()
if do_clear and not ctl.get("running"):
    _start_topology_action("clear_dtcs", "Clear All DTCs from Topology")
    st.rerun()

# ---- Live session panel (why it's running + every step) ----
if ctl.get("running") or ctl.get("logs") or ctl.get("outcome") is not None or ctl.get("error"):
    st.markdown("#### Live session")
    progress = float(ctl.get("progress") or 0.0)
    current = ctl.get("current_step") or (ctl.get("logs") or [{}])[-1]
    if isinstance(current, dict):
        current = current.get("msg") or "—"
    phase = ctl.get("phase") or _phase_from_step(str(current), progress)
    elapsed = _fmt_elapsed(ctl.get("started_at"))

    s1, s2, s3, s4, s5 = st.columns(5)
    if ctl.get("running"):
        s1.metric("Status", "RUNNING")
    elif ctl.get("error"):
        s1.metric("Status", "FAILED / STOPPED")
    elif ctl.get("outcome") and ctl["outcome"].get("ok"):
        s1.metric("Status", "COMPLETE")
    else:
        s1.metric("Status", "IDLE")
    s2.metric("Elapsed", elapsed)
    s3.metric("Steps", int(ctl.get("step_count") or len(ctl.get("logs") or [])))
    s4.metric("Brand", ctl.get("brand") or brand)
    s5.metric("Engineer", ctl.get("engineer") or engineer or "—")

    st.progress(progress)
    st.caption(f"Progress {int(progress * 100)}% · {phase}")
    st.info(f"**Current step:** {current}")
    st.caption(
        f"Phase: {phase} · Device: `{get_device_label(ctl.get('serial') or serial)}` · "
        f"Serial: `{ctl.get('serial') or serial}` · "
        f"Engineer: `{ctl.get('engineer') or engineer or '—'}` · "
        f"Report to: `{ctl.get('report_email') or report_email}`"
    )

    if ctl.get("running"):
        st.warning("Session is active — tablet actions are in progress. Use **Stop** or **Hard Reset** if it hangs.")

    with st.expander("Step-by-step log (all events)", expanded=True):
        lines = _log_lines(ctl.get("logs") or [])
        st.code("\n".join(lines) if lines else "(no steps yet)", language="text")

    if ctl.get("running"):
        time.sleep(0.55)
        st.rerun()

    outcome = ctl.get("outcome")
    err = ctl.get("error")
    if outcome and outcome.get("ok"):
        device_name = outcome.get("device_label") or get_device_label(serial)
        m0, m1, m2, m3, m4, m5 = st.columns(6)
        m0.metric("Device", device_name)
        m1.metric("VIN", outcome.get("vin") or "—")
        m2.metric("Make", outcome.get("make") or "—")
        m3.metric("Model", outcome.get("model") or "—")
        m4.metric("Mode", outcome.get("diag_mode") or outcome.get("software") or "—")
        if outcome.get("sgw") is not None and not outcome.get("emailed_to"):
            m5.metric("SGW", "Unlocked" if outcome.get("sgw") else "—")
            st.success(
                f"FCA oil reset complete on `{device_name}` · VIN `{outcome.get('vin') or '—'}` · back on home"
            )
        elif outcome.get("report_emailed"):
            m5.metric("Email", outcome.get("emailed_to") or "—")
            st.success(
                f"Full scan complete on `{device_name}` · engineer `{outcome.get('engineer') or ctl.get('engineer') or engineer or '—'}` · "
                f"report emailed to "
                f"`{outcome.get('emailed_to') or DEFAULT_REPORT_EMAIL}`"
            )
        else:
            m5.metric("Email", "NOT SENT")
            st.warning(
                (outcome.get("error") or err or "Scan finished but Gmail send was not confirmed. ")
                + " The tablet may still be on compose — wait, or tap **Report** to retry."
            )
    elif err and not ctl.get("running"):
        st.error(err)

# --- 4) Audit -------------------------------------------------------------------
st.markdown("---")
st.subheader("VIN audit history")
try:
    audit = fetch_vin_audit(limit=25)
    if audit.empty:
        st.info("No VIN audit rows yet — run auto detection once.")
    else:
        st.dataframe(audit, width="stretch")
except Exception as exc:
    st.warning(f"Could not load audit table: {exc}")
