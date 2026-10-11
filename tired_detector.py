"""
Drowsiness detection - proof of concept (Raspberry Pi + Arduino).

Webcam -> MediaPipe Face Landmarker -> Eye Aspect Ratio (EAR) + Mouth Aspect Ratio (MAR)
       -> PERCLOS, long eye closures, yawns -> ALERT / TIRED / DROWSY
       -> status sent to an Arduino over USB serial

Setup (Raspberry Pi OS 64-bit):
    pip install mediapipe opencv-python numpy pyserial

Run:
    python drowsiness_poc.py                          # window, auto-detect Arduino
    python drowsiness_poc.py --headless               # no window (e.g. no monitor / over SSH)
    python drowsiness_poc.py --port /dev/ttyACM0      # pick the Arduino port explicitly
    python drowsiness_poc.py --no-serial              # run without an Arduino (laptop testing)

The first run downloads the face landmarker model (~4 MB) next to this script.
For the first 3 seconds, look at the camera with your eyes open to calibrate.

Serial protocol (115200 baud, one line per message):
    Pi -> Arduino:  ALERT | TIRED | DROWSY | NOFACE | CALIBRATING
    Sent immediately when the status changes, and repeated every second as a heartbeat
    so the Arduino can tell if the Pi has stopped.

Keys (window mode):  q = quit   c = recalibrate
"""

import argparse
import glob
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

BAUD = 115200
HEARTBEAT_SECONDS = 1.0

COLOURS = {  # BGR
    "ALERT": (80, 200, 80),
    "TIRED": (0, 165, 255),
    "DROWSY": (0, 0, 255),
    "CALIBRATING": (200, 200, 200),
    "NO FACE": (150, 150, 150),
}


# --------------------------------------------------------------------------- Arduino link

class ArduinoLink:
    """Sends the current status to the Arduino. Never crashes the detector if the board
    is missing or gets unplugged - it just keeps trying to reconnect."""

    def __init__(self, port=None):
        self.port_arg = port
        self.ser = None
        self.last_sent = None
        self.last_sent_at = 0.0
        self.next_retry = 0.0

    @staticmethod
    def find_port():
        candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        return candidates[0] if candidates else None

    def _connect(self):
        import serial  # pyserial

        port = self.port_arg or self.find_port()
        if not port:
            return
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0)
            time.sleep(2)  # most Arduinos reset when the port opens; wait for the bootloader
            self.ser.reset_input_buffer()
            print(f"Arduino connected on {port}")
        except Exception as e:  # noqa: BLE001
            print(f"Couldn't open {port}: {e}")
            self.ser = None

    def send(self, status):
        now = time.monotonic()
        if self.ser is None:
            if now < self.next_retry:
                return
            self.next_retry = now + 3.0
            self._connect()
            if self.ser is None:
                return
            self.last_sent = None  # force a send after reconnecting

        msg = status.replace(" ", "")  # "NO FACE" -> "NOFACE"
        if msg == self.last_sent and now - self.last_sent_at < HEARTBEAT_SECONDS:
            return
        try:
            self.ser.write(f"{msg}\n".encode())
            self.last_sent, self.last_sent_at = msg, now
            # Print anything the Arduino says back (handy for debugging the sketch)
            reply = self.ser.read(self.ser.in_waiting or 0)
            if reply:
                print("Arduino:", reply.decode(errors="replace").strip())
        except Exception as e:  # noqa: BLE001
            print(f"Lost Arduino connection: {e}")
            self.close()

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
        self.ser = None


# --------------------------------------------------------------------------- detection

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


def parse_args():
    ap = argparse.ArgumentParser(description="Realtime drowsiness detector")
    ap.add_argument("--camera", type=int, default=0, help="camera index (default 0)")
    ap.add_argument("--width", type=int, default=640, help="capture width (lower = faster)")
    ap.add_argument("--height", type=int, default=480, help="capture height")
    ap.add_argument("--port", help="Arduino serial port (default: auto-detect)")
    ap.add_argument("--no-serial", action="store_true", help="don't talk to an Arduino")
    ap.add_argument("--headless", action="store_true", help="no preview window")
    return ap.parse_args()


def main():
    args = parse_args()
    ensure_model()
    options = vision.FaceLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
    )

    cap = cv2.VideoCapture(args.camera)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always process the newest frame, not a stale one
    if not cap.isOpened():
        raise SystemExit("Couldn't open the camera (try --camera 1).")

    arduino = None if args.no_serial else ArduinoLink(args.port)

    # State
    baseline = None
    calib_samples, calib_start = [], None
    eye_samples = deque()   # (time, eyes_closed)
    yawn_times = deque()
    closed_since = None
    yawn_since = None
    last_ts = 0
    prev_status = None
    fps, fps_t, fps_n = 0.0, time.monotonic(), 0

    try:
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
                pts = None
                if result.face_landmarks:
                    pts = np.array([(lm.x * w, lm.y * h) for lm in result.face_landmarks[0]])
                    ear = (eye_aspect_ratio(pts, LEFT_EYE) + eye_aspect_ratio(pts, RIGHT_EYE)) / 2
                    mar = mouth_aspect_ratio(pts)

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

                # Output
                if arduino:
                    arduino.send(status)
                if status != prev_status:
                    print(f"{time.strftime('%H:%M:%S')}  {status}"
                          f"  (PERCLOS {perclos * 100:.0f}%, yawns {len(yawn_times)})")
                    prev_status = status

                fps_n += 1
                if now - fps_t >= 1.0:
                    fps, fps_t, fps_n = fps_n / (now - fps_t), now, 0

                if args.headless:
                    continue

                # HUD
                if pts is not None:
                    for i in LEFT_EYE + RIGHT_EYE + MOUTH:
                        cv2.circle(frame, tuple(pts[i].astype(int)), 2, (255, 255, 0), -1)
                colour = COLOURS[status]
                darken(frame, 0, 0, min(w, 420), 215)
                darken(frame, 0, h - 35, min(w, 360), h)
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
                link = "off" if not arduino else ("ok" if arduino.ser else "--")
                put(frame, f"q: quit  c: recalibrate  {fps:.0f} FPS  Arduino {link}",
                    h - 15, (180, 180, 180), scale=0.5)
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
    except KeyboardInterrupt:
        pass
    finally:
        if arduino:
            arduino.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()