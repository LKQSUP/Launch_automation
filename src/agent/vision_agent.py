import base64
import io
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "lkq_remote_support.db"


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
    fallback = os.getenv("ADB_PATH") or "adb"
    return fallback


def _run_adb(args: List[str], timeout: int = 15, binary: bool = False) -> subprocess.CompletedProcess:
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


def _read_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vision_agent_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, goal TEXT, action TEXT, detail TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vision_agent_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, goal TEXT, action TEXT, success INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    return conn


def record_step(serial: str, goal: str, action: str, detail: str) -> None:
    conn = _read_db()
    try:
        conn.execute(
            "INSERT INTO vision_agent_steps (serial, goal, action, detail) VALUES (?, ?, ?, ?)",
            (serial, goal, action, detail),
        )
        conn.commit()
    finally:
        conn.close()


def record_memory(serial: str, goal: str, action: str, success: bool) -> None:
    conn = _read_db()
    try:
        conn.execute(
            "INSERT INTO vision_agent_memory (serial, goal, action, success) VALUES (?, ?, ?, ?)",
            (serial, goal, action, int(success)),
        )
        conn.commit()
    finally:
        conn.close()


def list_devices() -> List[str]:
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
        logger.warning("ADB device listing failed: %s", exc)
        return []


def capture_screenshot(serial: str, output_path: Optional[Path] = None) -> Optional[Path]:
    try:
        if output_path is None:
            output_path = Path(tempfile.gettempdir()) / f"launch_{int(time.time())}.png"
        proc = _run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=15, binary=True)
        if proc.returncode != 0:
            return None
        output_path.write_bytes(proc.stdout)
        return output_path
    except Exception as exc:
        logger.warning("Screenshot capture failed: %s", exc)
        return None


def capture_screenshot_bytes(serial: str) -> Optional[bytes]:
    try:
        proc = _run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=15, binary=True)
        if proc.returncode != 0:
            logger.warning("Screenshot capture failed, return code %s", proc.returncode)
            return None
        return proc.stdout
    except Exception as exc:
        logger.warning("Screenshot capture failed: %s", exc)
        return None


def dump_ui_xml(serial: str, output_path: Optional[Path] = None) -> Optional[Path]:
    try:
        if output_path is None:
            output_path = Path(tempfile.gettempdir()) / f"ui_{int(time.time())}.xml"
        _run_adb(["-s", serial, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], timeout=20)
        proc = _run_adb(["-s", serial, "pull", "/sdcard/window_dump.xml", str(output_path)], timeout=20)
        if proc.returncode != 0:
            return None
        return output_path if output_path.exists() else None
    except Exception as exc:
        logger.warning("UI dump failed: %s", exc)
        return None


def _parse_bounds(text: str) -> Optional[Tuple[int, int, int, int]]:
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))


def tap(serial: str, x: int, y: int) -> None:
    _run_adb(["-s", serial, "shell", "input", "tap", str(x), str(y)], timeout=5)


def click_text(serial: str, target_text: str) -> bool:
    xml_path = dump_ui_xml(serial)
    if not xml_path or not xml_path.exists():
        return False
    try:
        import xml.etree.ElementTree as ET

        tree = ET.parse(xml_path)
        root = tree.getroot()
        target = target_text.lower()
        for node in root.iter():
            text = (node.attrib.get("text") or "")
            content_desc = (node.attrib.get("content-desc") or "")
            combined = f"{text} {content_desc}".lower()
            if target in combined:
                bounds = _parse_bounds(node.attrib.get("bounds", ""))
                if not bounds:
                    continue
                x = (bounds[0] + bounds[2]) // 2
                y = (bounds[1] + bounds[3]) // 2
                tap(serial, x, y)
                return True
    except Exception as exc:
        logger.warning("Click text failed: %s", exc)
    return False


def wait_for_text(serial: str, target_text: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if click_text(serial, target_text):
            return True
        time.sleep(1)
    return False


def dismiss_popups(serial: str) -> List[str]:
    prompts = ["ok", "yes", "confirm", "allow", "continue", "turn ignition on", "connect charger", "security gateway"]
    matched = []
    for prompt in prompts:
        if click_text(serial, prompt):
            matched.append(prompt)
            time.sleep(1)
    return matched


def _call_openai_vision(image_bytes: bytes, prompt: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"action": "done", "reason": "No OPENAI_API_KEY configured"}
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": "You are an automotive UI agent. Return JSON with action, target, reason. Actions: click_text, click_coords, wait, done.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(image_bytes).decode('utf-8')}"}},
                ],
            },
        ],
        "temperature": 0.2,
    }
    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    try:
        import json

        return json.loads(content)
    except Exception:
        return {"action": "done", "reason": content}


class VisionAgent:
    def __init__(self, serial: str, goal: str, callback: Optional[Callable[[str], None]] = None) -> None:
        self.serial = serial
        self.goal = goal
        self.callback = callback

    def notify(self, message: str) -> None:
        if self.callback:
            self.callback(message)

    def run(self) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        self.notify("Starting vision agent")
        for attempt in range(6):
            screenshot = capture_screenshot_bytes(self.serial)
            if not screenshot:
                self.notify("Unable to capture screenshot")
                break
            prompt = (
                f"Goal: {self.goal}. "
                "Inspect the tablet screen and choose the best next action. "
                "Return JSON with action: click_text, click_coords, wait, or done; target: element or coordinates; reason: brief explanation."
            )
            decision = _call_openai_vision(screenshot, prompt)
            action = decision.get("action", "done")
            target = decision.get("target") or ""
            reason = decision.get("reason") or ""
            steps.append({"attempt": attempt + 1, "action": action, "target": target, "reason": reason})
            record_step(self.serial, self.goal, action, f"target={target}; reason={reason}")

            if action == "click_text":
                if click_text(self.serial, str(target)):
                    self.notify(f"Clicked text: {target}")
                    record_memory(self.serial, self.goal, f"click_text:{target}", True)
                else:
                    self.notify(f"Could not find text: {target}")
                    record_memory(self.serial, self.goal, f"click_text:{target}", False)
            elif action == "click_coords":
                try:
                    coords = str(target).split(",")
                    if len(coords) == 2:
                        tap(self.serial, int(coords[0]), int(coords[1]))
                    self.notify(f"Tapped coordinates: {target}")
                except Exception as exc:
                    self.notify(f"Coordinate tap failed: {exc}")
            elif action == "wait":
                time.sleep(2)
                self.notify("Waiting for the UI to settle")
            else:
                self.notify("Completed or stopped")
                break

            dismiss_popups(self.serial)
            time.sleep(1)

        return steps
