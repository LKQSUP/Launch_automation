"""Launch / stop scrcpy so operators can watch and take over the tablet.

Scrcpy is the right tool for live control (mouse/keyboard on the tablet).
Streamlit can only show a refreshed screencap preview — use both together.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# serial → running Popen
_PROCS: Dict[str, subprocess.Popen] = {}

# Wi-Fi friendly defaults (same idea as the Osias ADB Wireless & Scrcpy bat).
DEFAULT_ARGS = (
    "-b",
    "4M",
    "-m",
    "1024",
    "--max-fps",
    "30",
    "--window-title",
    "LKQ X431 tablet",
)


def find_scrcpy() -> Optional[str]:
    """Return path to scrcpy.exe / scrcpy if on PATH or in common WinGet folders."""
    which = shutil.which("scrcpy") or shutil.which("scrcpy.exe")
    if which:
        return which
    if sys.platform.startswith("win"):
        roots = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "scrcpy",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "scrcpy",
            Path.home() / "scoop" / "apps" / "scrcpy" / "current",
        ]
        for root in roots:
            if not root.exists():
                continue
            for hit in root.rglob("scrcpy.exe"):
                return str(hit)
    return None


def is_mirror_running(serial: str = "") -> bool:
    """True if we still have a live scrcpy process (optionally for ``serial``)."""
    dead: List[str] = []
    for key, proc in list(_PROCS.items()):
        if proc.poll() is not None:
            dead.append(key)
            continue
        if not serial or key == serial:
            return True
    for key in dead:
        _PROCS.pop(key, None)
    return False


def mirror_status(serial: str = "") -> Dict[str, Any]:
    exe = find_scrcpy()
    running = is_mirror_running(serial) if serial else is_mirror_running()
    pid = None
    if serial and serial in _PROCS and _PROCS[serial].poll() is None:
        pid = _PROCS[serial].pid
    return {
        "scrcpy_path": exe,
        "available": bool(exe),
        "running": running,
        "pid": pid,
        "serial": serial or None,
    }


def start_mirror(
    serial: str,
    *,
    bitrate: str = "4M",
    max_size: int = 1024,
    max_fps: int = 30,
    stay_awake: bool = True,
    turn_screen_off: bool = False,
) -> Dict[str, Any]:
    """Start scrcpy for ``serial`` in a separate window (interactive control)."""
    serial = (serial or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "pid": None,
        "scrcpy": None,
        "error": None,
        "steps": [],
    }
    if not serial:
        out["error"] = "No ADB device selected"
        return out

    exe = find_scrcpy()
    if not exe:
        out["error"] = (
            "scrcpy not found. Install with: winget install Genymobile.scrcpy "
            "or add scrcpy.exe to PATH."
        )
        return out
    out["scrcpy"] = exe
    out["steps"].append(f"Using {exe}")

    # Replace previous window for this serial.
    if serial in _PROCS and _PROCS[serial].poll() is None:
        stop_mirror(serial)
        out["steps"].append("Stopped previous scrcpy for this device")

    cmd: List[str] = [
        exe,
        "-s",
        serial,
        "-b",
        str(bitrate),
        "-m",
        str(int(max_size)),
        "--max-fps",
        str(int(max_fps)),
        "--window-title",
        f"LKQ X431 · {serial}",
    ]
    if stay_awake:
        cmd.append("--stay-awake")
    if turn_screen_off:
        cmd.append("--turn-screen-off")

    creationflags = 0
    if sys.platform.startswith("win"):
        # New console group so Streamlit exit does not always kill the mirror.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            cwd=str(Path(exe).parent),
        )
    except Exception as exc:
        out["error"] = f"Failed to start scrcpy: {exc}"
        logger.warning(out["error"])
        return out

    time.sleep(0.6)
    if proc.poll() is not None:
        out["error"] = (
            "scrcpy exited immediately — check ADB connection "
            "(USB or Wi-Fi) and that only one mirror is needed."
        )
        return out

    _PROCS[serial] = proc
    out["ok"] = True
    out["pid"] = proc.pid
    out["steps"].append(f"scrcpy running pid={proc.pid} for {serial}")
    return out


def stop_mirror(serial: str = "") -> Dict[str, Any]:
    """Stop scrcpy for one serial, or all tracked mirrors if ``serial`` empty."""
    stopped: List[str] = []
    keys = [serial] if serial else list(_PROCS.keys())
    for key in keys:
        proc = _PROCS.pop(key, None)
        if not proc:
            continue
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:
                logger.warning("stop scrcpy %s: %s", key, exc)
        stopped.append(key)
    return {"ok": True, "stopped": stopped}
