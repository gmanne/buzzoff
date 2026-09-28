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
import json

app = Flask(__name__)

# ==================== CONFIGURATION ====================
CAPTURE_SCRIPT = "/home/mavericks/buzzoff/pi_capture/capture.py"
PID_FILE = "/home/mavericks/buzzoff/pi_capture/capture.pid"
START_TIME_FILE = "/home/mavericks/buzzoff/pi_capture/capture_started.txt"
SECRET_FILE = "/home/mavericks/buzzoff/secret.txt"
LOCATIONS_FILE = "/home/mavericks/buzzoff/locations.json"
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


def load_locations():
    if not os.path.exists(LOCATIONS_FILE):
        return {}
    with open(LOCATIONS_FILE, "r") as f:
        return json.load(f)


def reverse_geocode(latitude, longitude):
    """Looks up city/state/zip/country for a coordinate, one time only,
    using the free OpenStreetMap Nominatim service. Nominatim requires a
    real User-Agent header identifying the app, or it will block requests."""
    import requests
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": latitude, "lon": longitude, "format": "json"}
    headers = {"User-Agent": "BuzzOff-eCYBERMISSION-Project"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        address = response.json().get("address", {})
        return {
            "city": address.get("city") or address.get("town") or address.get("village") or "Unknown",
            "state": address.get("state", "Unknown"),
            "zip_code": address.get("postcode", "Unknown"),
            "country": address.get("country", "Unknown"),
        }
    except Exception as e:
        return {"city": "Unknown", "state": "Unknown", "zip_code": "Unknown", "country": "Unknown"}


def save_location(name, latitude, longitude):
    address = reverse_geocode(latitude, longitude)
    locations = load_locations()
    locations[name] = {
        "latitude": latitude,
        "longitude": longitude,
        "city": address["city"],
        "state": address["state"],
        "zip_code": address["zip_code"],
        "country": address["country"],
    }
    with open(LOCATIONS_FILE, "w") as f:
        json.dump(locations, f, indent=2)
    return locations[name]


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
      <select id="locationSelect" style="font-size:16px; padding:10px; width:270px; border-radius:6px;" onchange="handleLocationChange()">
        <option value="">Loading locations...</option>
      </select>
      <div id="newLocationFields" style="display:none; margin-top:10px;">
        <input type="text" id="newName" placeholder="New location name" style="font-size:15px; padding:8px; width:200px;"><br><br>
        <button onclick="useMyLocation()" style="font-size:15px; padding:10px 16px; background:#1565c0; color:white; border:none; border-radius:6px;">📍 Use My Current Location</button>
        <p style="font-size:13px; color:#666; margin:8px 0;">or enter coordinates manually:</p>
        <input type="text" id="newLat" placeholder="Latitude" style="font-size:15px; padding:8px; width:200px;"><br><br>
        <input type="text" id="newLon" placeholder="Longitude" style="font-size:15px; padding:8px; width:200px;"><br><br>
        <button onclick="saveNewLocation()" style="font-size:15px; padding:8px 16px;">Save Location</button>
      </div>
      <br><br>
      <button id="startBtn" class="start" onclick="callStart()">Start</button>
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

        function loadLocationOptions() {{
          fetch(`/locations?key=${{KEY}}`)
            .then(r => r.json())
            .then(data => {{
              const select = document.getElementById('locationSelect');
              select.innerHTML = "";
              Object.keys(data).forEach(name => {{
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                select.appendChild(opt);
              }});
              const addOpt = document.createElement('option');
              addOpt.value = "__add_new__";
              addOpt.textContent = "+ Add new location";
              select.appendChild(addOpt);
            }});
        }}

        function handleLocationChange() {{
          const value = document.getElementById('locationSelect').value;
          document.getElementById('newLocationFields').style.display = (value === "__add_new__") ? "block" : "none";
        }}

        function useMyLocation() {{
          if (!navigator.geolocation) {{
            alert("This browser doesn't support location. Try a different phone/browser.");
            return;
          }}
          document.getElementById('status').innerText = "Getting your location...";
          navigator.geolocation.getCurrentPosition(
            function(position) {{
              document.getElementById('newLat').value = position.coords.latitude.toFixed(6);
              document.getElementById('newLon').value = position.coords.longitude.toFixed(6);
              document.getElementById('status').innerText = "Location captured. Now click Save.";
            }},
            function(error) {{
              document.getElementById('status').innerText = "Couldn't get location: " + error.message;
              alert("Location access failed or was denied. Make sure you tap 'Allow' when your browser asks for location permission.");
            }},
            {{ enableHighAccuracy: true, timeout: 10000 }}
          );
        }}

        function saveNewLocation() {{
          const name = document.getElementById('newName').value.trim();
          const lat = document.getElementById('newLat').value.trim();
          const lon = document.getElementById('newLon').value.trim();
          if (!name || !lat || !lon) {{
            alert("Please fill in name, latitude, and longitude.");
            return;
          }}
          document.getElementById('status').innerText = "Looking up city/state/zip...";
          fetch(`/add_location?key=${{KEY}}&name=${{encodeURIComponent(name)}}&latitude=${{lat}}&longitude=${{lon}}`)
            .then(r => r.text())
            .then(text => {{
              document.getElementById('status').innerText = "Location saved: " + text;
              document.getElementById('newLocationFields').style.display = "none";
              loadLocationOptions();
            }});
        }}

        function callStart() {{
          const location = document.getElementById('locationSelect').value;
          if (!location || location === "__add_new__") {{
            alert("Please select or add a location first.");
            return;
          }}
          document.getElementById('status').innerText = "Working...";
          fetch(`/start?key=${{KEY}}&location=${{encodeURIComponent(location)}}`)
            .then(r => r.text())
            .then(text => {{
              document.getElementById('status').innerText = text;
              setTimeout(refreshStatus, 2000);
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
        loadLocationOptions();
      </script>
    </body>
    </html>
    """


@app.route("/locations")
def locations():
    if not check_key():
        return "Unauthorized", 401
    return json.dumps(load_locations())


@app.route("/add_location")
def add_location():
    if not check_key():
        return "Unauthorized", 401

    name = request.args.get("name", "").strip()
    try:
        latitude = float(request.args.get("latitude"))
        longitude = float(request.args.get("longitude"))
    except (TypeError, ValueError):
        return "Invalid latitude/longitude", 400

    if not name:
        return "Location name required", 400

    record = save_location(name, latitude, longitude)
    return json.dumps(record)


@app.route("/start")
def start():
    if not check_key():
        return "Unauthorized", 401

    if is_running():
        return f"Already running: {os.path.basename(CAPTURE_SCRIPT)}"

    location = request.args.get("location", "")
    if location not in load_locations():
        return f"Unknown location '{location}'. Add it first via /add_location.", 400

    process = subprocess.Popen(["python3", CAPTURE_SCRIPT, "--location", location])
    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))
    with open(START_TIME_FILE, "w") as f:
        f.write(time.strftime("%b %d, %I:%M %p"))

    return f"Started: {os.path.basename(CAPTURE_SCRIPT)} at {location} (PID {process.pid})"


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
