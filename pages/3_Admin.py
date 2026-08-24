"""Admin — management, audit, and controller session debug."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from modules.adb_controller import (
    connect_adb_wifi,
    get_device_label,
    is_wifi_serial,
    launch_x431,
    list_devices,
    sync_adb_devices,
)
from modules.db import (
    ADMIN_TABLES,
    clear_table,
    clear_tickets,
    ensure_database,
    fetch_reports,
    fetch_table,
    fetch_tickets,
    fetch_vin_audit,
    table_counts,
)
from modules.session_debug import diagnose_session, list_local_report_files

ensure_database()

st.set_page_config(page_title="Admin — Management & Debug", layout="wide")
st.title("Admin")
st.caption(
    "Full management, audit history, and live session debug when the controller is stuck or failing. "
    "VIN audit / tickets / reports include the engineer who ran the scan."
)

tab_mgmt, tab_audit, tab_debug = st.tabs(["Management", "Audit", "Session debug"])

# -----------------------------------------------------------------------------
# Management
# -----------------------------------------------------------------------------
with tab_mgmt:
    st.subheader("Database overview")
    counts = table_counts()
    cols = st.columns(len(ADMIN_TABLES))
    for col, name in zip(cols, ADMIN_TABLES):
        col.metric(name, counts.get(name, 0))

    st.markdown("---")
    st.subheader("Browse tables")
    table_name = st.selectbox("Table", list(ADMIN_TABLES), index=0)
    limit = st.slider("Rows to show", min_value=10, max_value=200, value=50, step=10)
    try:
        df = fetch_table(table_name)
        if df.empty:
            st.info(f"`{table_name}` is empty.")
        else:
            if "id" in df.columns:
                df = df.sort_values("id", ascending=False)
            st.dataframe(df.head(limit), use_container_width=True)
    except Exception as exc:
        st.error(str(exc))

    st.markdown("---")
    st.subheader("Danger zone — clear data")
    st.warning("Clears persisted SQLite rows. Workflows on the tablet are not stopped.")
    c1, c2 = st.columns(2)
    with c1:
        clear_target = st.selectbox("Clear one table", list(ADMIN_TABLES), key="clear_one")
        if st.button(f"Clear `{clear_target}`", type="secondary"):
            n = clear_table(clear_target)
            st.success(f"Deleted {n} row(s) from `{clear_target}`.")
            st.rerun()
    with c2:
        if st.button("Clear all tickets only", type="secondary"):
            n = clear_tickets()
            st.success(f"Deleted {n} ticket row(s).")
            st.rerun()

# -----------------------------------------------------------------------------
# Audit
# -----------------------------------------------------------------------------
with tab_audit:
    st.subheader("VIN audit")
    try:
        audit = fetch_vin_audit(limit=100)
        if audit.empty:
            st.info("No VIN audit rows yet.")
        else:
            st.dataframe(audit, use_container_width=True)
    except Exception as exc:
        st.error(str(exc))

    st.markdown("---")
    st.subheader("Tickets")
    try:
        tickets = fetch_tickets(limit=100)
        if tickets.empty:
            st.info("No tickets yet.")
        else:
            st.dataframe(tickets, use_container_width=True)
    except Exception as exc:
        st.error(str(exc))

    st.markdown("---")
    st.subheader("Workflow reports (DB)")
    try:
        reports = fetch_reports(limit=100)
        if reports.empty:
            st.info("No workflow reports yet.")
        else:
            st.dataframe(reports, use_container_width=True)
    except Exception as exc:
        st.error(str(exc))

    st.markdown("---")
    st.subheader("Local report files")
    files = list_local_report_files(limit=25)
    if not files:
        st.info("No files under `reports/`.")
    else:
        for item in files:
            with st.expander(item["name"]):
                path = Path(item["path"])
                st.code(path.read_text(encoding="utf-8", errors="replace")[:8000])

# -----------------------------------------------------------------------------
# Session debug
# -----------------------------------------------------------------------------
with tab_debug:
    st.subheader("Controller stuck / error / timeout")
    st.markdown(
        "Capture a full live session snapshot: connection, screenshot, visible UI text, "
        "recent failures, and concrete recovery steps."
    )

    devices = list_devices()
    if devices or "last_adb_devices" not in st.session_state:
        st.session_state["last_adb_devices"] = devices or st.session_state.get("last_adb_devices", [])

    d1, d2, d3 = st.columns([1, 2, 1])
    with d1:
        if st.button("Sync ADB", use_container_width=True):
            result = sync_adb_devices()
            st.session_state["last_adb_devices"] = result.get("devices") or []
            if result.get("devices"):
                st.success(f"Found {len(result['devices'])} device(s)")
            else:
                st.error("No ADB device found")
    with d2:
        options = st.session_state.get("last_adb_devices") or []
        if not options:
            st.warning("Sync a Launch tablet first.")
            serial = None
        else:
            serial = st.selectbox(
                "ADB device",
                options,
                index=0,
                key="admin_serial",
                format_func=get_device_label,
            )
            label = get_device_label(serial)
            st.caption(f"Selected: `{label}`" + (f" · serial `{serial}`" if label != serial else ""))
    with d3:
        open_ok = st.button("Open EURO LINK", use_container_width=True, disabled=not serial)
        if open_ok and serial:
            try:
                st.success(launch_x431(serial))
            except Exception as exc:
                st.error(str(exc))

    st.markdown("**Wi-Fi (same network)**")
    aw1, aw2 = st.columns([3, 1])
    with aw1:
        admin_wifi_host = st.text_input(
            "Tablet IP",
            key="admin_wifi_host",
            placeholder="192.168.1.50",
            label_visibility="collapsed",
        )
    with aw2:
        if st.button("Connect Wi-Fi", use_container_width=True):
            result = connect_adb_wifi(admin_wifi_host or "")
            st.session_state["last_adb_devices"] = result.get("devices") or list_devices()
            if result.get("ok"):
                st.success(f"Connected `{result.get('endpoint')}`")
                st.rerun()
            else:
                st.error("Wi-Fi connect failed")
                st.code("\n".join(result.get("steps") or []))
    if serial:
        st.caption("Mode: " + ("Wi-Fi" if is_wifi_serial(serial) else "USB"))

    run = st.button("Diagnose session", type="primary", disabled=not serial, use_container_width=True)
    if run and serial:
        with st.spinner("Capturing session…"):
            diag = diagnose_session(serial)

        if diag.get("error") and not diag.get("ok"):
            st.error(diag["error"])

        conn = diag.get("connection") or {}
        info = diag.get("device_info") or {}
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Device", diag.get("device_label") or serial or "—")
        m2.metric("ADB", "OK" if conn.get("adb_connected") else "DOWN")
        m3.metric("uiautomator2", "OK" if conn.get("u2_connected") else "DOWN")
        m4.metric("Product", conn.get("product") or info.get("model") or "—")
        m5.metric("Android", info.get("android_version") or "—")
        if diag.get("serial"):
            st.caption(f"Serial: `{diag['serial']}`")

        if conn.get("error"):
            st.warning(f"Connection note: {conn['error']}")

        st.markdown("### What to do")
        for tip in diag.get("advice") or []:
            st.markdown(f"- {tip}")

        st.markdown("### Screen summary")
        st.write(diag.get("screen_summary") or "—")

        shot = diag.get("screenshot")
        t_left, t_right = st.columns([1, 1])
        with t_left:
            st.markdown("**Screenshot**")
            if shot and Path(shot).exists():
                st.image(shot, use_container_width=True)
            else:
                st.info("No screenshot captured.")
        with t_right:
            st.markdown("**Visible UI texts**")
            texts = diag.get("visible_texts") or []
            if texts:
                st.code("\n".join(texts))
            else:
                st.info("No UI texts parsed.")

        st.markdown("### Recent workflow failures / reports")
        fails = diag.get("recent_failures") or []
        if fails:
            st.dataframe(fails, use_container_width=True)
        else:
            st.info("No recent report rows.")

        c_a, c_b = st.columns(2)
        with c_a:
            st.markdown("**Recent tickets**")
            if diag.get("recent_tickets"):
                st.dataframe(diag["recent_tickets"], use_container_width=True)
            else:
                st.info("None")
        with c_b:
            st.markdown("**Recent VIN audit**")
            if diag.get("recent_audit"):
                st.dataframe(diag["recent_audit"], use_container_width=True)
            else:
                st.info("None")

        with st.expander("Raw diagnose payload"):
            st.json(
                {
                    k: v
                    for k, v in diag.items()
                    if k not in {"screenshot"}  # path shown above
                }
            )
