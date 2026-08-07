import streamlit as st
from modules.adb_controller import (
    execute_dtc_clear_sequence,
    execute_dtc_scan_and_report,
    execute_full_car_identification,
    execute_service_reset,
    get_device_info,
    get_device_state,
    list_devices,
    perform_action,
    sync_adb_devices,
)
from src.agent.vision_agent import VisionAgent

st.set_page_config(page_title="Launch X431 ADB Controller", layout="wide")
st.title("Launch X431 ADB Controller")

st.markdown(
    "Control and manage a connected Launch tablet to open X431 Euro Link, inspect the UI, and run state-driven diagnostic workflows."
)

if "last_adb_devices" not in st.session_state:
    st.session_state["last_adb_devices"] = list_devices()

col_sync, col_select = st.columns([1, 4])
with col_sync:
    if st.button("Sync ADB devices"):
        status_area = st.empty()
        raw_area = st.empty()
        with st.spinner("Looking for ADB devices..."):
            result = sync_adb_devices()
        for s in result.get("steps", []):
            status_area.write(s)
        if result.get("ok") and result.get("devices"):
            st.success(f"ADB sync successful — found {len(result['devices'])} device(s)")
            st.session_state["last_adb_devices"] = result["devices"]
        else:
            st.error("ADB sync completed: no devices found or error occurred")
        raw_area.code(result.get("raw", ""))

with col_select:
    device_serial = st.selectbox("Select ADB device", ["<none>"] + st.session_state.get("last_adb_devices", []))

