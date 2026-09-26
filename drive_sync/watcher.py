#!/usr/bin/env python3
"""
BuzzOff Drive Upload Watcher
Runs continuously (as a systemd service, not cron) checking every
POLL_SECONDS for new event folders and uploading them one at a time.
Being a single persistent loop, overlapping runs are structurally
impossible, and near-real-time upload happens without ever touching
or slowing down capture.py.
"""

import os
import subprocess
import time
from datetime import datetime

EVENTS_DIR = "/home/mavericks/buzzoff/events"
REMOTE_DEST = "gdrive:BuzzOff/incoming_events"
UPLOADED_MARKER = ".uploaded"
POLL_SECONDS = 10


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def upload_event(event_id, event_path):
    marker_path = os.path.join(event_path, UPLOADED_MARKER)

    log(f"Uploading {event_id}...")
    result = subprocess.run(
        ["rclone", "copy", event_path, f"{REMOTE_DEST}/{event_id}", "--quiet"],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        with open(marker_path, "w") as f:
            f.write("uploaded")
        log(f"  -> Success")
        return True
    else:
        log(f"  -> FAILED: {result.stderr.strip()}")
        return False


def check_and_upload_new_events():
    if not os.path.exists(EVENTS_DIR):
        return

    for event_id in os.listdir(EVENTS_DIR):
        event_path = os.path.join(EVENTS_DIR, event_id)
        if not os.path.isdir(event_path):
            continue

        marker_path = os.path.join(event_path, UPLOADED_MARKER)
        if os.path.exists(marker_path):
            continue

        upload_event(event_id, event_path)


def main():
    log("BuzzOff upload watcher starting.")
    log(f"Watching {EVENTS_DIR} every {POLL_SECONDS} seconds.")

    while True:
        check_and_upload_new_events()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
