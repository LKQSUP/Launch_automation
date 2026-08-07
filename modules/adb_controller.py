import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def _resolve_adb_executable() -> str:
    env_candidates = []
    for env_name in ("ADB", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.getenv(env_name)
        if value:
            env_candidates.append(os.path.join(value, "platform-tools", "adb.exe"))
            env_candidates.append(os.path.join(value, "platform-tools", "adb"))

    common_candidates = [
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb"),
        r"C:\Android\Sdk\platform-tools\adb.exe",
        r"C:\Android\Sdk\platform-tools\adb",
        r"C:\Users\lkqse\AppData\Local\Android\Sdk\platform-tools\adb.exe",
        r"C:\Users\lkqse\AppData\Local\Android\Sdk\platform-tools\adb",
    ]

    for candidate in env_candidates + common_candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    resolved = shutil.which("adb") or shutil.which("adb.exe")
    if resolved:
        return resolved
    return "adb"


def _run_adb(args: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
    adb_path = _resolve_adb_executable()
    env = os.environ.copy()
    adb_dir = os.path.dirname(adb_path)
    if adb_dir and adb_dir not in env.get("PATH", ""):
        env["PATH"] = f"{adb_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run([adb_path, *args], capture_output=True, text=True, timeout=timeout, env=env)


def list_devices() -> List[str]:
    """Return a list of connected ADB device serials."""
    try:
        proc = _run_adb(["devices", "-l"], timeout=10)
        devices = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("List") and not line.startswith("*"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
        return devices
    except Exception as exc:
        logger.warning("ADB list devices failed: %s", exc)
        return []


def get_device_state(serial: str) -> dict:
    """Get device state and product model via adb shell commands."""
    try:
        proc = _run_adb(["-s", serial, "shell", "echo", "ok"], timeout=5)
        connected = proc.returncode == 0
        product = "Unknown"
        if connected:
            model_proc = _run_adb(["-s", serial, "shell", "getprop", "ro.product.model"], timeout=5)
            product = model_proc.stdout.strip() or "Unknown"
        return {"connected": connected, "serial": serial, "product": product}
    except Exception as exc:
        logger.warning("ADB device state error: %s", exc)
        return {"connected": False, "serial": serial}


def shell(serial: str, command: str) -> str:
    """Execute a shell command on the target device via ADB."""
    try:
        proc = _run_adb(["-s", serial, "shell", *command.split()], timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "ADB command failed")
        return proc.stdout.strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to execute shell command: {exc}")


def tap(serial: str, x: int, y: int) -> str:
    """Simulate a touch tap on the device screen."""
    try:
        _run_adb(["-s", serial, "shell", "input", "tap", str(x), str(y)], timeout=5)
        return f"Tap at ({x}, {y}) sent"
    except Exception as exc:
        raise RuntimeError(f"Failed to tap: {exc}")


def launch_app(serial: str, package: str, activity: str) -> str:
    """Launch an Android app on the device."""
    try:
        proc = _run_adb(["-s", serial, "shell", "am", "start", "-n", f"{package}/{activity}"], timeout=8)
        if proc.returncode == 0:
            return f"Launched {package}"
        raise RuntimeError(proc.stderr or proc.stdout or "Failed to launch app")
    except Exception as exc:
        raise RuntimeError(f"Failed to launch app: {exc}")


def get_device_info(serial: str) -> dict:
    """Retrieve device info: model, Android version, and build fingerprint."""
    try:
        model_proc = _run_adb(["-s", serial, "shell", "getprop", "ro.product.model"], timeout=5)
        android_proc = _run_adb(["-s", serial, "shell", "getprop", "ro.build.version.release"], timeout=5)
        build_proc = _run_adb(["-s", serial, "shell", "getprop", "ro.build.fingerprint"], timeout=5)
        if model_proc.returncode == 0 and android_proc.returncode == 0:
            return {
                "model": model_proc.stdout.strip(),
                "android_version": android_proc.stdout.strip(),
                "build_fingerprint": build_proc.stdout.strip(),
            }
        raise RuntimeError("Could not fetch device properties")
    except Exception as exc:
        return {"error": str(exc)}


def perform_action(serial: str, action: str) -> str:
    """Execute a single named automation action."""
    if action == "launch_x431":
        return launch_app(serial, "com.launch.x431", "com.launch.x431.MainActivity")
    if action == "auto_vin":
        return tap(serial, 540, 600)
    if action == "system_scan":
        return tap(serial, 540, 900)
    if action == "clear_memory":
        return tap(serial, 540, 1200)
    return "unknown action"


def sync_adb_devices(timeout: int = 10) -> dict:
    """Run adb devices -l and return structured results."""
    steps = []
    try:
        steps.append("Resolving adb executable and starting server")
        _run_adb(["start-server"], timeout=5)
        steps.append("Scanning for USB-connected devices")
        proc = _run_adb(["devices", "-l"], timeout=timeout)
        raw = proc.stdout or proc.stderr or ""
        steps.append("ADB scan completed")

        devices = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])

        if devices:
            steps.append(f"Found {len(devices)} device(s)")
        else:
            steps.append("No devices found")
        return {"ok": True, "steps": steps, "raw": raw, "devices": devices}
    except Exception as exc:
        steps.append(f"Error while running adb: {exc}")
        return {"ok": False, "steps": steps, "raw": "", "devices": []}


def _parse_bounds(bounds: str) -> Optional[tuple[int, int, int, int]]:
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)))


