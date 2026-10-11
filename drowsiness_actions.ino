/*
  drowsiness_actions.ino

  Receives commands from the Raspberry Pi over USB serial:
    - drowsiness status  -> runs the PLACEHOLDER action functions below
    - motor commands     -> drives the two wheel motors (keyboard control from teleop.py)

  Protocol (115200 baud, one message per line):
    ALERT | TIRED | DROWSY | NOFACE | CALIBRATING
    M <left> <right>        wheel speeds -255..255, negative = reverse

  Safety: the Pi resends the motor command every 100 ms. If none arrives for
  DRIVE_TIMEOUT_MS the motors stop, so the robot halts if the Pi crashes or the
  cable comes out.
*/

// Must stay above every function: the Arduino IDE auto-inserts function
// prototypes before the first function, and they use this type.
enum Status { UNKNOWN, ALERT, TIRED, DROWSY, NOFACE, CALIBRATING };

// =================================================================== MOTOR WIRING
// Written for an L298N / L9110 / TB6612-style driver: one PWM pin + two direction
// pins per motor. Change the pins to match your wiring (PWM pins must be ~ pins).
// TB6612 users: also tie STBY high.
const int LEFT_PWM  = 5;   // ENA
const int LEFT_IN1  = 7;   // IN1
const int LEFT_IN2  = 8;   // IN2
const int RIGHT_PWM = 6;   // ENB
const int RIGHT_IN1 = 9;   // IN3
const int RIGHT_IN2 = 10;  // IN4

// If a wheel spins backwards when you press forward, flip its flag (no rewiring needed).
const bool INVERT_LEFT  = false;
const bool INVERT_RIGHT = false;

const unsigned long DRIVE_TIMEOUT_MS = 500;
const unsigned long PI_TIMEOUT_MS = 3000;

// =================================================================== PLACEHOLDERS
// Enter*: runs once when the state starts.  While*: runs every loop() while in it.
// Use millis() for timing, never delay() - delay() stops the robot reading commands.

void onEnterAlert()       { /* PLACEHOLDER: e.g. turn everything off, green LED on */ }
void onEnterTired()       { /* PLACEHOLDER: e.g. amber LED, short beep */ }
void whileTired()         { /* PLACEHOLDER: e.g. beep every 10 s */ }
void onEnterDrowsy()      { /* PLACEHOLDER: e.g. red LED, start alarm / vibration */ }
void whileDrowsy()        { /* PLACEHOLDER: e.g. pulse the buzzer */ }
void onEnterNoFace()      { /* PLACEHOLDER */ }
void onEnterCalibrating() { /* PLACEHOLDER */ }
void onPiLost()           { /* PLACEHOLDER: Pi stopped sending status - e.g. fault LED */ }

// =================================================================== motors

unsigned long lastDriveAt = 0;
bool motorsRunning = false;

void setMotor(int pwmPin, int in1, int in2, int speed, bool invert) {
  speed = constrain(speed, -255, 255);
  if (invert) speed = -speed;
  digitalWrite(in1, speed > 0 ? HIGH : LOW);
  digitalWrite(in2, speed < 0 ? HIGH : LOW);
  analogWrite(pwmPin, abs(speed));
}

void setMotors(int left, int right) {
  setMotor(LEFT_PWM,  LEFT_IN1,  LEFT_IN2,  left,  INVERT_LEFT);
  setMotor(RIGHT_PWM, RIGHT_IN1, RIGHT_IN2, right, INVERT_RIGHT);
  motorsRunning = (left != 0 || right != 0);
}

// =================================================================== status

Status current = UNKNOWN;
unsigned long lastStatusAt = 0;
bool piLost = true;

Status parseStatus(const char *s) {
  if (strcmp(s, "ALERT") == 0)       return ALERT;
  if (strcmp(s, "TIRED") == 0)       return TIRED;
  if (strcmp(s, "DROWSY") == 0)      return DROWSY;
  if (strcmp(s, "NOFACE") == 0)      return NOFACE;
  if (strcmp(s, "CALIBRATING") == 0) return CALIBRATING;
  return UNKNOWN;
}

void enterState(Status s) {
  current = s;
  switch (s) {
    case ALERT:       onEnterAlert();       Serial.println(F("ack ALERT"));       break;
    case TIRED:       onEnterTired();       Serial.println(F("ack TIRED"));       break;
    case DROWSY:      onEnterDrowsy();      Serial.println(F("ack DROWSY"));      break;
    case NOFACE:      onEnterNoFace();      Serial.println(F("ack NOFACE"));      break;
    case CALIBRATING: onEnterCalibrating(); Serial.println(F("ack CALIBRATING")); break;
    default: break;
  }
}

// =================================================================== serial input

char lineBuf[24];
byte lineLen = 0;

void handleLine(const char *line) {
  if (line[0] == 'M' && line[1] == ' ') {
    int left, right;
    if (sscanf(line + 2, "%d %d", &left, &right) == 2) {
      setMotors(left, right);
      lastDriveAt = millis();
    }
    return;
  }

  Status s = parseStatus(line);
  if (s == UNKNOWN) return;
  lastStatusAt = millis();
  piLost = false;
  if (s != current) enterState(s);
}

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      lineLen = 0;
      handleLine(lineBuf);
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;  // overlong garbage line - drop it
    }
  }
}

// =================================================================== main

void setup() {
  Serial.begin(115200);
  pinMode(LEFT_PWM, OUTPUT);  pinMode(LEFT_IN1, OUTPUT);  pinMode(LEFT_IN2, OUTPUT);
  pinMode(RIGHT_PWM, OUTPUT); pinMode(RIGHT_IN1, OUTPUT); pinMode(RIGHT_IN2, OUTPUT);
  setMotors(0, 0);
  // PLACEHOLDER: pinMode(...) for your buzzer / LEDs here
}

void loop() {
  readSerial();

  // Motor watchdog
  if (motorsRunning && millis() - lastDriveAt > DRIVE_TIMEOUT_MS) {
    setMotors(0, 0);
  }

  // Status heartbeat
  if (!piLost && millis() - lastStatusAt > PI_TIMEOUT_MS) {
    piLost = true;
    current = UNKNOWN;
    onPiLost();
  }

  switch (current) {
    case TIRED:  whileTired();  break;
    case DROWSY: whileDrowsy(); break;
    default: break;
  }
}
