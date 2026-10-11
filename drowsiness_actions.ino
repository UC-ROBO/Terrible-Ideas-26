/*
  drowsiness_actions.ino

  Receives the driver/user status from the Raspberry Pi over USB serial and runs an
  action for each state. Fill in the PLACEHOLDER functions with your hardware
  (buzzer, vibration motor, LEDs, relay, ...).

  Protocol (115200 baud, one word per line, sent by drowsiness_poc.py):
    ALERT | TIRED | DROWSY | NOFACE | CALIBRATING
  The Pi repeats the current status every second as a heartbeat. If nothing arrives
  for PI_TIMEOUT_MS, onPiLost() runs.
*/

enum Status { UNKNOWN, ALERT, TIRED, DROWSY, NOFACE, CALIBRATING };

const unsigned long PI_TIMEOUT_MS = 3000;

Status current = UNKNOWN;
unsigned long lastMessageAt = 0;
bool piLost = true;

char lineBuf[16];
byte lineLen = 0;

// ------------------------------------------------------------------- PLACEHOLDERS
// Enter*: runs once when the state starts.  While*: runs every loop() while in it.

void onEnterAlert()       { /* PLACEHOLDER: e.g. turn everything off, green LED on */ }
void onEnterTired()       { /* PLACEHOLDER: e.g. amber LED, short beep */ }
void whileTired()         { /* PLACEHOLDER: e.g. beep every 10 s (use millis(), not delay()) */ }
void onEnterDrowsy()      { /* PLACEHOLDER: e.g. red LED, start alarm / vibration */ }
void whileDrowsy()        { /* PLACEHOLDER: e.g. pulse the buzzer */ }
void onEnterNoFace()      { /* PLACEHOLDER: e.g. nothing, or blink an LED */ }
void onEnterCalibrating() { /* PLACEHOLDER */ }
void onPiLost()           { /* PLACEHOLDER: Pi stopped sending - e.g. fault LED, silence alarms */ }

// ------------------------------------------------------------------- plumbing

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

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      lineLen = 0;
      Status s = parseStatus(lineBuf);
      if (s == UNKNOWN) continue;
      lastMessageAt = millis();
      piLost = false;
      if (s != current) enterState(s);
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;  // overlong garbage line - drop it
    }
  }
}

void setup() {
  Serial.begin(115200);
  // PLACEHOLDER: pinMode(...) for your buzzer / LEDs / motor here
}

void loop() {
  readSerial();

  if (!piLost && millis() - lastMessageAt > PI_TIMEOUT_MS) {
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
