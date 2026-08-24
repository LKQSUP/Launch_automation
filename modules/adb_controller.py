"""ADB / uiautomator2 connection lifecycle and low-level device helpers.

Workflow state machines live in ``modules.x431_workflows``. This module owns
device discovery, reconnect logic, screenshots, and shell/input primitives.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_U2_CONNECT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="u2connect")

ProgressCallback = Callable[[str, Optional[float]], None]

# Friendly labels for ADB serials (UI only; workflows still use the real serial).
_ALIASES_PATH = Path(__file__).resolve().parent.parent / "config" / "adb_aliases.json"
_DEFAULT_DEVICE_LABEL = "Launch_Osias"

# Known Launch X431 package candidates (Euro Link / regional variants).
# Device-discovered packages are preferred at runtime via discover_x431_packages().
X431_PACKAGES = (
    "com.cnlaunch.x431.europro5",
    "com.cnlaunch.x431euro",
    "com.cnlaunch.x431pro",
    "com.launch.x431eurolink",
    "com.launch.x431pad",
    "com.launch.x431",
)


def _resolve_adb_executable() -> str:
    """Locate the adb binary from env vars, SDK paths, or PATH."""
    env_candidates: List[str] = []
    for env_name in ("ADB", "ADB_PATH", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.getenv(env_name)
        if not value:
            continue
        if env_name in ("ADB", "ADB_PATH") and os.path.isfile(value):
            env_candidates.append(value)
        else:
            env_candidates.append(os.path.join(value, "platform-tools", "adb.exe"))
            env_candidates.append(os.path.join(value, "platform-tools", "adb"))

    common_candidates = [
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb"),
        r"C:\Android\Sdk\platform-tools\adb.exe",
        r"C:\Android\Sdk\platform-tools\adb",
    ]

    for candidate in env_candidates + common_candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    resolved = shutil.which("adb") or shutil.which("adb.exe")
    return resolved or "adb"


def _run_adb(
    args: List[str],
    timeout: int = 15,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    """Run an adb subprocess with PATH patched for the resolved binary."""
    adb_path = _resolve_adb_executable()
    env = os.environ.copy()
    adb_dir = os.path.dirname(adb_path)
    if adb_dir and adb_dir not in env.get("PATH", ""):
        env["PATH"] = f"{adb_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [adb_path, *args],
        capture_output=True,
        text=not binary,
        timeout=timeout,
        env=env,
    )


def load_device_aliases() -> Dict[str, str]:
    """Load serial → friendly label map from disk."""
    try:
        if not _ALIASES_PATH.is_file():
            return {}
        data = json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}
    except Exception as exc:
        logger.warning("Failed to load ADB aliases: %s", exc)
        return {}


def save_device_aliases(aliases: Dict[str, str]) -> None:
    """Persist serial → friendly label map."""
    try:
        _ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
        cleaned = {str(k).strip(): str(v).strip() for k, v in aliases.items() if str(k).strip() and str(v).strip()}
        _ALIASES_PATH.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save ADB aliases: %s", exc)


def get_device_label(serial: str) -> str:
    """Return friendly label for ``serial``, or the serial itself."""
    serial = (serial or "").strip()
    if not serial:
        return serial
    return load_device_aliases().get(serial, serial)


def set_device_alias(serial: str, label: str) -> str:
    """Set or clear a friendly label for ``serial``. Returns the stored label."""
    serial = (serial or "").strip()
    label = (label or "").strip()
    if not serial:
        return ""
    aliases = load_device_aliases()
    if not label or label == serial:
        aliases.pop(serial, None)
        save_device_aliases(aliases)
        return serial
    # Keep labels unique: drop the same label from other serials.
    aliases = {s: name for s, name in aliases.items() if name != label}
    aliases[serial] = label
    save_device_aliases(aliases)
    return label


def ensure_default_device_alias(
    serials: List[str],
    default_label: str = _DEFAULT_DEVICE_LABEL,
) -> None:
    """Assign ``default_label`` to the first unlabeled device if the label is free."""
    serials = [s for s in serials if s]
    if not serials or not default_label:
        return
    aliases = load_device_aliases()
    if default_label in aliases.values():
        return
    for serial in serials:
        if serial not in aliases:
            set_device_alias(serial, default_label)
            return


def list_devices() -> List[str]:
    """Return serials of ADB devices currently in ``device`` state."""
    try:
        proc = _run_adb(["devices", "-l"], timeout=10)
        devices: List[str] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("List") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        ensure_default_device_alias(devices)
        return devices
    except Exception as exc:
        logger.warning("ADB list devices failed: %s", exc)
        return []


def sync_adb_devices(timeout: int = 10) -> Dict[str, Any]:
    """Start the ADB server, scan for devices, and return a structured result."""
    steps: List[str] = []
    try:
        steps.append("Resolving adb executable and starting server")
        _run_adb(["start-server"], timeout=8)
        steps.append("Scanning for USB / Wi-Fi connected devices")
        proc = _run_adb(["devices", "-l"], timeout=timeout)
        raw = proc.stdout or proc.stderr or ""
        steps.append("ADB scan completed")

        devices: List[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])

        ensure_default_device_alias(devices)
        steps.append(f"Found {len(devices)} device(s)" if devices else "No devices found")
        return {"ok": True, "steps": steps, "raw": raw, "devices": devices}
    except Exception as exc:
        steps.append(f"Error while running adb: {exc}")
        return {"ok": False, "steps": steps, "raw": "", "devices": []}


_WIFI_SERIAL_RE = re.compile(
    r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?$"
    r"|^\[[0-9a-fA-F:]+\]:\d{1,5}$"
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def is_wifi_serial(serial: str) -> bool:
    """True when the ADB serial is a network endpoint (``host:port``)."""
    serial = (serial or "").strip()
    if not serial or ":" not in serial:
        return False
    return bool(_WIFI_SERIAL_RE.match(serial))


def connection_mode(serial: str) -> str:
    """Return ``wifi`` or ``usb`` for display / branching."""
    return "wifi" if is_wifi_serial(serial) else "usb"


def _normalize_wifi_endpoint(host: str, port: int = 5555) -> str:
    """Normalize user input into ``host:port`` for ``adb connect``."""
    host = (host or "").strip()
    if not host:
        raise ValueError("Host / IP is required")
    if host.startswith("[") and "]:" in host:
        return host
    if _WIFI_SERIAL_RE.match(host) and host.count(":") == 1:
        return host
    # Bare IPv4 or hostname → append default ADB port.
    if ":" not in host:
        return f"{host}:{int(port)}"
    # Hostname:port
    return host


def get_device_wifi_ip(serial: str) -> Optional[str]:
    """Best-effort Wi-Fi IPv4 for a connected device (USB or already wireless)."""
    serial = (serial or "").strip()
    if not serial:
        return None

    # Already connected over Wi-Fi — serial is the endpoint.
    if is_wifi_serial(serial):
        return serial.rsplit(":", 1)[0].strip("[]")

    probes = [
        ["-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
        ["-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan1"],
        ["-s", serial, "shell", "ip", "route"],
        ["-s", serial, "shell", "getprop", "dhcp.wlan0.ipaddress"],
        ["-s", serial, "shell", "getprop", "dhcp.eth0.ipaddress"],
    ]
    for args in probes:
        try:
            proc = _run_adb(args, timeout=8)
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            for match in _IPV4_RE.finditer(text):
                ip = match.group(0)
                if ip.startswith("127.") or ip.endswith(".255"):
                    continue
                # Skip obvious netmasks mistaken as IPs (255.x.x.x).
                if ip.startswith("255."):
                    continue
                return ip
        except Exception as exc:
            logger.debug("Wi-Fi IP probe failed (%s): %s", args[-1:], exc)
    return None


def enable_adb_tcpip(serial: str, port: int = 5555) -> Dict[str, Any]:
    """Switch a USB-connected device into TCP/IP listen mode (``adb tcpip``)."""
    serial = (serial or "").strip()
    port = int(port)
    steps: List[str] = []
    try:
        if not serial:
            return {"ok": False, "steps": ["No device serial"], "port": port}
        if is_wifi_serial(serial):
            return {
                "ok": True,
                "steps": [f"{serial} is already a Wi-Fi endpoint"],
                "port": port,
                "serial": serial,
            }
        steps.append(f"Enabling TCP/IP mode on port {port}")
        proc = _run_adb(["-s", serial, "tcpip", str(port)], timeout=15)
        raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if raw:
            steps.append(raw)
        if proc.returncode != 0:
            return {"ok": False, "steps": steps, "raw": raw, "port": port, "serial": serial}
        # Give adbd a moment to restart in TCP mode.
        time.sleep(1.5)
        steps.append("TCP/IP mode enabled — tablet stays reachable after USB unplug")
        return {"ok": True, "steps": steps, "raw": raw, "port": port, "serial": serial}
    except Exception as exc:
        steps.append(f"tcpip failed: {exc}")
        return {"ok": False, "steps": steps, "port": port, "serial": serial}


def connect_adb_wifi(host: str, port: int = 5555) -> Dict[str, Any]:
    """Connect to a tablet on the same LAN via ``adb connect host:port``."""
    steps: List[str] = []
    try:
        endpoint = _normalize_wifi_endpoint(host, port=port)
        steps.append("Starting ADB server")
        _run_adb(["start-server"], timeout=8)
        steps.append(f"Connecting to {endpoint}")
        proc = _run_adb(["connect", endpoint], timeout=20)
        raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if raw:
            steps.append(raw)
        low = raw.lower()
        ok = proc.returncode == 0 and ("connected to" in low or "already connected" in low)
        if not ok and "failed" in low:
            ok = False
        devices = list_devices()
        if endpoint in devices:
            ok = True
        # Prefer the connected endpoint for aliases / selection.
        if ok:
            ensure_default_device_alias([endpoint])
            steps.append(f"Wi-Fi device ready: {endpoint}")
        else:
            steps.append("Connect did not report success — check IP, same Wi-Fi, and USB debugging")
        return {
            "ok": ok,
            "steps": steps,
            "raw": raw,
            "endpoint": endpoint,
            "devices": devices,
        }
    except Exception as exc:
        steps.append(f"Wi-Fi connect failed: {exc}")
        return {"ok": False, "steps": steps, "raw": "", "endpoint": "", "devices": list_devices()}


def disconnect_adb_wifi(endpoint: Optional[str] = None) -> Dict[str, Any]:
    """Disconnect one Wi-Fi endpoint, or all wireless connections if omitted."""
    steps: List[str] = []
    try:
        endpoint = (endpoint or "").strip() or None
        if endpoint:
            endpoint = _normalize_wifi_endpoint(endpoint)
            steps.append(f"Disconnecting {endpoint}")
            proc = _run_adb(["disconnect", endpoint], timeout=10)
        else:
            steps.append("Disconnecting all Wi-Fi ADB sessions")
            proc = _run_adb(["disconnect"], timeout=10)
        raw = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if raw:
            steps.append(raw)
        devices = list_devices()
        return {
            "ok": proc.returncode == 0,
            "steps": steps,
            "raw": raw,
            "devices": devices,
        }
    except Exception as exc:
        steps.append(f"Disconnect failed: {exc}")
        return {"ok": False, "steps": steps, "raw": "", "devices": list_devices()}


def switch_usb_to_wifi(serial: str, port: int = 5555) -> Dict[str, Any]:
    """One-shot: read Wi-Fi IP → ``adb tcpip`` → ``adb connect`` (then USB can unplug).

    Requires the tablet to already be on the same LAN as the PC. After success,
    workflows can use the returned ``endpoint`` (``IP:port``) as the ADB serial.
    """
    serial = (serial or "").strip()
    port = int(port)
    steps: List[str] = []
    try:
        if not serial:
            return {"ok": False, "steps": ["No USB device selected"], "endpoint": "", "devices": []}
        if is_wifi_serial(serial):
            steps.append(f"Already on Wi-Fi: {serial}")
            return {
                "ok": True,
                "steps": steps,
                "endpoint": serial,
                "devices": list_devices(),
                "ip": serial.rsplit(":", 1)[0],
            }

        steps.append(f"Reading tablet Wi-Fi IP from {serial}")
        ip = get_device_wifi_ip(serial)
        if not ip:
            return {
                "ok": False,
                "steps": steps + ["Could not read Wi-Fi IP — join the same network on the tablet"],
                "endpoint": "",
                "devices": list_devices(),
            }
        steps.append(f"Tablet Wi-Fi IP: {ip}")

        tcp = enable_adb_tcpip(serial, port=port)
        steps.extend(tcp.get("steps") or [])
        if not tcp.get("ok"):
            return {
                "ok": False,
                "steps": steps,
                "endpoint": "",
                "ip": ip,
                "devices": list_devices(),
            }

        conn = connect_adb_wifi(ip, port=port)
        steps.extend(conn.get("steps") or [])
        endpoint = conn.get("endpoint") or f"{ip}:{port}"

        # Carry the friendly label from the USB serial onto the Wi-Fi endpoint.
        if conn.get("ok"):
            usb_label = get_device_label(serial)
            if usb_label and usb_label != serial:
                set_device_alias(endpoint, usb_label)
            steps.append("You can unplug USB; keep using the Wi-Fi device in the list")

        return {
            "ok": bool(conn.get("ok")),
            "steps": steps,
            "endpoint": endpoint if conn.get("ok") else "",
            "ip": ip,
            "devices": conn.get("devices") or list_devices(),
            "raw": conn.get("raw") or "",
        }
    except Exception as exc:
        steps.append(f"USB→Wi-Fi switch failed: {exc}")
        return {"ok": False, "steps": steps, "endpoint": "", "devices": list_devices()}


def get_device_state(serial: str) -> Dict[str, Any]:
    """Probe connectivity and product model for ``serial``."""
    try:
        proc = _run_adb(["-s", serial, "shell", "echo", "ok"], timeout=5)
        connected = proc.returncode == 0 and "ok" in (proc.stdout or "")
        product = "Unknown"
        if connected:
            model_proc = _run_adb(["-s", serial, "shell", "getprop", "ro.product.model"], timeout=5)
            product = (model_proc.stdout or "").strip() or "Unknown"
        return {"connected": connected, "serial": serial, "product": product}
    except Exception as exc:
        logger.warning("ADB device state error: %s", exc)
        return {"connected": False, "serial": serial, "product": "Unknown", "error": str(exc)}


def get_device_info(serial: str) -> Dict[str, Any]:
    """Fetch model, Android version, and build fingerprint."""
    try:
        model = _run_adb(["-s", serial, "shell", "getprop", "ro.product.model"], timeout=5)
        android = _run_adb(["-s", serial, "shell", "getprop", "ro.build.version.release"], timeout=5)
        build = _run_adb(["-s", serial, "shell", "getprop", "ro.build.fingerprint"], timeout=5)
        if model.returncode == 0 and android.returncode == 0:
            return {
                "model": (model.stdout or "").strip(),
                "android_version": (android.stdout or "").strip(),
                "build_fingerprint": (build.stdout or "").strip(),
            }
        raise RuntimeError("Could not fetch device properties")
    except Exception as exc:
        return {"error": str(exc)}


def shell(serial: str, command: str, timeout: int = 10) -> str:
    """Execute a shell command on the device and return stdout."""
    try:
        proc = _run_adb(["-s", serial, "shell", *command.split()], timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "ADB command failed")
        return (proc.stdout or "").strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to execute shell command: {exc}") from exc


def tap(serial: str, x: int, y: int) -> str:
    """Send a tap at ``(x, y)`` via ``input tap``."""
    try:
        _run_adb(["-s", serial, "shell", "input", "tap", str(x), str(y)], timeout=5)
        return f"Tap at ({x}, {y}) sent"
    except Exception as exc:
        raise RuntimeError(f"Failed to tap: {exc}") from exc


def launch_app(serial: str, package: str, activity: Optional[str] = None) -> str:
    """Launch an Android app by package (and optional activity)."""
    try:
        if activity:
            proc = _run_adb(
                ["-s", serial, "shell", "am", "start", "-n", f"{package}/{activity}"],
                timeout=10,
            )
        else:
            proc = _run_adb(
                [
                    "-s",
                    serial,
                    "shell",
                    "monkey",
                    "-p",
                    package,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
                timeout=10,
            )
        if proc.returncode == 0:
            return f"Launched {package}"
        raise RuntimeError(proc.stderr or proc.stdout or "Failed to launch app")
    except Exception as exc:
        raise RuntimeError(f"Failed to launch app: {exc}") from exc


def discover_x431_packages(serial: str) -> List[str]:
    """Return installed packages that look like Launch X431 / Euro Link apps."""
    try:
        proc = _run_adb(["-s", serial, "shell", "pm", "list", "packages"], timeout=30)
        found: List[str] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            pkg = line.split("package:", 1)[1].strip()
            low = pkg.lower()
            if "x431" in low or "eurolink" in low or "europro" in low:
                # Prefer main diagnostic app over scanners/tools.
                found.append(pkg)
        # Rank: europro / euro link first, then other x431, scanners last.
        def rank(pkg: str) -> tuple:
            low = pkg.lower()
            if "europro" in low or "eurolink" in low or "euro.link" in low:
                return (0, low)
            if "scanner" in low or "tools" in low or "screencast" in low:
                return (2, low)
            return (1, low)

        found = sorted(set(found), key=rank)
        return found
    except Exception as exc:
        logger.warning("Package discovery failed: %s", exc)
        return []


def launch_x431(serial: str) -> str:
    """Launch the installed X-431 EURO LINK app (auto-discover package).

    Resolves the exported launcher activity (e.g. ``.WelcomeActivity``) when
    possible; falls back to monkey. ``MainActivity`` is often not exported.
    """
    candidates = discover_x431_packages(serial) + list(X431_PACKAGES)
    seen: set[str] = set()
    ordered: List[str] = []
    for pkg in candidates:
        if pkg not in seen:
            seen.add(pkg)
            ordered.append(pkg)

    errors: List[str] = []
    for package in ordered:
        # Prefer the real launcher activity (WelcomeActivity on Euro Link V8).
        try:
            proc = _run_adb(
                ["-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief", package],
                timeout=10,
            )
            component = None
            for ln in (proc.stdout or "").splitlines():
                ln = ln.strip()
                if "/" in ln and not ln.startswith("priority=") and not ln.startswith("specific"):
                    component = ln
                    break
            if component:
                raw = _run_adb(
                    ["-s", serial, "shell", "am", "start", "-n", component],
                    timeout=10,
                )
                combined = (raw.stdout or "") + (raw.stderr or "")
                if raw.returncode == 0 and "Exception" not in combined and "Error:" not in combined:
                    return f"Launched {component}"
                errors.append(f"{component}: {combined.strip()[:200]}")
        except Exception as exc:
            errors.append(f"{package} resolve: {exc}")

        try:
            return launch_app(serial, package)
        except Exception as exc:
            errors.append(f"{package}: {exc}")

    raise RuntimeError("Unable to launch X431. Tried: " + "; ".join(errors[:8]))


def force_stop_x431(serial: str) -> List[str]:
    """Force-stop known EURO LINK packages on the tablet."""
    steps: List[str] = []
    packages = discover_x431_packages(serial) + list(X431_PACKAGES)
    seen: set[str] = set()
    for package in packages:
        if package in seen:
            continue
        seen.add(package)
        try:
            proc = _run_adb(
                ["-s", serial, "shell", "am", "force-stop", package],
                timeout=8,
            )
            steps.append(f"force-stop {package} (rc={proc.returncode})")
        except Exception as exc:
            steps.append(f"force-stop {package} failed: {exc}")
    return steps


def hard_reset_x431_session(serial: str, relaunch: bool = True) -> Dict[str, Any]:
    """Hard-reset automation session on the tablet and optionally reopen EURO LINK home.

    Use when auto-detect is stuck: force-stop the app, clear u2-related agent if possible,
    then relaunch WelcomeActivity / home.
    """
    steps: List[str] = []
    out: Dict[str, Any] = {"ok": False, "steps": steps, "serial": serial, "launched": None}

    steps.append(f"Hard reset for {serial}")
    steps.extend(force_stop_x431(serial))
    time.sleep(0.6)

    # Best-effort: stop uiautomator / atx helper processes that can wedge exists()/dump.
    for pkg in ("com.github.uiautomator", "com.github.uiautomator.test"):
        try:
            _run_adb(["-s", serial, "shell", "am", "force-stop", pkg], timeout=5)
            steps.append(f"force-stop {pkg}")
        except Exception as exc:
            steps.append(f"skip {pkg}: {exc}")

    # Wake screen + home key so we leave any overlay.
    try:
        _run_adb(["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=4)
        _run_adb(["-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME"], timeout=4)
        steps.append("Wake + Home")
    except Exception as exc:
        steps.append(f"Wake/Home skip: {exc}")

    time.sleep(0.5)
    if relaunch:
        try:
            msg = launch_x431(serial)
            out["launched"] = msg
            steps.append(msg)
            time.sleep(1.2)
        except Exception as exc:
            steps.append(f"Relaunch failed: {exc}")
            out["ok"] = False
            return out

    out["ok"] = True
    steps.append("Hard reset complete — ready for a fresh Start auto detection")
    return out


def perform_action(serial: str, action: str) -> str:
    """Execute a single named low-level action (compat helper)."""
    if action == "launch_x431":
        return launch_x431(serial)
    if action == "auto_vin":
        return tap(serial, 540, 600)
    if action == "system_scan":
        return tap(serial, 540, 900)
    if action == "clear_memory":
        return tap(serial, 540, 1200)
    return "unknown action"


def capture_screenshot(serial: str, output_path: Optional[Path] = None) -> Optional[Path]:
    """Capture a device screenshot to a local PNG path."""
    try:
        if output_path is None:
            output_path = Path(tempfile.gettempdir()) / f"x431_{serial}_{int(time.time())}.png"
        proc = _run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=20, binary=True)
        if proc.returncode != 0 or not proc.stdout:
            return None
        output_path.write_bytes(proc.stdout)
        return output_path if output_path.exists() else None
    except Exception as exc:
        logger.warning("Screenshot capture failed: %s", exc)
        return None


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))


def _canonical(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def dump_ui_hierarchy(serial: str, output_path: Optional[Path] = None) -> Optional[Path]:
    """Dump the Android UI hierarchy XML and return the local path."""
    try:
        if output_path is None:
            output_path = Path(tempfile.gettempdir()) / f"ui_dump_{int(time.time())}.xml"
        remote_path = "/sdcard/window_dump.xml"
        _run_adb(["-s", serial, "shell", "uiautomator", "dump", remote_path], timeout=25)
        _run_adb(["-s", serial, "pull", remote_path, str(output_path)], timeout=20)
        return output_path if output_path.exists() else None
    except Exception as exc:
        logger.warning("UI dump failed: %s", exc)
        return None


def get_ui_elements(serial: str) -> List[Dict[str, str]]:
    """Parse the current UI dump into text/bounds dictionaries."""
    try:
        xml_path = dump_ui_hierarchy(serial)
        if not xml_path or not xml_path.exists():
            return []
        tree = ET.parse(xml_path)
        root = tree.getroot()
        elements: List[Dict[str, str]] = []
        for node in root.iter():
            text = node.attrib.get("text") or node.attrib.get("content-desc") or ""
            bounds = node.attrib.get("bounds") or ""
            if text or bounds:
                elements.append(
                    {
                        "text": text,
                        "content_desc": node.attrib.get("content-desc", ""),
                        "resource_id": node.attrib.get("resource-id", ""),
                        "bounds": bounds,
                    }
                )
        return elements
    except Exception as exc:
        logger.warning("UI parse failed: %s", exc)
        return []


def find_and_tap_text(serial: str, target_text: str, timeout: int = 10) -> bool:
    """Find a UI node containing ``target_text`` and tap its center."""
    target = _canonical(target_text)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for node in get_ui_elements(serial):
            combined = " ".join(
                [node.get("text", ""), node.get("content_desc", ""), node.get("resource_id", "")]
            )
            if target in _canonical(combined):
                bounds = _parse_bounds(node.get("bounds", ""))
                if bounds:
                    x = (bounds[0] + bounds[2]) // 2
                    y = (bounds[1] + bounds[3]) // 2
                    tap(serial, x, y)
                    return True
        time.sleep(0.8)
    return False


def wait_for_text(serial: str, target_text: str, timeout: int = 30) -> bool:
    """Poll until ``target_text`` appears in the UI hierarchy."""
    target = _canonical(target_text)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for node in get_ui_elements(serial):
            combined = " ".join(
                [node.get("text", ""), node.get("content_desc", ""), node.get("resource_id", "")]
            )
            if target in _canonical(combined):
                return True
        time.sleep(1)
    return False


def connect_u2(serial: str, reconnect: bool = False):
    """Connect a uiautomator2 device session for ``serial`` (fast path by default).

    Args:
        serial: ADB device serial.
        reconnect: If True, retry once after an adb wait (slower). Default False.

    Returns:
        A ``uiautomator2.Device`` instance.
    """
    try:
        import uiautomator2 as u2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "uiautomator2 is not installed. Run: pip install uiautomator2"
        ) from exc

    last_error: Optional[Exception] = None
    attempts = 2 if reconnect else 1
    from concurrent.futures import TimeoutError as FuturesTimeout

    for attempt in range(1, attempts + 1):
        try:
            fut = _U2_CONNECT_POOL.submit(u2.connect, serial)
            device = fut.result(timeout=8)
            logger.info("uiautomator2 connected to %s (attempt %s)", serial, attempt)
            return device
        except FuturesTimeout:
            last_error = TimeoutError("uiautomator2 connect timed out after 8s")
            logger.warning("u2 connect attempt %s timed out", attempt)
        except Exception as exc:
            last_error = exc
            logger.warning("u2 connect attempt %s failed: %s", attempt, exc)
        if attempt < attempts:
            try:
                _run_adb(["start-server"], timeout=5)
                _run_adb(["-s", serial, "wait-for-device"], timeout=8)
            except Exception:
                pass
            time.sleep(0.5)
    raise RuntimeError(f"Failed to connect uiautomator2 to {serial}: {last_error}")


def get_connection_status(serial: str) -> Dict[str, Any]:
    """Return a status card payload for ADB + uiautomator2 connectivity."""
    adb_state = get_device_state(serial)
    status: Dict[str, Any] = {
        "serial": serial,
        "adb_connected": bool(adb_state.get("connected")),
        "product": adb_state.get("product", "Unknown"),
        "u2_connected": False,
        "u2_info": {},
        "error": None,
    }
    if not status["adb_connected"]:
        status["error"] = "ADB device not reachable"
        return status

    try:
        device = connect_u2(serial, reconnect=True)
        info = device.info or {}
        status["u2_connected"] = True
        status["u2_info"] = {
            "display": f"{info.get('displayWidth', '?')}x{info.get('displayHeight', '?')}",
            "sdk": info.get("sdkInt"),
            "product": info.get("productName") or adb_state.get("product"),
        }
    except Exception as exc:
        status["error"] = str(exc)
    return status


def emit_progress(
    callback: Optional[ProgressCallback],
    message: str,
    progress: Optional[float] = None,
) -> None:
    """Safe progress callback helper for Streamlit status streaming."""
    if callback:
        try:
            callback(message, progress)
        except Exception as exc:
            logger.debug("Progress callback failed: %s", exc)
