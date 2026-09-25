#!/usr/bin/env python3
"""
BuzzOff Drive Sync
Watches the local events/ folder and uploads any event bundle not yet
uploaded to Google Drive, using the 'gdrive' rclone remote. Meant to be
run on a schedule (cron) every few minutes.
"""

import os
import subprocess
from datetime import datetime

EVENTS_DIR = "/home/mavericks/buzzoff/events"
REMOTE_DEST = "gdrive:BuzzOff/incoming_events"
UPLOADED_MARKER = ".uploaded"


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def sync_events():
    if not os.path.exists(EVENTS_DIR):
        log(f"Events directory not found: {EVENTS_DIR}")
        return

    event_ids = [d for d in os.listdir(EVENTS_DIR)
                 if os.path.isdir(os.path.join(EVENTS_DIR, d))]

    if not event_ids:
        log("No event folders found.")
        return

    uploaded_count = 0
    skipped_count = 0
    failed_count = 0

    for event_id in event_ids:
        event_path = os.path.join(EVENTS_DIR, event_id)
        marker_path = os.path.join(event_path, UPLOADED_MARKER)

        if os.path.exists(marker_path):
            skipped_count += 1
            continue

        log(f"Uploading {event_id}...")
        result = subprocess.run(
            ["rclone", "copy", event_path, f"{REMOTE_DEST}/{event_id}", "--quiet"],
            capture_output=True, text=True
        )

        if result.returncode == 0:
            # Only write the marker AFTER a successful upload, and only
            # locally — this file is never itself uploaded, since it
            # didn't exist yet during the rclone copy above.
            with open(marker_path, "w") as f:
                f.write("uploaded")
            log(f"  -> Success")
            uploaded_count += 1
        else:
            log(f"  -> FAILED: {result.stderr.strip()}")
            failed_count += 1

    log(f"Done. Uploaded: {uploaded_count}, Already synced: {skipped_count}, Failed: {failed_count}")


if __name__ == "__main__":
    sync_events()
