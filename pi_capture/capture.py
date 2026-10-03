#!/usr/bin/env python3
"""
BuzzOff Pi5 Capture Script
On motion detection: takes a BURST of photos while recording a short audio
clip, then reads the BME280 environmental sensor, bundling everything into
one event folder under events/{event_id}/ with a shared event_id.

Burst: BURST_COUNT photos spread evenly across the RECORD_SECONDS audio
window (the first one is the frame that triggered motion). Early photos
catch the mosquito arriving; later ones catch it after it lands on the
sticky pad and sits still. All burst photos are kept (photo_1.jpg ...),
and the sharpest one (around where the motion happened) is also saved as
photo.jpg so everything downstream keeps working.

Safe hand-off to the uploader: each event is built inside
events/_incomplete/ and only moved into events/ once every file is
written, so watcher.py never uploads (or deletes) a half-written event.

Self-check at start: the USB microphone (opened by name, never "default"),
the camera and the BME280 must all pass a quick test, or capture stops with
a clear message. Results are written to selfcheck.json.
"""

import cv2
import numpy as np
from picamera2 import Picamera2
from datetime import datetime
import time
import os
import json
import uuid
import wave
import shutil
import argparse
import threading

import pyaudio
import smbus2
import bme280

# ========== ARGUMENTS ==========
parser = argparse.ArgumentParser()
parser.add_argument("--location", required=True, help="Name of the deployment location, must exist in locations.json")
args = parser.parse_args()
SESSION_LOCATION = args.location

# Load full location details (lat/long/city/state/zip/country), cached in
# locations.json the one time this location was first added via the
# control page. This is looked up once at startup, not per-event.
LOCATIONS_FILE = "/home/mavericks/buzzoff/locations.json"
with open(LOCATIONS_FILE, "r") as f:
    all_locations = json.load(f)

if SESSION_LOCATION not in all_locations:
    print(f"Error: location '{SESSION_LOCATION}' not found in {LOCATIONS_FILE}")
    print("Add it via the control page first, or add it manually to that file.")
    raise SystemExit(1)

location_details = all_locations[SESSION_LOCATION]
SESSION_LATITUDE = location_details["latitude"]
SESSION_LONGITUDE = location_details["longitude"]
SESSION_CITY = location_details["city"]
SESSION_STATE = location_details["state"]
SESSION_ZIP = location_details["zip_code"]
SESSION_COUNTRY = location_details["country"]

# ========== DEVICE IDENTITY ==========
DEVICE_CONFIG_FILE = "/home/mavericks/buzzoff/device_config.json"
with open(DEVICE_CONFIG_FILE, "r") as f:
    device_config = json.load(f)
DEVICE_ID = device_config["device_id"]
DEVICE_NAME = device_config["device_name"]

# ========== CONFIGURATION ==========

# Camera (Arducam OV5647). 1296x972 is the sensor's full-view 2x2 binned mode.
RESOLUTION = (1296, 972)
CAMERA_FPS = 30            # Camera frame rate. 30 fps caps each exposure at ~1/30 s,
                           # which cuts motion blur vs. the old 10 fps (up to 1/10 s).
DETECT_FPS = 10            # How often we check for motion (keeps CPU low)
JPEG_QUALITY = 92          # ~250-400 KB per photo at this size

# Motion detection
MOTION_THRESHOLD = 15      # Lower = more sensitive (15-50)
MIN_AREA = 300             # Minimum pixel area to trigger motion
CAPTURE_COOLDOWN = 2       # Seconds between triggers

# Burst photos (taken during the audio recording, so no extra time per event)
BURST_COUNT = 6            # 6 photos over 3 s = one every 0.5 s
SHARPNESS_BOX = 300        # Size (pixels) of the area around the motion used to pick the sharpest photo

# Storage
EVENTS_DIR = "/home/mavericks/buzzoff/events"
INCOMPLETE_DIR = os.path.join(EVENTS_DIR, "_incomplete")   # watcher.py ignores this folder
MIN_FREE_MB = 500          # Skip saving new events if the SD card has less free space than this

