"""
Drowsiness detection - proof of concept.

Webcam -> MediaPipe Face Landmarker -> Eye Aspect Ratio (EAR) + Mouth Aspect Ratio (MAR)
       -> PERCLOS, long eye closures, yawns -> ALERT / TIRED / DROWSY

Setup:
    pip install mediapipe opencv-python numpy
    python drowsiness_poc.py

The first run downloads the face landmarker model (~4 MB) next to this script.
For the first 3 seconds, look at the camera with your eyes open to calibrate.

Keys:  q = quit   c = recalibrate
"""

import os
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

# The pip OpenCV wheel's Qt window looks for fonts inside the package and warns when
# it can't find them. Point it at the system fonts instead (must happen before cv2 loads).
if sys.platform.startswith("linux") and Path("/usr/share/fonts").is_dir():
    os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
MODEL_PATH = Path(__file__).with_name("face_landmarker.task")

# Face mesh landmark indices, ordered p1..p6 (corner, top, top, corner, bottom, bottom)
RIGHT_EYE = [33, 160, 158, 133, 153, 144]
LEFT_EYE = [362, 385, 387, 263, 373, 380]
MOUTH = [61, 291, 13, 14]  # left corner, right corner, upper inner lip, lower inner lip

# Tunables
CALIBRATION_SECONDS = 3.0
CLOSED_RATIO = 0.70      # eye "closed" when EAR < 70% of your open-eye baseline
PERCLOS_WINDOW = 30.0    # seconds
LONG_CLOSURE = 1.5       # seconds of continuous closure -> DROWSY immediately
YAWN_MAR = 0.6           # mouth opening / mouth width
YAWN_MIN_DURATION = 1.5  # seconds the mouth must stay open to count as a yawn
YAWN_WINDOW = 60.0       # seconds

PERCLOS_TIRED = 0.15
PERCLOS_DROWSY = 0.25
YAWNS_TIRED = 2

COLOURS = {  # BGR
    "ALERT": (80, 200, 80),
    "TIRED": (0, 165, 255),
    "DROWSY": (0, 0, 255),
    "CALIBRATING": (200, 200, 200),
    "NO FACE": (150, 150, 150),
}


def ensure_model():
    if not MODEL_PATH.exists():
        print("Downloading face landmarker model...")
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)


def eye_aspect_ratio(pts, idx):
    p = pts[idx]
    vertical = np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])
    horizontal = np.linalg.norm(p[0] - p[3])
    return vertical / (2.0 * horizontal) if horizontal else 0.0


def mouth_aspect_ratio(pts):
    left, right, top, bottom = pts[MOUTH]
    width = np.linalg.norm(left - right)
    return np.linalg.norm(top - bottom) / width if width else 0.0


def put(frame, text, y, colour=(255, 255, 255), scale=0.6, thickness=1):
    cv2.putText(frame, text, (15, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thickness, cv2.LINE_AA)


def darken(frame, x1, y1, x2, y2, alpha=0.55):
    """Dim a rectangle so text drawn on it stays readable, without outlining each line."""
    roi = frame[y1:y2, x1:x2]
    frame[y1:y2, x1:x2] = (roi * (1 - alpha)).astype(frame.dtype)


def main():
    ensure_model()
    options = vision.FaceLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
    )

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise SystemExit("Couldn't open the webcam (try a different index in cv2.VideoCapture).")

    # State
    baseline = None
    calib_samples, calib_start = [], None
    eye_samples = deque()   # (time, eyes_closed)
    yawn_times = deque()
    closed_since = None
    yawn_since = None
    last_ts = 0

    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            now = time.monotonic()

            ts = max(last_ts + 1, int(now * 1000))  # must strictly increase
            last_ts = ts
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts
            )

            ear = mar = None
            if result.face_landmarks:
                pts = np.array([(lm.x * w, lm.y * h) for lm in result.face_landmarks[0]])
                ear = (eye_aspect_ratio(pts, LEFT_EYE) + eye_aspect_ratio(pts, RIGHT_EYE)) / 2
                mar = mouth_aspect_ratio(pts)

                for i in LEFT_EYE + RIGHT_EYE + MOUTH:
                    cv2.circle(frame, tuple(pts[i].astype(int)), 2, (255, 255, 0), -1)

                if baseline is None:
                    calib_start = calib_start or now
                    calib_samples.append(ear)
                    if now - calib_start >= CALIBRATION_SECONDS:
                        baseline = float(np.median(calib_samples))
                else:
                    closed = ear < baseline * CLOSED_RATIO
                    eye_samples.append((now, closed))
                    closed_since = (closed_since or now) if closed else None

                    if mar > YAWN_MAR:
                        yawn_since = yawn_since or now
                    elif yawn_since is not None:
                        if now - yawn_since >= YAWN_MIN_DURATION:
                            yawn_times.append(now)
                        yawn_since = None
            else:
                closed_since = None
                yawn_since = None

            # Drop old samples
            while eye_samples and now - eye_samples[0][0] > PERCLOS_WINDOW:
                eye_samples.popleft()
            while yawn_times and now - yawn_times[0] > YAWN_WINDOW:
                yawn_times.popleft()

            perclos = sum(c for _, c in eye_samples) / len(eye_samples) if eye_samples else 0.0
            closure = now - closed_since if closed_since else 0.0

            # Decide status
            if ear is None:
                status = "NO FACE"
            elif baseline is None:
                status = "CALIBRATING"
            elif closure >= LONG_CLOSURE or perclos >= PERCLOS_DROWSY:
                status = "DROWSY"
            elif perclos >= PERCLOS_TIRED or len(yawn_times) >= YAWNS_TIRED:
                status = "TIRED"
            else:
                status = "ALERT"

            # HUD
            colour = COLOURS[status]
            darken(frame, 0, 0, min(w, 420), 215)
            darken(frame, 0, h - 35, min(w, 260), h)
            put(frame, status, 40, colour, scale=1.1, thickness=2)
            if status == "CALIBRATING":
                put(frame, "Look at the camera with your eyes open", 70)
            if ear is not None:
                base_txt = f" (open baseline {baseline:.3f})" if baseline else ""
                put(frame, f"EAR {ear:.3f}{base_txt}", 100)
                put(frame, f"MAR {mar:.2f}", 125)
            put(frame, f"PERCLOS ({int(PERCLOS_WINDOW)}s) {perclos * 100:.0f}%", 150)
            put(frame, f"Eyes closed for {closure:.1f}s", 175)
            put(frame, f"Yawns (last {int(YAWN_WINDOW)}s) {len(yawn_times)}", 200)
            put(frame, "q: quit   c: recalibrate", h - 15, (180, 180, 180), scale=0.5)
            if status == "DROWSY":
                cv2.rectangle(frame, (0, 0), (w - 1, h - 1), colour, 12)

            cv2.imshow("Drowsiness PoC", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("c"):
                baseline, calib_samples, calib_start = None, [], None
                eye_samples.clear()
                yawn_times.clear()
                closed_since = yawn_since = None

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()