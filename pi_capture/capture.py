#!/usr/bin/env python3
"""
BuzzOff Pi5 Capture Script
On motion detection: captures a photo, records a short audio clip, and reads
the BME280 environmental sensor, bundling all three into one event folder
under events/{event_id}/ with a shared event_id.
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

import pyaudio
import smbus2
import bme280

# ========== CONFIGURATION ==========

# Motion detection (camera)
MOTION_THRESHOLD = 15      # Lower = more sensitive (15-50)
MIN_AREA = 300             # Minimum pixel area to trigger motion
RESOLUTION = (1296, 972)   # Camera resolution
FPS = 10                   # Frames per second for detection
CAPTURE_COOLDOWN = 2       # Seconds between triggers

# Where event bundles get saved (this is what drive_sync/sync.py should watch)
EVENTS_DIR = "/home/mavericks/buzzoff/events"

# Audio recording
SAMPLE_RATE = 48000
CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt32
CHUNK_SIZE = 4096
RECORD_SECONDS = 3

# BME280 environmental sensor
I2C_PORT = 1
I2C_ADDRESS = 0x77

# Fixed deployment location — update this for each Pi5/location you deploy
LOCATION_NAME = "Arjun's backyard"
FIXED_LATITUDE = 35.8233
FIXED_LONGITUDE = -78.825294

# ========== SETUP ==========

os.makedirs(EVENTS_DIR, exist_ok=True)

print("=" * 50)
print("BuzzOff Capture - initializing")
print("=" * 50)

# Camera
print("Initializing camera...")
picam2 = Picamera2()
config = picam2.create_video_configuration(
    main={"size": RESOLUTION, "format": "RGB888"},
    controls={"FrameRate": FPS}
)
picam2.configure(config)
picam2.start()
time.sleep(2)
print("Camera ready.")

# Audio
print("Initializing audio...")
audio = pyaudio.PyAudio()
print("Audio ready.")

# BME280
print("Initializing BME280...")
bus = smbus2.SMBus(I2C_PORT)
try:
    bme280_calibration = bme280.load_calibration_params(bus, I2C_ADDRESS)
    print(f"BME280 found at address 0x{I2C_ADDRESS:02X}")
except Exception as e:
    print(f"Could not initialize BME280: {e}")
    print("Check wiring and I2C address. Exiting.")
    raise SystemExit(1)

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
    """Unique ID shared across photo, audio, and env data for one event."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    return f"evt_{timestamp}_{suffix}"


def save_photo(frame, event_dir):
    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    photo_path = os.path.join(event_dir, "photo.jpg")
    cv2.imwrite(photo_path, frame_bgr)
    return photo_path


def record_audio(event_dir):
    """Records a short clip. Blocks the main loop for RECORD_SECONDS —
    acceptable for this proof of concept, but means motion detection
    pauses briefly during each recording."""
    stream = audio.open(
        format=AUDIO_FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE
    )

    frames = []
    for _ in range(int(SAMPLE_RATE / CHUNK_SIZE * RECORD_SECONDS)):
        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        frames.append(data)

    stream.stop_stream()
    stream.close()

    audio_path = os.path.join(event_dir, "audio.wav")
    wav_file = wave.open(audio_path, 'wb')
    wav_file.setnchannels(CHANNELS)
    wav_file.setsampwidth(audio.get_sample_size(AUDIO_FORMAT))
    wav_file.setframerate(SAMPLE_RATE)
    wav_file.writeframes(b''.join(frames))
    wav_file.close()
    return audio_path


def save_env_reading(event_dir, event_id, timestamp):
    """Reads the BME280 and writes env.json with the fields the Colab
    notebook expects (temperature_c, humidity_pct, pressure_hpa, etc)."""
    data = bme280.sample(bus, I2C_ADDRESS, bme280_calibration)

    env_data = {
        "event_id": event_id,
        "timestamp": timestamp.isoformat(),
        "latitude": FIXED_LATITUDE,
        "longitude": FIXED_LONGITUDE,
        "location": LOCATION_NAME,
        "temperature_c": round(data.temperature, 2),
        "humidity_pct": round(data.humidity, 2),
        "pressure_hpa": round(data.pressure, 2),
    }

    env_path = os.path.join(event_dir, "env.json")
    with open(env_path, 'w') as f:
        json.dump(env_data, f, indent=2)
    return env_path


def capture_event(frame):
    """Runs the full bundle: photo + audio + env, all under one event_id."""
    event_id = generate_event_id()
    event_dir = os.path.join(EVENTS_DIR, event_id)
    os.makedirs(event_dir, exist_ok=True)
    timestamp = datetime.now()

    photo_path = save_photo(frame, event_dir)
    print(f"    -> Photo saved: {photo_path}")

    print("    -> Recording audio...")
    audio_path = record_audio(event_dir)
    print(f"    -> Audio saved: {audio_path}")

    env_path = save_env_reading(event_dir, event_id, timestamp)
    print(f"    -> Environment saved: {env_path}")

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
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > MIN_AREA:
                motion_detected = True
                if area > largest_area:
                    largest_area = area

        current_time = time.time()
        if motion_detected and (current_time - last_capture_time) > CAPTURE_COOLDOWN:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] MOTION DETECTED! Area: {largest_area:.0f} pixels")
            capture_event(frame)
            last_capture_time = time.time()  # reset AFTER capture, since recording takes a few seconds

        if frame_count % 100 == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] System running... Frames processed: {frame_count}")

        time.sleep(1.0 / FPS)

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