if device_serial and device_serial != "<none>":
    st.markdown("---")
    st.subheader("Device Information")

    state = get_device_state(device_serial)
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"**Serial:** {device_serial}")
        st.write(f"**Connected:** {state.get('connected')}")
    with col2:
        st.write(f"**Product:** {state.get('product', 'Unknown')}")

    with st.spinner("Fetching device details..."):
        try:
            dev_info = get_device_info(device_serial)
            if "error" in dev_info:
                st.warning(f"Could not fetch full device info: {dev_info['error']}")
            else:
                st.write(f"**Model:** {dev_info.get('model', 'N/A')}")
                st.write(f"**Android Version:** {dev_info.get('android_version', 'N/A')}")
                st.write(f"**Build:** {dev_info.get('build_fingerprint', 'N/A')}")
        except Exception as exc:
            st.error(f"Error fetching device info: {exc}")

    st.markdown("---")
    st.subheader("Workflow Automation")

    workflow_col1, workflow_col2, workflow_col3, workflow_col4 = st.columns(4)
    with workflow_col1:
        if st.button("Run Full Auto-Scan & Clear"):
            with st.spinner("Running the full workflow..."):
                logs = []
                status = st.status("Running workflow")
                progress_bar = st.progress(0.0)

                def emit(message: str, progress: float | None = None):
                    logs.append(message)
                    status.write(message)
                    if progress is not None:
                        progress_bar.progress(progress)

                try:
                    execute_full_car_identification(device_serial, callback=emit)
                    execute_dtc_scan_and_report(device_serial, callback=emit)
                    execute_dtc_clear_sequence(device_serial, callback=emit)
                    status.update(label="Workflow completed", state="complete", expanded=False)
                    progress_bar.progress(1.0)
                except Exception as exc:
                    status.update(label="Workflow failed", state="error", expanded=False)
                    logs.append(f"ERROR: {exc}")
                st.text_area("Execution log", "\n".join(logs), height=220)

    with workflow_col2:
        if st.button("Execute Oil Reset"):
            logs = []
            status = st.status("Executing Oil Reset")
            progress_bar = st.progress(0.0)

            def emit(message: str, progress: float | None = None):
                logs.append(message)
                status.write(message)
                if progress is not None:
                    progress_bar.progress(progress)

            try:
                execute_service_reset(device_serial, "Oil Reset", callback=emit)
                status.update(label="Oil Reset completed", state="complete", expanded=False)
                progress_bar.progress(1.0)
            except Exception as exc:
                status.update(label="Oil Reset failed", state="error", expanded=False)
                logs.append(f"ERROR: {exc}")
            st.text_area("Execution log", "\n".join(logs), height=220)

    with workflow_col3:
        if st.button("Export PDF Report"):
            logs = []
            status = st.status("Exporting report")
            progress_bar = st.progress(0.0)

            def emit(message: str, progress: float | None = None):
                logs.append(message)
                status.write(message)
                if progress is not None:
                    progress_bar.progress(progress)

            try:
                execute_dtc_scan_and_report(device_serial, callback=emit)
                status.update(label="Report export completed", state="complete", expanded=False)
                progress_bar.progress(1.0)
            except Exception as exc:
                status.update(label="Report export failed", state="error", expanded=False)
                logs.append(f"ERROR: {exc}")
            st.text_area("Execution log", "\n".join(logs), height=220)

    with workflow_col4:
        if st.button("Launch X431"):
            with st.spinner("Launching X431 Euro Link..."):
                try:
                    result = perform_action(device_serial, "launch_x431")
                    st.success("X431 Euro Link app launched")
                    st.code(result)
                except Exception as exc:
                    st.error(f"Failed to launch X431: {exc}")

    st.markdown("---")
    st.subheader("Vision Agent")
    vision_prompt = st.text_area(
        "Vision Agent Prompt",
        value="Run the full pre-scan and look for clear fault memory actions.",
        height=100,
    )
    if st.button("Run Vision Agent"):
        if not vision_prompt.strip():
            st.warning("Please enter a goal for the vision agent.")
        else:
            logs = []
            status_area = st.empty()
            log_area = st.empty()

            def emit(message: str) -> None:
                logs.append(message)
                status_area.info(message)
                log_area.text("\n".join(logs))

            try:
                status_area.info("Starting vision agent...")
                agent = VisionAgent(device_serial, vision_prompt, callback=emit)
                steps = agent.run()
                status_area.success("Vision agent completed")
                log_area.text("\n".join(logs) if logs else "No logs generated.")
                st.markdown("**Vision Agent Actions**")
                for step in steps:
                    st.write(f"{step['attempt']}: **{step['action']}** -> {step.get('target', '')} — {step.get('reason', '')}")
            except Exception as exc:
                status_area.error("Vision agent failed")
                st.error(f"Vision agent failed: {exc}")

    st.markdown("---")
    st.subheader("Manual Actions")
    manual_col1, manual_col2, manual_col3 = st.columns(3)
    with manual_col1:
        if st.button("Auto-VIN Scan"):
            with st.spinner("Triggering Auto-VIN scan..."):
                try:
                    result = perform_action(device_serial, "auto_vin")
                    st.success("Auto-VIN scan triggered")
                    st.code(result)
                except Exception as exc:
                    st.error(f"Failed to trigger Auto-VIN: {exc}")
    with manual_col2:
        if st.button("System Scan"):
            with st.spinner("Starting system diagnostic scan..."):
                try:
                    result = perform_action(device_serial, "system_scan")
                    st.success("System scan initiated")
                    st.code(result)
                except Exception as exc:
                    st.error(f"Failed to start system scan: {exc}")
    with manual_col3:
        if st.button("Clear Memory"):
            with st.spinner("Clearing diagnostic memory..."):
                try:
                    result = perform_action(device_serial, "clear_memory")
                    st.success("Clear memory command sent")
                    st.code(result)
                except Exception as exc:
                    st.error(f"Failed to clear memory: {exc}")
else:
    st.warning("No connected ADB devices found. Ensure the tablet is plugged in and ADB is enabled. Click 'Sync ADB devices' to refresh.")

st.markdown("---")
st.info(
    "**State-driven diagnostics:** The controller now inspects the UI hierarchy and uses text matching to navigate common Launch X431 screens instead of relying only on hard-coded taps."
)
