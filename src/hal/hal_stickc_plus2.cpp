// M5StickC Plus2 backend — ESP32-PICO-V3-02 + AXP2101 PMIC, BM8563 RTC,
// buzzer, red LED on GPIO19. M5Unified's AXP2101 driver exposes charge
// state and battery level only — no current sense, no PMIC temperature,
// no coulomb counter — so this backend is the basics.
#if defined(BOARD_STICKC_PLUS2)
#include "hal.h"
#include <M5Unified.h>

namespace board {

static const int LED_PIN = 19;   // red LED, active-high (G19 drives it directly)

void begin() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Speaker.begin();
  M5.Speaker.setVolume(160);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);    // off
}

const char* name() { return "M5StickC Plus2"; }

// AXP2101 board: hardware power-off is the 6s hold; single click toggles
// the screen. No software double-click gesture.
bool powerOffRequested()     { return false; }
bool screenToggleRequested() { return M5.BtnPWR.wasClicked(); }

// None of the AXP192 extras are reachable through M5Unified's AXP2101 driver.
bool  hasBatteryCurrent() { return false; }
int   batteryCurrentMa()  { return 0; }
bool  hasCoulomb()        { return false; }
void  coulombEnable()     {}
void  coulombClear()      {}
float coulombMah()        { return 0.0f; }
bool  hasInternalTemp()   { return false; }
int   internalTempC()     { return 0; }

void attentionLed(bool on) { digitalWrite(LED_PIN, on ? HIGH : LOW); }

// Same battery-backed BM8563 as the original Plus.
void setClock(const struct tm* lt) { M5.Rtc.setDateTime(lt); }

bool getClock(struct tm* out) {
  m5::rtc_datetime_t dt;
  if (!M5.Rtc.getDateTime(&dt)) return false;
  *out = dt.get_tm();
  return true;
}

bool clockValid() { return M5.Rtc.isEnabled() && !M5.Rtc.getVoltLow(); }

}  // namespace board
#endif  // BOARD_STICKC_PLUS2
