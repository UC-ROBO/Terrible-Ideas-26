/*
  drowsiness_actions.ino  (L293D motor shield: 2 drive motors + 1 stepper)

  Receives commands from the Raspberry Pi over USB serial:
    - drowsiness status  -> stepper turns 90 degrees when TIRED or DROWSY, back when ALERT
                            and runs the PLACEHOLDER action functions below
    - motor commands     -> drives the left/right wheel motors

  Protocol (115200 baud, one message per line):
    ALERT | TIRED | DROWSY | NOFACE | CALIBRATING
    M <left> <right>        wheel speeds -255..255, negative = reverse

  Hardware: L293D motor shield (Adafruit v1 or clone) - 3 chips, two 5-screw blocks.
    M1  left drive motor        M2  right drive motor
    M3 + M4  stepper (4-wire: coil A on M3, coil B on M4;
                      5-wire 28BYJ-48: red common wire in the middle GND screw)
  Library: "Adafruit Motor Shield library" (AFMotor) - the original, not V2.
  Free pins for your placeholders: 2, 13, A0-A5.

  Safety: the Pi resends the motor command every 100 ms. If none arrives for
  DRIVE_TIMEOUT_MS the drive motors stop.

  Stepper position: there's no limit switch, so the sketch remembers the last position
  in EEPROM. The very first time, power up with the stepper in the parallel position.
*/

#include <AFMotor.h>
#include <EEPROM.h>

// Must stay above every function: the Arduino IDE auto-inserts function
// prototypes before the first function, and they use this type.
enum Status { UNKNOWN, ALERT, TIRED, DROWSY, NOFACE, CALIBRATING };

// =================================================================== SETTINGS

// ---- drive motors
const uint8_t LEFT_MOTOR  = 1;     // terminal M1
const uint8_t RIGHT_MOTOR = 2;     // terminal M2
const bool INVERT_LEFT  = false;   // flip if that wheel spins backwards on "forward"
const bool INVERT_RIGHT = false;
const unsigned long DRIVE_TIMEOUT_MS = 500;

// ---- stepper (on M3 + M4)
// Steps for one full turn: 200 for most NEMA / 1.8-degree steppers,
// 2048 for the 28BYJ-48 (the small geared one with a 5-wire plug).
const uint16_t STEPS_PER_REV   = 2048;
const uint16_t TURN_STEPS      = STEPS_PER_REV / 4;  // 90 degrees
const uint8_t  RAISE_DIRECTION = FORWARD;            // BACKWARD to turn the other way
const uint16_t STEPPER_RPM     = 10;                 // 28BYJ-48: keep <= 15
const uint8_t  STEP_STYLE      = DOUBLE;             // DOUBLE = most torque
const bool RELEASE_WHEN_IDLE   = true;   // unpower coils when still (stops heating;
                                         // set false if the load drifts back)
const bool LOWER_WHEN_PI_LOST  = true;   // return to parallel if the Pi stops talking

const unsigned long PI_TIMEOUT_MS = 3000;

// Which status puts the stepper where. NOFACE / CALIBRATING leave it where it is,
// so looking away for a moment doesn't make it flap up and down.
long stepperTargetFor(Status s, long current) {
  switch (s) {
    case TIRED:
    case DROWSY: return TURN_STEPS;   // 90 degrees
    case ALERT:  return 0;            // parallel
    default:     return current;
  }
}

// =================================================================== PLACEHOLDERS
// Enter*: runs once when the state starts.  While*: runs every loop() while in it.
// Use millis() for timing, never delay() - it would stall the motors and stepper.

void onEnterAlert()       { /* PLACEHOLDER: e.g. green LED on */ }
void onEnterTired()       { /* PLACEHOLDER: e.g. amber LED, short beep */ }
void whileTired()         { /* PLACEHOLDER: e.g. beep every 10 s */ }
void onEnterDrowsy()      { /* PLACEHOLDER: e.g. red LED, start alarm */ }
void whileDrowsy()        { /* PLACEHOLDER: e.g. pulse the buzzer */ }
void onEnterNoFace()      { /* PLACEHOLDER */ }
void onEnterCalibrating() { /* PLACEHOLDER */ }
void onPiLost()           { /* PLACEHOLDER: Pi stopped sending status - e.g. fault LED */ }
void setupPlaceholders()  { /* PLACEHOLDER: pinMode(...) for your buzzer / LEDs (2, 13, A0-A5) */ }

