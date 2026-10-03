#!/usr/bin/env python3
"""
BuzzOff Drive Upload Watcher
Runs continuously (as a systemd service, not cron) checking every
POLL_SECONDS for finished event folders, uploading them one at a time,
and deleting each local copy ONLY after Google Drive has confirmed it.

How it stays safe:
- capture.py builds each event in events/_incomplete/ and moves it into
  events/ only when every file is written, so this script never sees a
  half-written event (it skips any folder starting with "_" or ".").
- `rclone move` uploads each file, checks it arrived intact (size + MD5
  checksum on Google Drive), and only then removes that file locally.
  If the network drops mid-upload, the remaining files stay on the Pi and
  are retried on the next loop. Nothing is deleted that is not in Drive.
- Events uploaded by the old version (with a .uploaded marker) are
  handled the same way: rclone skips files already in Drive, uploads any
  that are missing, then the local folder is removed.
"""

import os
import shutil
import subprocess
import time
from datetime import datetime

EVENTS_DIR = "/home/mavericks/buzzoff/events"
REMOTE_DEST = "gdrive:BuzzOff/incoming_events"
OLD_MARKER = ".uploaded"          # left behind by the previous watcher version
POLL_SECONDS = 10


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def upload_and_remove(event_id, event_path):
    old_marker = os.path.join(event_path, OLD_MARKER)
    if os.path.exists(old_marker):
        os.remove(old_marker)        # never upload the marker file itself

    log(f"Uploading {event_id}...")
    result = subprocess.run(
        ["rclone", "move", event_path, f"{REMOTE_DEST}/{event_id}", "--quiet"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"  -> FAILED (will retry): {result.stderr.strip()[-300:]}")
        return False

    leftover = os.listdir(event_path) if os.path.isdir(event_path) else []
    if leftover:
        log(f"  -> Uploaded, but {len(leftover)} file(s) still local - will retry: {leftover}")
        return False

    shutil.rmtree(event_path, ignore_errors=True)
    log(f"  -> Success, local copy deleted. SD card free: "
        f"{shutil.disk_usage(EVENTS_DIR).free / 1e9:.1f} GB")
    return True


def check_and_upload_new_events():
    if not os.path.exists(EVENTS_DIR):
        return
    for event_id in sorted(os.listdir(EVENTS_DIR)):          # oldest first
        if event_id.startswith(("_", ".")):
            continue                                          # _incomplete/ and hidden folders
        event_path = os.path.join(EVENTS_DIR, event_id)
        if os.path.isdir(event_path):
            upload_and_remove(event_id, event_path)


def main():
    log("BuzzOff upload watcher starting (upload, verify, then delete local copy).")
    log(f"Watching {EVENTS_DIR} every {POLL_SECONDS} seconds.")
    while True:
        try:
            check_and_upload_new_events()
        except Exception as e:                                # never let the watcher die
            log(f"Watcher error (will retry): {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