def _canonical(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def dump_ui_hierarchy(serial: str, output_path: Optional[Path] = None) -> Optional[Path]:
    """Dump the Android UI hierarchy and return the local XML path."""
    try:
        if output_path is None:
            output_path = Path(tempfile.gettempdir()) / f"ui_dump_{int(time.time())}.xml"
        remote_path = "/sdcard/window_dump.xml"
        _run_adb(["-s", serial, "shell", "uiautomator", "dump", remote_path], timeout=20)
        _run_adb(["-s", serial, "pull", remote_path, str(output_path)], timeout=20)
        return output_path if output_path.exists() else None
    except Exception as exc:
        logger.warning("UI dump failed: %s", exc)
        return None


def get_ui_elements(serial: str) -> List[dict]:
    """Return UI elements from the current screen as text/bounds dictionaries."""
    try:
        xml_path = dump_ui_hierarchy(serial)
        if not xml_path or not xml_path.exists():
            return []
        tree = ET.parse(xml_path)
        root = tree.getroot()
        elements = []
        for node in root.iter():
            text = node.attrib.get("text") or node.attrib.get("content-desc") or ""
            bounds = node.attrib.get("bounds") or ""
            if text or bounds:
                elements.append({"text": text, "content_desc": node.attrib.get("content-desc", ""), "resource_id": node.attrib.get("resource-id", ""), "bounds": bounds})
        return elements
    except Exception as exc:
        logger.warning("UI parse failed: %s", exc)
        return []


def find_and_tap_text(serial: str, target_text: str, timeout: int = 10) -> bool:
    """Find a UI element whose text contains target_text and tap its center."""
    target = _canonical(target_text)
    for _ in range(timeout):
        for node in get_ui_elements(serial):
            combined = " ".join([node.get("text", ""), node.get("content_desc", ""), node.get("resource_id", "")])
            if target in _canonical(combined):
                bounds = _parse_bounds(node.get("bounds", ""))
                if bounds:
                    x = (bounds[0] + bounds[2]) // 2
                    y = (bounds[1] + bounds[3]) // 2
                    tap(serial, x, y)
                    return True
        time.sleep(1)
    return False


def wait_for_text(serial: str, target_text: str, timeout: int = 30) -> bool:
    """Poll for a target text to appear on the screen."""
    target = _canonical(target_text)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for node in get_ui_elements(serial):
            combined = " ".join([node.get("text", ""), node.get("content_desc", ""), node.get("resource_id", "")])
            if target in _canonical(combined):
                return True
        time.sleep(1)
    return False


def dismiss_popups_if_present(serial: str) -> List[str]:
    """Attempt to dismiss common prompts by tapping matching text buttons."""
    prompt_actions = ["ok", "yes", "confirm", "continue", "allow", "turn ignition on", "network connection required", "connection required"]
    hit = []
    for prompt in prompt_actions:
        if find_and_tap_text(serial, prompt, timeout=2):
            hit.append(prompt)
    return hit


def _emit_progress(callback: Optional[Callable[[str, Optional[float]], None]], message: str, progress: Optional[float] = None) -> None:
    if callback:
        callback(message, progress)


def execute_full_car_identification(serial: str, callback: Optional[Callable[[str, Optional[float]], None]] = None) -> List[str]:
    """Launch X431, navigate the initial car identification workflow, and prepare for diagnostics."""
    from modules.db import save_report  # local import to avoid cycles

    log = []

    def step(message: str, progress: Optional[float] = None) -> None:
        log.append(message)
        _emit_progress(callback, message, progress)

    step("Launching Launch X431 app", 0.1)
    launch_app(serial, "com.launch.x431", "com.launch.x431.MainActivity")
    time.sleep(2)

    step("Looking for Smart Diagnosis or Vehicle Diagnosis", 0.25)
    if not find_and_tap_text(serial, "smart diagnosis") and not find_and_tap_text(serial, "vehicle diagnosis"):
        step("Primary diagnosis entry not found; continuing with generic navigation", 0.3)

    step("Attempting Auto-VIN or VIN entry flow", 0.45)
    find_and_tap_text(serial, "auto vin")
    dismiss_popups_if_present(serial)

    step("Handling confirmation prompts and system scan entry", 0.7)
    find_and_tap_text(serial, "system scan")
    find_and_tap_text(serial, "health report")
    dismiss_popups_if_present(serial)

    step("Workflow completed", 1.0)
    save_report(serial, "full_car_identification", "completed", "\n".join(log), None)
    return log


def execute_dtc_scan_and_report(serial: str, callback: Optional[Callable[[str, Optional[float]], None]] = None) -> List[str]:
    """Drive the scan/report flow, wait for completion, and export a report artifact if possible."""
    from modules.db import save_report

    log = []

    def step(message: str, progress: Optional[float] = None) -> None:
        log.append(message)
        _emit_progress(callback, message, progress)

    step("Opening scan workflow", 0.15)
    execute_full_car_identification(serial, callback=None)
    step("Looking for Health Report or Report action", 0.35)
    find_and_tap_text(serial, "health report")
    find_and_tap_text(serial, "report")
    step("Waiting for scan completion indicators", 0.6)
    wait_for_text(serial, "100%", timeout=45)
    dismiss_popups_if_present(serial)

    step("Attempting to export or save the report", 0.8)
    find_and_tap_text(serial, "export")
    find_and_tap_text(serial, "save")
    find_and_tap_text(serial, "pdf")

    report_dir = Path(__file__).resolve().parent.parent / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"dtc_report_{int(time.time())}.txt"
    report_path.write_text("\n".join(log) + "\n", encoding="utf-8")
    step(f"Report artifact saved to {report_path}", 1.0)
    save_report(serial, "dtc_scan_report", "completed", "\n".join(log), str(report_path))
    return log


def execute_dtc_clear_sequence(serial: str, callback: Optional[Callable[[str, Optional[float]], None]] = None) -> List[str]:
    """Navigate to Clear DTC and clear fault memory if it can be found."""
    from modules.db import save_report

    log = []

    def step(message: str, progress: Optional[float] = None) -> None:
        log.append(message)
        _emit_progress(callback, message, progress)

    step("Opening diagnostic functions", 0.2)
    execute_full_car_identification(serial, callback=None)
    step("Searching for Clear DTC or Fault Memory", 0.5)
    if not find_and_tap_text(serial, "clear dtc") and not find_and_tap_text(serial, "fault memory"):
        step("Clear DTC action not detected; trying generic confirmation path", 0.6)
    dismiss_popups_if_present(serial)
    step("Re-scanning to verify the state", 0.9)
    find_and_tap_text(serial, "scan")
    step("Clear sequence completed", 1.0)
    save_report(serial, "dtc_clear_sequence", "completed", "\n".join(log), None)
    return log


def execute_service_reset(serial: str, reset_type: str, callback: Optional[Callable[[str, Optional[float]], None]] = None) -> List[str]:
    """Navigate to a reset function such as Oil Reset, Brake Reset, or SAS Reset."""
    from modules.db import save_report

    log = []

    def step(message: str, progress: Optional[float] = None) -> None:
        log.append(message)
        _emit_progress(callback, message, progress)

    step(f"Preparing {reset_type} workflow", 0.2)
    execute_full_car_identification(serial, callback=None)
    reset_label = reset_type.lower().replace(" ", " ")
    labels = [reset_type, reset_type.replace("Reset", " Reset")]
    for label in labels:
        if find_and_tap_text(serial, label):
            break
    dismiss_popups_if_present(serial)
    step(f"{reset_type} workflow finished", 1.0)
    save_report(serial, reset_type.lower().replace(" ", "_"), "completed", "\n".join(log), None)
    return log
