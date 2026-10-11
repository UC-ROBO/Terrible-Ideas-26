"""
Serial link from the Raspberry Pi to the Arduino, shared by drowsiness_poc.py and teleop.py.

Protocol (115200 baud, one message per line):
    ALERT | TIRED | DROWSY | NOFACE | CALIBRATING   drowsiness status
    M <left> <right>                                 wheel speeds, -255..255 (negative = reverse)

Status is sent on change and repeated every second as a heartbeat. Drive commands are
repeated every 100 ms; the Arduino stops the motors if they stop arriving, so the robot
halts if this program crashes or the cable is pulled.

Never raises if the Arduino is missing or unplugged - it just keeps trying to reconnect.
"""

import glob
import time

BAUD = 115200
STATUS_HEARTBEAT = 1.0   # seconds
DRIVE_RESEND = 0.1       # seconds - keep well under the Arduino's DRIVE_TIMEOUT_MS
RECONNECT_EVERY = 3.0    # seconds


class ArduinoLink:
    def __init__(self, port=None):
        self.port_arg = port
        self.ser = None
        self.next_retry = 0.0
        self.last_status, self.last_status_at = None, 0.0
        self.last_drive, self.last_drive_at = None, 0.0

    @property
    def connected(self):
        return self.ser is not None

    @staticmethod
    def find_port():
        candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        return candidates[0] if candidates else None

    def _ensure_connected(self):
        if self.ser is not None:
            return True
        now = time.monotonic()
        if now < self.next_retry:
            return False
        self.next_retry = now + RECONNECT_EVERY

        import serial  # pyserial

        port = self.port_arg or self.find_port()
        if not port:
            return False
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0, write_timeout=0.5)
            time.sleep(2)  # most Arduinos reset when the port opens; wait for the bootloader
            self.ser.reset_input_buffer()
            print(f"\nArduino connected on {port}")
        except Exception as e:  # noqa: BLE001
            print(f"\nCouldn't open {port}: {e}")
            self.ser = None
            return False
        # Force everything to be re-sent after a (re)connect
        self.last_status = self.last_drive = None
        return True

    def _write(self, line):
        try:
            self.ser.write(f"{line}\n".encode())
            return True
        except Exception as e:  # noqa: BLE001
            print(f"\nLost Arduino connection: {e}")
            self._drop()
            return False

    def send(self, status):
        """Drowsiness status, e.g. 'TIRED'. Sent on change, repeated as a heartbeat."""
        if not self._ensure_connected():
            return
        msg = status.replace(" ", "")  # "NO FACE" -> "NOFACE"
        now = time.monotonic()
        if msg == self.last_status and now - self.last_status_at < STATUS_HEARTBEAT:
            return
        if self._write(msg):
            self.last_status, self.last_status_at = msg, now

    def drive(self, left, right):
        """Wheel speeds -255..255. Sent immediately on change, otherwise every DRIVE_RESEND."""
        if not self._ensure_connected():
            return
        cmd = (max(-255, min(255, int(left))), max(-255, min(255, int(right))))
        now = time.monotonic()
        if cmd == self.last_drive and now - self.last_drive_at < DRIVE_RESEND:
            return
        if self._write(f"M {cmd[0]} {cmd[1]}"):
            self.last_drive, self.last_drive_at = cmd, now

    def poll(self):
        """Print anything the Arduino sent back (handy when debugging the sketch)."""
        if self.ser is None:
            return
        try:
            reply = self.ser.read(self.ser.in_waiting or 0)
        except Exception:  # noqa: BLE001
            self._drop()
            return
        if reply:
            for line in reply.decode(errors="replace").splitlines():
                if line.strip():
                    print(f"\nArduino: {line.strip()}")

    def _drop(self):
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001
            pass
        self.ser = None

    def close(self):
        if self.ser is not None:
            try:
                self.ser.write(b"M 0 0\n")  # stop the motors on the way out
                self.ser.flush()
            except Exception:  # noqa: BLE001
                pass
            self._drop()
