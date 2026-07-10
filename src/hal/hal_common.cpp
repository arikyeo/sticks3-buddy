// Board-independent HAL paths — everything M5Unified already unifies across
// the supported sticks. Per-board backends live in hal_sticks3.cpp /
// hal_stickc_plus.cpp / hal_stickc_plus2.cpp.
#include "hal.h"
#include <M5Unified.h>

namespace board {

void update() { M5.update(); }

// ---------------------------------------------------------------------------
// Display
// ---------------------------------------------------------------------------
void setBrightness(uint8_t v0_255) { M5.Display.setBrightness(v0_255); }

void screenPower(bool on) {
  if (on) { M5.Display.wakeup(); }
  else    { M5.Display.setBrightness(0); M5.Display.sleep(); }
}

// ---------------------------------------------------------------------------
// Battery / power (generic across all three PMICs via M5.Power)
// ---------------------------------------------------------------------------
int  batteryMilliVolts() { int mv = M5.Power.getBatteryVoltage(); return mv < 0 ? 0 : mv; }

// Coarse linear voltage estimate, 3.2V..4.2V -> 0..100%. Matches the historic
// DEVICE-page figure (reads high on USB); also the S3's only battery gauge.
int  batteryPercent() { int p = (batteryMilliVolts() - 3200) / 10; return p < 0 ? 0 : p > 100 ? 100 : p; }

int  usbMilliVolts() { int mv = M5.Power.getVBUSVoltage(); return mv < 0 ? 0 : mv; }
bool onUsb()         { return usbMilliVolts() > 4000; }
bool isCharging()    { return M5.Power.isCharging() == m5::Power_Class::is_charging; }
void powerOff()      { M5.Power.powerOff(); }

// ---------------------------------------------------------------------------
// Audio — StickC Plus/Plus2 buzzer and StickS3 ES8311 both drive through
// M5.Speaker under M5Unified.
// ---------------------------------------------------------------------------
void beep(uint16_t freq, uint16_t durMs) { M5.Speaker.tone((float)freq, durMs); }

// Interleaved (freqHz, durMs) pairs; n = total uint16_t count (2 per note).
// M5.Speaker.tone is one-at-a-time — a second call stomps the first — so a
// sequence has to be paced. Blocks for the summed duration; keep sequences
// short (UI chimes, not music).
void playTone(const uint16_t* seq, size_t n) {
  if (!seq) return;
  for (size_t i = 0; i + 1 < n; i += 2) {
    M5.Speaker.tone((float)seq[i], seq[i + 1]);
    delay(seq[i + 1]);
  }
}

void setVolume(uint8_t v0_255) { M5.Speaker.setVolume(v0_255); }

// ---------------------------------------------------------------------------
// Sensors / IO
// ---------------------------------------------------------------------------
void imuAccel(float* ax, float* ay, float* az) { M5.Imu.getAccel(ax, ay, az); }

void serialInit() {
#if defined(BOARD_STICKS3)
  // USB CDC: with no host attached, a full TX ring would otherwise block the
  // loop on every Serial print. Zero timeout = drop instead of stall.
  Serial.setTxTimeoutMs(0);
#endif
  // Drain whatever arrived before we were ready (boot noise, half a line
  // from a bridge that never noticed the reboot) so the first JSON line
  // parsed is a whole one.
  while (Serial.available()) Serial.read();
}

}  // namespace board
