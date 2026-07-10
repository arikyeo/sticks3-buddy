// M5StickC Plus backend — ESP32 + AXP192 PMIC, BM8563 RTC, buzzer, red LED
// on GPIO10 (active-low). The AXP192 brings the extras: battery current
// sense, internal temperature, and a hardware coulomb counter.
// Register access ported from ttpears/main src/board.cpp.
#if defined(BOARD_STICKC_PLUS)
#include "hal.h"
#include <M5Unified.h>

namespace board {

static const int LED_PIN = 10;   // red LED, active-low

void begin() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Speaker.begin();
  M5.Speaker.setVolume(160);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);   // off
  coulombEnable();               // free — rides the always-on battery ADC
}

const char* name() { return "M5StickC Plus"; }

// AXP192 boards power off via the hardware 6s hold on the power button, so
// there is no software double-click gesture; single click toggles the screen.
bool powerOffRequested()     { return false; }
bool screenToggleRequested() { return M5.BtnPWR.wasClicked(); }

bool hasBatteryCurrent() { return true; }
int  batteryCurrentMa() {
  // + when charging, - when discharging, matching the old GetBatCurrent sign.
  return (int)(M5.Power.Axp192.getBatteryChargeCurrent()
             - M5.Power.Axp192.getBatteryDischargeCurrent());
}

bool hasInternalTemp() { return true; }
int  internalTempC()   { return (int)M5.Power.Axp192.getInternalTemperature(); }

// Coulomb counter: control reg 0xB8 (bit7 enable, bit5 clear), 32-bit charge /
// discharge accumulators at 0xB0 / 0xB4. Formula matches the M5StickCPlus lib:
// mAh = 65536 * 0.5 * (charge - discharge) / 3600 / 25 (ADC rate).
static uint32_t axpRead32(uint8_t reg) {
  uint8_t b[4] = {0};
  M5.Power.Axp192.readRegister(reg, b, 4);
  return ((uint32_t)b[0] << 24) | ((uint32_t)b[1] << 16)
       | ((uint32_t)b[2] << 8)  |  (uint32_t)b[3];
}

bool  hasCoulomb()    { return true; }
void  coulombEnable() { M5.Power.Axp192.writeRegister8(0xB8, 0x80); }
void  coulombClear()  { M5.Power.Axp192.writeRegister8(0xB8, 0xA0); }  // enable+clear
float coulombMah() {
  int32_t coin  = (int32_t)axpRead32(0xB0);
  int32_t coout = (int32_t)axpRead32(0xB4);
  return 65536.0f * 0.5f * (float)(coin - coout) / 3600.0f / 25.0f;
}

void attentionLed(bool on) { digitalWrite(LED_PIN, on ? LOW : HIGH); }

// Battery-backed BM8563 RTC. clockValid() goes false when the RTC reports a
// low-voltage event (coin cell died / first boot) — the stored time is
// garbage until the bridge re-syncs it.
void setClock(const struct tm* lt) { M5.Rtc.setDateTime(lt); }

bool getClock(struct tm* out) {
  m5::rtc_datetime_t dt;
  if (!M5.Rtc.getDateTime(&dt)) return false;
  *out = dt.get_tm();
  return true;
}

bool clockValid() { return M5.Rtc.isEnabled() && !M5.Rtc.getVoltLow(); }

}  // namespace board
#endif  // BOARD_STICKC_PLUS
