from src.agent.vision_agent import capture_screenshot_bytes

serial = "0123456789ABCDEF"
bytes_data = capture_screenshot_bytes(serial)
if bytes_data:
    print(f"Captured {len(bytes_data)} bytes")
else:
    print("Capture failed")
