import requests
import time
import os
import signal
from datetime import datetime

# 🔧 Config
UE_TYPE = os.getenv("UE_TYPE", "web")
SERVER_URL = os.getenv("SERVER_URL")
LOG_FILE = "ue_logs.txt"

running = True

# 📝 Logging
def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{now}] {message}"
    print(log_msg)

    with open(LOG_FILE, "a") as f:
        f.write(log_msg + "\n")

# 🛑 Graceful shutdown
def stop_handler(sig, frame):
    global running
    log("🛑 Stopping UE gracefully...")
    running = False

signal.signal(signal.SIGINT, stop_handler)

# 🚀 Core request logic
def send_request():
    try:
        if UE_TYPE == "video":
            requests.post(f"{SERVER_URL}/video", data={"size": 100})

        elif UE_TYPE == "file":
            requests.post(f"{SERVER_URL}/file", data={"size": 200})

        elif UE_TYPE == "web":
            requests.get(f"{SERVER_URL}/web")

        log(f"{UE_TYPE.upper()} request sent")

    except Exception as e:
        log(f"ERROR: {e}")

# 🔁 Main loop
log(f"🚀 UE started | Type: {UE_TYPE}")

while running:
    send_request()
    time.sleep(1)

log("✅ UE stopped")
