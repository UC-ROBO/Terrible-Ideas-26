"""
Keyboard driving for the robot, from the Pi's terminal (works over SSH, no monitor needed).

    python teleop.py                     # auto-detect the Arduino
    python teleop.py --port /dev/ttyACM0
    python teleop.py --hold              # robot only moves while a key is held down

Keys:
    w / up arrow     forward             x / space   stop
    s / down arrow   reverse             + / -       speed up / slow down
    a / left arrow   spin left           Ctrl+C      quit (stops the robot)
    d / right arrow  spin right

Default is "toggle" driving: press w once and the robot keeps going until you press
another key. Terminals can't report when a key is released, so --hold works by stopping
the robot shortly after key-repeat events stop arriving.

The same keys also work inside the drowsiness window when running
`python drowsiness_poc.py --drive`.
"""

import argparse
import os
import select
import sys
import threading
import time

# --------------------------------------------------------------------------- tunables
DEFAULT_SPEED = 150      # 0..255 PWM
SPEED_STEP = 25
MIN_SPEED, MAX_SPEED = 50, 255
TURN_SCALE = 0.7         # spin turns at 70% of drive speed (easier to control)
HOLD_TIMEOUT = 0.6       # --hold: stop if no key-repeat arrives for this long (s).
                         # Must be longer than your keyboard's initial repeat delay.

# key -> (left direction, right direction)
MOVES = {
    "w": (1, 1),
    "s": (-1, -1),
    "a": (-1, 1),
    "d": (1, -1),
}
STOP_KEYS = {"x", " "}
ARROWS = {"A": "w", "B": "s", "C": "d", "D": "a"}  # ESC [ A/B/C/D


class KeyboardTeleop:
    def __init__(self, speed=DEFAULT_SPEED, hold=False):
        self.speed = speed
        self.hold = hold
        self.move = (0, 0)
        self.last_key_at = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._old_term = None
        self._thread = None

    # ----- input
    def start(self):
        """Read keys from the terminal in the background. Returns False if there's no terminal."""
        if not sys.stdin.isatty():
            return False
        import termios
        import tty

        fd = sys.stdin.fileno()
        self._old_term = termios.tcgetattr(fd)
        tty.setcbreak(fd)  # keys arrive immediately, Ctrl+C still works
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return True

    def _reader(self):
        fd = sys.stdin.fileno()
        while self._running:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            ch = os.read(fd, 1).decode(errors="ignore")
            if ch == "\x1b":  # arrow keys arrive as ESC [ A..D
                seq = ""
                while len(seq) < 2 and select.select([fd], [], [], 0.02)[0]:
                    seq += os.read(fd, 1).decode(errors="ignore")
                if len(seq) == 2 and seq[0] == "[" and seq[1] in ARROWS:
                    ch = ARROWS[seq[1]]
                else:
                    continue
            self.handle_key(ch)

    def handle_key(self, key):
        """Apply one key press. Returns True if it was a driving key."""
        key = key.lower()
        with self._lock:
            if key in MOVES:
                self.move = MOVES[key]
            elif key in STOP_KEYS:
                self.move = (0, 0)
            elif key in ("+", "="):
                self.speed = min(MAX_SPEED, self.speed + SPEED_STEP)
            elif key in ("-", "_"):
                self.speed = max(MIN_SPEED, self.speed - SPEED_STEP)
            else:
                return False
            self.last_key_at = time.monotonic()
            return True

    # ----- output
    def wheel_speeds(self):
        """Current (left, right) in -255..255."""
        with self._lock:
            if self.hold and time.monotonic() - self.last_key_at > HOLD_TIMEOUT:
                self.move = (0, 0)
            left, right = self.move
            spinning = left != right
            power = self.speed * (TURN_SCALE if spinning else 1.0)
            return int(left * power), int(right * power)

    def describe(self):
        names = {(1, 1): "FORWARD", (-1, -1): "REVERSE", (-1, 1): "LEFT", (1, -1): "RIGHT"}
        return f"{names.get(self.move, 'STOPPED'):8} speed {self.speed}"

    def stop(self):
        with self._lock:
            self.move = (0, 0)

    def close(self):
        self._running = False
        self.stop()
        if self._old_term is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_term)
            self._old_term = None


def main():
    from arduino_link import ArduinoLink

    ap = argparse.ArgumentParser(description="Drive the robot with the keyboard")
    ap.add_argument("--port", help="Arduino serial port (default: auto-detect)")
    ap.add_argument("--speed", type=int, default=DEFAULT_SPEED, help="starting speed 0..255")
    ap.add_argument("--hold", action="store_true", help="only move while a key is held")
    args = ap.parse_args()

    teleop = KeyboardTeleop(args.speed, args.hold)
    if not teleop.start():
        raise SystemExit("teleop.py needs an interactive terminal (run it directly or over SSH).")
    link = ArduinoLink(args.port)
    print(__doc__.split("Keys:")[1].split("Default")[0])

    try:
        while True:
            link.drive(*teleop.wheel_speeds())
            link.poll()
            state = "ok" if link.connected else "not connected"
            print(f"\r{teleop.describe()}   Arduino {state}      ", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        teleop.close()
        link.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