# Audio recording
SAMPLE_RATE = 48000
CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16   # final format/rate are chosen by the self-check below
CHUNK_SIZE = 4096
RECORD_SECONDS = 3

# BME280 environmental sensor
I2C_PORT = 1
I2C_ADDRESS = 0x77

# ========== SETUP ==========

os.makedirs(EVENTS_DIR, exist_ok=True)
# Throw away any half-built event left over from a previous Stop/power-off
shutil.rmtree(INCOMPLETE_DIR, ignore_errors=True)
os.makedirs(INCOMPLETE_DIR, exist_ok=True)

print("=" * 50)
print("BuzzOff Capture - initializing")
print("=" * 50)

# ---------- PRE-FLIGHT SELF-CHECK ----------
# Before watching for mosquitoes, prove that all 3 sensors really work:
#   1) USB microphone records live sound   2) camera gives real pictures   3) BME280 gives sensible readings
# If any check fails, capture stops with a clear message (shown in the control service log)
# instead of silently saving useless events. Results are also saved to selfcheck.json.
SELFCHECK_FILE = "/home/mavericks/buzzoff/selfcheck.json"
selfcheck = {"time": datetime.now().isoformat(timespec="seconds"), "location": SESSION_LOCATION}


def fail(part, message):
    selfcheck[part] = {"ok": False, "detail": message}
    selfcheck["ready"] = False
    with open(SELFCHECK_FILE, "w") as f:
        json.dump(selfcheck, f, indent=2)
    print("=" * 50)
    print(f"SELF-CHECK FAILED - {part.upper()}: {message}")
    print("Capture NOT started. Fix the problem above, then press Start again.")
    print("=" * 50)
    raise SystemExit(1)


def passed(part, message):
    selfcheck[part] = {"ok": True, "detail": message}
    print(f"  [OK] {part}: {message}")


print("Running self-check of all sensors...")

# 1) USB MICROPHONE --------------------------------------------------------
# BuzzOff uses ONLY the USB microphone. We open it directly by name (never "default"),
# so another sound card can never take its place.
audio = pyaudio.PyAudio()


def find_usb_mic():
    for i in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0 and "usb" in info["name"].lower():
            return i, info["name"]
    return None


def test_mic(index, fmt, rate, seconds=1.0):
    """Records briefly; returns (distinct sample values, loudness 0-1). A dead mic gives 1-2 values."""
    s = audio.open(format=fmt, channels=CHANNELS, rate=rate, input=True,
                   input_device_index=index, frames_per_buffer=CHUNK_SIZE)
    data = b"".join(s.read(CHUNK_SIZE, exception_on_overflow=False)
                    for _ in range(max(1, int(rate * seconds / CHUNK_SIZE))))
    s.stop_stream(); s.close()
    dtype, full = (np.int32, 2**31) if fmt == pyaudio.paInt32 else (np.int16, 2**15)
    x = np.frombuffer(data, dtype=dtype).astype(np.float64)
    return len(np.unique(x)), float(np.abs(x).max() / full)


usb = find_usb_mic()
if usb is None:
    fail("microphone", "USB microphone not found. Is it plugged in? (check with: arecord -l)")
MIC_INDEX, MIC_NAME = usb
mic_ok = None
for attempt in range(3):                       # the desktop sound system may hold the mic for a few seconds
    for fmt in (pyaudio.paInt16, pyaudio.paInt32):
        for rate in (48000, 44100):
            try:
                distinct, peak = test_mic(MIC_INDEX, fmt, rate)
            except Exception as e:
                if "silence" not in globals().get("last_error", ""):
                    last_error = str(e)
                continue
            if distinct > 50:
                mic_ok = (fmt, rate, distinct, peak)
                break
            last_error = f"microphone records silence ({distinct} distinct values) - try another USB port or mic"
        if mic_ok:
            break
    if mic_ok:
        break
    print(f"  ...microphone busy or silent, retrying in 6 s ({last_error})")
    time.sleep(6)
