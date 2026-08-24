# LKQ Remote Support Dashboard

Streamlit ops dashboard for LKQ Remote Support: monitor tickets, automate Launch X431 Euro Link on a USB/Wi-Fi tablet, audit VIN detections, and debug stuck controller sessions — **fully local** (no OpenAI/Claude keys).

## Features
- Live Ticket Monitor for connected cars and automation status (real data only)
- Launch X431 ADB Controller powered by `uiautomator2` + local EasyOCR/OpenCV
- Multi-step workflows: Auto-VIN, Diagnostic, System Topology ECU scan, DTC / service flows
- Adaptive local agent loop with dialog dismissal and OCR recovery
- Admin page: DB management, VIN/ticket/report audit, live session diagnose when stuck

## Architecture
```
main.py
pages/          # Streamlit pages
modules/
  adb_controller.py      # ADB + u2 connection lifecycle
  local_ocr_vision.py    # EasyOCR + OpenCV template match
  x431_workflows.py      # Diagnostic state machines
  vag_workflows.py       # VAG auto-detect + topology scan
  session_debug.py       # Stuck/error session diagnostics
  db.py                  # SQLite persistence
src/agent/
  local_agent_loop.py    # Perception + recovery + task routing
```

## Run
1. Create and activate a virtual environment
2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3. Ensure Android platform-tools (`adb`) is on PATH and the Launch tablet has USB debugging enabled.

4. Launch:

```bash
streamlit run main.py
```

Optional: set `ADB`, `ADB_PATH`, `ANDROID_HOME`, or `ANDROID_SDK_ROOT` if adb is not on PATH.

### Tablet connection (USB or Wi-Fi)

- **USB:** plug in → **Sync ADB** on the Launch X431 page.
- **Same Wi-Fi (no cable):** PC and tablet on the same LAN.
  1. First time: connect USB once → **Switch USB → Wi-Fi** (enables `adb tcpip` and connects to the tablet IP).
  2. Unplug USB; keep using the `IP:5555` device in the list.
  3. Later sessions: enter the tablet IP → **Connect Wi-Fi** (after a tablet reboot, USB is needed once again to re-enable TCP/IP).

## Notes
- First EasyOCR run downloads language models locally (one-time).
- `torch` is required by EasyOCR; CPU wheels are fine for this project.