// =================================================================== drive motors

AF_DCMotor leftMotor(LEFT_MOTOR);
AF_DCMotor rightMotor(RIGHT_MOTOR);
unsigned long lastDriveAt = 0;
bool motorsRunning = false;

void driveMotor(AF_DCMotor &m, int speed) {
  m.setSpeed(abs(speed));
  if (speed > 0)      m.run(FORWARD);
  else if (speed < 0) m.run(BACKWARD);
  else                m.run(RELEASE);
}

void setMotors(int left, int right) {
  left  = constrain(left,  -255, 255);
  right = constrain(right, -255, 255);
  driveMotor(leftMotor,  INVERT_LEFT  ? -left  : left);
  driveMotor(rightMotor, INVERT_RIGHT ? -right : right);
  motorsRunning = (left != 0 || right != 0);
}

// =================================================================== stepper
// Moves one step per loop() pass, so driving and serial keep working while it turns.

AF_Stepper stepper(STEPS_PER_REV, 2);   // port 2 = M3 + M4
const unsigned long STEP_INTERVAL_US = 60000000UL / ((unsigned long)STEPS_PER_REV * STEPPER_RPM);

const int  EEPROM_ADDR  = 0;
const byte EEPROM_MAGIC = 0xA7;
long stepperPos = 0;       // steps away from parallel, 0..TURN_STEPS
long stepperTarget = 0;
bool stepperMoving = false;
unsigned long lastStepAt = 0;

void stepperBegin() {
  if (EEPROM.read(EEPROM_ADDR) == EEPROM_MAGIC) {
    EEPROM.get(EEPROM_ADDR + 1, stepperPos);
    if (stepperPos < 0 || stepperPos > (long)TURN_STEPS) stepperPos = 0;
  }
  stepperTarget = stepperPos;
}

void stepperSavePosition() {
  EEPROM.update(EEPROM_ADDR, EEPROM_MAGIC);
  EEPROM.put(EEPROM_ADDR + 1, stepperPos);  // put() only rewrites bytes that changed
}

void stepperUpdate() {
  if (stepperPos == stepperTarget) {
    if (stepperMoving) {
      stepperMoving = false;
      if (RELEASE_WHEN_IDLE) stepper.release();
      stepperSavePosition();
      Serial.println(stepperPos == 0 ? F("stepper parallel") : F("stepper 90"));
    }
    return;
  }
  unsigned long now = micros();
  if (stepperMoving && now - lastStepAt < STEP_INTERVAL_US) return;
  lastStepAt = now;
  stepperMoving = true;

  bool raising = stepperTarget > stepperPos;
  uint8_t lowerDirection = (RAISE_DIRECTION == FORWARD) ? BACKWARD : FORWARD;
  stepper.onestep(raising ? RAISE_DIRECTION : lowerDirection, STEP_STYLE);
  stepperPos += raising ? 1 : -1;
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
  stepperTarget = stepperTargetFor(s, stepperTarget);
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
  setMotors(0, 0);
  stepperBegin();
  setupPlaceholders();
}

void loop() {
  readSerial();

  // Drive motor watchdog
  if (motorsRunning && millis() - lastDriveAt > DRIVE_TIMEOUT_MS) {
    setMotors(0, 0);
  }

  // Status heartbeat
  if (!piLost && millis() - lastStatusAt > PI_TIMEOUT_MS) {
    piLost = true;
    current = UNKNOWN;
    if (LOWER_WHEN_PI_LOST) stepperTarget = 0;
    onPiLost();
  }

  stepperUpdate();

  switch (current) {
    case TIRED:  whileTired();  break;
    case DROWSY: whileDrowsy(); break;
    default: break;
  }
}