if not mic_ok:
    fail("microphone", f"USB microphone found but not recording: {last_error}")
AUDIO_FORMAT, SAMPLE_RATE, _distinct, _peak = mic_ok
passed("microphone", f"'{MIC_NAME}' live at {SAMPLE_RATE} Hz, "
       f"{'16' if AUDIO_FORMAT == pyaudio.paInt16 else '32'}-bit ({_distinct} sound levels, peak {_peak:.0%})")
if _peak > 0.98:
    print("  [!] microphone level is very high (clipping). Keep loud sounds away during the check, "
          "or lower the mic level in alsamixer.")

# 2) CAMERA ------------------------------------------------------------------
try:
    picam2 = Picamera2()
    camera_controls = {"FrameRate": CAMERA_FPS}
    try:
        from libcamera import controls as lc
        camera_controls["AeExposureMode"] = lc.AeExposureModeEnum.Short   # prefer short exposures (less blur)
    except Exception:
        pass
    config = picam2.create_video_configuration(
        main={"size": RESOLUTION, "format": "RGB888"},
        controls=camera_controls
    )
    picam2.configure(config)
    picam2.start()
    time.sleep(2)
    test_frames = [picam2.capture_array() for _ in range(3)]
except Exception as e:
    fail("camera", f"camera did not start ({e}). Check the ribbon cable is fully inserted at both ends.")
f0 = test_frames[-1]
if f0.shape[0] != RESOLUTION[1] or f0.shape[1] != RESOLUTION[0]:
    fail("camera", f"unexpected picture size {f0.shape}")
brightness, detail = float(f0.mean()), float(f0.std())
if detail < 2:
    fail("camera", f"pictures are a single flat colour (brightness {brightness:.0f}). "
                   f"Lens covered, cable loose, or camera faulty.")
passed("camera", f"{RESOLUTION[0]}x{RESOLUTION[1]} at {CAMERA_FPS} fps, brightness {brightness:.0f}/255")
if brightness < 15:
    print("  [!] pictures are very dark - add light (LED) or check the lens cover.")

# 3) BME280 ------------------------------------------------------------------
try:
    bus = smbus2.SMBus(I2C_PORT)
    bme280_calibration = bme280.load_calibration_params(bus, I2C_ADDRESS)
    reading = bme280.sample(bus, I2C_ADDRESS, bme280_calibration)
except Exception as e:
    fail("bme280", f"sensor not responding at address 0x{I2C_ADDRESS:02X} ({e}). Check the 4 wires (VCC, GND, SDA, SCL).")
t, h, p = reading.temperature, reading.humidity, reading.pressure
if not (-20 <= t <= 60 and 0 <= h <= 100 and 800 <= p <= 1100):
    fail("bme280", f"readings look wrong (temp {t:.1f} C, humidity {h:.1f} %, pressure {p:.1f} hPa)")
passed("bme280", f"{t:.1f} C, {h:.1f} % humidity, {p:.1f} hPa")

selfcheck["ready"] = True
with open(SELFCHECK_FILE, "w") as f:
    json.dump(selfcheck, f, indent=2)
print("SELF-CHECK PASSED - all 3 sensors working.")

# Motion detector
bg_subtractor = cv2.createBackgroundSubtractorMOG2(
    history=500,
    varThreshold=MOTION_THRESHOLD,
    detectShadows=False
)

print("=" * 50)
print("Ready. Watching for motion...")
print(f"Events will be saved to: {EVENTS_DIR}")
print("Press Ctrl+C to stop")
print("=" * 50)

last_capture_time = 0


# ========== HELPER FUNCTIONS ==========

