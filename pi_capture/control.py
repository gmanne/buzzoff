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
START_TIME_FILE = "/home/mavericks/buzzoff/pi_capture/capture_started.txt"
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
    key = request.args.get("key", "")
    if key != SECRET:
        return """
        <html><body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
          <h2>BuzzOff Pi5 Control</h2>
          <p>Add your key to the URL, like:<br>yourlink.ts.net/?key=yoursecret</p>
        </body></html>
        """

    return f"""
    <html>
    <head>
      <title>BuzzOff Control</title>
      <style>
        body {{ font-family: sans-serif; text-align: center; padding-top: 40px; background: #f5f5f5; }}
        button {{ font-size: 20px; padding: 16px 32px; margin: 10px; border-radius: 10px; border: none; color: white; }}
        .start {{ background: #2e7d32; }}
        .stop {{ background: #c62828; }}
        button:disabled {{ background: #bbbbbb; cursor: not-allowed; }}
        #status {{ font-size: 18px; margin-top: 20px; font-weight: bold; }}
      </style>
    </head>
    <body>
      <h2>BuzzOff Pi5 Control</h2>
      <div id="status">Checking status...</div>
      <br>
      <button id="startBtn" class="start" onclick="callAction('start')">Start</button>
      <button id="stopBtn" class="stop" onclick="callAction('stop')">Stop</button>

      <script>
        const KEY = "{SECRET}";

        function updateButtons(statusText) {{
          const running = statusText.startsWith("Running");
          document.getElementById('startBtn').disabled = running;
          document.getElementById('stopBtn').disabled = !running;
        }}

        function refreshStatus() {{
          fetch(`/status?key=${{KEY}}`)
            .then(r => r.text())
            .then(text => {{
              document.getElementById('status').innerText = text;
              updateButtons(text);
            }});
        }}

        function callAction(action) {{
          document.getElementById('status').innerText = "Working...";
          fetch(`/${{action}}?key=${{KEY}}`)
            .then(r => r.text())
            .then(text => {{
              document.getElementById('status').innerText = text;
              setTimeout(refreshStatus, 2000);
            }});
        }}

        refreshStatus();
      </script>
    </body>
    </html>
    """


@app.route("/start")
def start():
    if not check_key():
        return "Unauthorized", 401

    if is_running():
        return f"Already running: {os.path.basename(CAPTURE_SCRIPT)}"

    process = subprocess.Popen(["python3", CAPTURE_SCRIPT])
    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))
    with open(START_TIME_FILE, "w") as f:
        f.write(time.strftime("%b %d, %I:%M %p"))

    return f"Started: {os.path.basename(CAPTURE_SCRIPT)} (PID {process.pid})"


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

    script_name = os.path.basename(CAPTURE_SCRIPT)

    if is_running():
        started = ""
        if os.path.exists(START_TIME_FILE):
            with open(START_TIME_FILE, "r") as f:
                started = f" (since {f.read().strip()})"
        return f"Running: {script_name}{started}"
    return f"Stopped: {script_name}"


# ==================== RUN ====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
