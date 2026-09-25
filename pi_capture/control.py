#!/usr/bin/env python3
"""
BuzzOff Pi5 Remote Control
Exposes /start, /stop, /status so capture.py can be launched or stopped
from a browser (via Tailscale Funnel) or from a Colab notebook, instead
of needing to SSH in and use Ctrl+C.
"""

from flask import Flask, request
import subprocess
import os
import signal
import time

app = Flask(__name__)

# ==================== CONFIGURATION ====================
CAPTURE_SCRIPT = "/home/mavericks/buzzoff/pi_capture/capture.py"
PID_FILE = "/home/mavericks/buzzoff/pi_capture/capture.pid"
SECRET_FILE = "/home/mavericks/buzzoff/secret.txt"
PORT = 5000

# ==================== SETUP ====================
with open(SECRET_FILE, "r") as f:
    SECRET = f.read().strip()


def check_key():
    key = request.args.get("key", "")
    return key == SECRET


def is_running():
    if not os.path.exists(PID_FILE):
        return False
    with open(PID_FILE, "r") as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)  # signal 0 = just check if it exists, don't actually kill
        return True
    except ProcessLookupError:
        os.remove(PID_FILE)  # stale pid file, clean it up
        return False


# ==================== ROUTES ====================

@app.route("/")
def home():
    return """
    <html><body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
      <h2>BuzzOff Pi5 Control</h2>
      <p>Use ?key=yoursecret with /start, /stop, or /status</p>
    </body></html>
    """


@app.route("/start")
def start():
    if not check_key():
        return "Unauthorized", 401

    if is_running():
        return "Already running."

    process = subprocess.Popen(["python3", CAPTURE_SCRIPT])
    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))

    return f"Started capture.py (PID {process.pid})"


@app.route("/stop")
def stop():
    if not check_key():
        return "Unauthorized", 401

    if not is_running():
        return "Not currently running."

    with open(PID_FILE, "r") as f:
        pid = int(f.read().strip())

    # Send SIGINT first, same as Ctrl+C, so the script's own cleanup
    # (closing the camera, audio, and I2C bus) runs normally.
    os.kill(pid, signal.SIGINT)

    # Give it a few seconds to shut down cleanly before forcing it.
    for _ in range(10):
        time.sleep(1)
        if not is_running():
            return "Stopped cleanly."

    # If it's still alive after 10 seconds (e.g. stuck in picam2.stop()),
    # force-kill it so you're never left with a permanently stuck script.
    os.kill(pid, signal.SIGKILL)
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    return "Did not stop cleanly within 10 seconds - force-killed it instead."


@app.route("/status")
def status():
    if not check_key():
        return "Unauthorized", 401

    if is_running():
        with open(PID_FILE, "r") as f:
            pid = f.read().strip()
        return f"Running (PID {pid})"
    return "Stopped"


# ==================== RUN ====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