def generate_event_id():
    """Unique ID shared across photos, audio, and env data for one event."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    return f"evt_{timestamp}_{suffix}"


def free_space_mb(path):
    return shutil.disk_usage(path).free / (1024 * 1024)


def sharpness(frame, box):
    """How sharp the photo is near where the motion happened
    (variance of the Laplacian: higher = crisper edges)."""
    x, y, w, h = box
    cx, cy = x + w // 2, y + h // 2
    half = SHARPNESS_BOX // 2
    H, W = frame.shape[:2]
    x0, x1 = max(0, cx - half), min(W, cx + half)
    y0, y1 = max(0, cy - half), min(H, cy + half)
    gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def open_mic_stream():
    """Opens the USB mic. If it was unplugged/replugged (any USB port), find it again by name."""
    global MIC_INDEX, audio
    try:
        return audio.open(format=AUDIO_FORMAT, channels=CHANNELS, rate=SAMPLE_RATE, input=True,
                          input_device_index=MIC_INDEX, frames_per_buffer=CHUNK_SIZE)
    except Exception:
        try:
            audio.terminate()
        except Exception:
            pass
        audio = pyaudio.PyAudio()                 # re-scan sound devices (new USB port = new number)
        found = find_usb_mic()
        if found is None:
            raise
        MIC_INDEX = found[0]
        return audio.open(format=AUDIO_FORMAT, channels=CHANNELS, rate=SAMPLE_RATE, input=True,
                          input_device_index=MIC_INDEX, frames_per_buffer=CHUNK_SIZE)


def record_audio(event_dir, result):
    """Records a short clip. Runs in a background thread so the camera
    can take the burst photos at the same time."""
    try:
        stream = open_mic_stream()
        frames = []
        for _ in range(int(SAMPLE_RATE / CHUNK_SIZE * RECORD_SECONDS)):
            frames.append(stream.read(CHUNK_SIZE, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()

        audio_path = os.path.join(event_dir, "audio.wav")
        with wave.open(audio_path, 'wb') as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(audio.get_sample_size(AUDIO_FORMAT))
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(b''.join(frames))
        result["audio_path"] = audio_path
        dtype = np.int32 if AUDIO_FORMAT == pyaudio.paInt32 else np.int16
        result["distinct_values"] = int(len(np.unique(np.frombuffer(b''.join(frames), dtype=dtype))))
    except Exception as e:
        result["error"] = str(e)


def capture_burst(first_frame, event_dir, motion_box):
    """Takes BURST_COUNT photos spread across RECORD_SECONDS. Saves them all,
    plus the sharpest as photo.jpg. Returns info for burst.json."""
    interval = RECORD_SECONDS / BURST_COUNT
    start = time.time()
    shots = []
    frame = first_frame
    for i in range(BURST_COUNT):
        if i > 0:
            wait = start + i * interval - time.time()
            if wait > 0:
                time.sleep(wait)
            frame = picam2.capture_array()
        t = round(time.time() - start, 2)
        name = f"photo_{i + 1}.jpg"
        cv2.imwrite(os.path.join(event_dir, name), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        shots.append({"file": name, "seconds_after_trigger": t,
                      "sharpness": round(sharpness(frame, motion_box), 1)})

    best = max(reversed(shots), key=lambda s: s["sharpness"])   # ties -> later photo (more likely landed)
    shutil.copyfile(os.path.join(event_dir, best["file"]), os.path.join(event_dir, "photo.jpg"))
    return {"burst_count": BURST_COUNT, "interval_seconds": round(interval, 2),
            "motion_box_xywh": list(motion_box), "best_photo": best["file"], "photos": shots}


def save_env_reading(event_dir, event_id, timestamp, burst_info):
    """Reads the BME280 and writes env.json with the fields the Colab
    notebook expects (temperature_c, humidity_pct, pressure_hpa, etc)."""
    data = bme280.sample(bus, I2C_ADDRESS, bme280_calibration)

    env_data = {
        "event_id": event_id,
        "device_id": DEVICE_ID,
        "device_name": DEVICE_NAME,
        "timestamp": timestamp.isoformat(),
        "location": SESSION_LOCATION,
        "city": SESSION_CITY,
        "state": SESSION_STATE,
        "zip_code": SESSION_ZIP,
        "country": SESSION_COUNTRY,
        "latitude": SESSION_LATITUDE,
        "longitude": SESSION_LONGITUDE,
        "temperature_c": round(data.temperature, 2),
        "humidity_pct": round(data.humidity, 2),
        "pressure_hpa": round(data.pressure, 2),
        "microphone": MIC_NAME,
        "burst_count": burst_info["burst_count"],
        "best_photo": burst_info["best_photo"],
    }

    env_path = os.path.join(event_dir, "env.json")
    with open(env_path, 'w') as f:
        json.dump(env_data, f, indent=2)
    with open(os.path.join(event_dir, "burst.json"), 'w') as f:
        json.dump(burst_info, f, indent=2)
    return env_path


def capture_event(first_frame, motion_box):
    """Runs the full bundle: burst photos + audio (at the same time) + env,
    all under one event_id. Built in _incomplete/, then moved into place."""
    if free_space_mb(EVENTS_DIR) < MIN_FREE_MB:
        print(f"    !! Less than {MIN_FREE_MB} MB free on the SD card - skipping this event. "
              f"Check that the uploader is running.")
        return None

    event_id = generate_event_id()
    work_dir = os.path.join(INCOMPLETE_DIR, event_id)
    os.makedirs(work_dir, exist_ok=True)
    timestamp = datetime.now()

    print(f"    -> Recording audio + taking {BURST_COUNT} photos...")
    audio_result = {}
    audio_thread = threading.Thread(target=record_audio, args=(work_dir, audio_result), daemon=True)
    audio_thread.start()
    burst_info = capture_burst(first_frame, work_dir, motion_box)
    audio_thread.join()

    if "error" in audio_result:
        print(f"    -> Audio FAILED: {audio_result['error']}")
    elif audio_result.get("distinct_values", 0) <= 50:
        print(f"    !! Audio saved but it is SILENT - check the microphone")
    else:
        print(f"    -> Audio saved")
    print(f"    -> Photos saved (sharpest: {burst_info['best_photo']})")

    save_env_reading(work_dir, event_id, timestamp, burst_info)
    print(f"    -> Environment saved")

    # Hand the finished event to the uploader in one step
    os.rename(work_dir, os.path.join(EVENTS_DIR, event_id))
    print(f"Event complete: {event_id}")
    return event_id


# ========== MAIN LOOP ==========

try:
    frame_count = 0

    while True:
        frame = picam2.capture_array()
        frame_count += 1

        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        fg_mask = bg_subtractor.apply(gray)

        contours, _ = cv2.findContours(
            fg_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        motion_detected = False
        largest_area = 0
        motion_box = (0, 0, RESOLUTION[0], RESOLUTION[1])
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > MIN_AREA:
                motion_detected = True
                if area > largest_area:
                    largest_area = area
                    motion_box = cv2.boundingRect(contour)

        current_time = time.time()
        if motion_detected and (current_time - last_capture_time) > CAPTURE_COOLDOWN:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] MOTION DETECTED! Area: {largest_area:.0f} pixels")
            capture_event(frame, motion_box)
            last_capture_time = time.time()  # reset AFTER capture, since recording takes a few seconds

        if frame_count % 100 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] System running... Frames processed: {frame_count}")

        time.sleep(1.0 / DETECT_FPS)

except KeyboardInterrupt:
    print("\n" + "=" * 50)
    print("Stopping capture...")
    print(f"Total frames processed: {frame_count}")
    print("=" * 50)

except Exception as e:
    print(f"\nError occurred: {e}")

finally:
    try:
        picam2.stop()
        picam2.close()
    except Exception:
        pass
    audio.terminate()
    bus.close()
    print("Camera, audio, and I2C bus closed. Goodbye!")
