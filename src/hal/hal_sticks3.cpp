// M5StickS3 backend — ESP32-S3 + M5PM1 PMIC, ES8311 speaker, BMI270 IMU.
// No battery-backed RTC, no user LED, no coulomb counter / current sense.
#if defined(BOARD_STICKS3)
#include "hal.h"
#include <M5Unified.h>
#include "../logic/clock_time_logic.h"

namespace board {

void begin() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Speaker.begin();
  M5.Speaker.setVolume(160);
  // BMI270 IMU is configured by M5.begin(); no AXP-style extras to set up.
}

const char* name() { return "M5StickS3"; }

// Power button: double-click powers off, single click toggles the screen.
// The board reserves a long hold for download/reset mode, so — unlike the
// AXP sticks — the hold can't be the off gesture (bjedwards 75729fb).
bool powerOffRequested()     { return M5.BtnPWR.wasDoubleClicked(); }
bool screenToggleRequested() { return M5.BtnPWR.wasClicked(); }

// M5PM1: no current sense, no coulomb counter.
bool  hasBatteryCurrent() { return false; }
int   batteryCurrentMa()  { return 0; }
bool  hasCoulomb()        { return false; }
void  coulombEnable()     {}
void  coulombClear()      {}
float coulombMah()        { return 0.0f; }

// No PMIC temperature — report the ESP32-S3 die sensor, which is what the
// DEVICE page always showed on this board.
bool hasInternalTemp() { return true; }
int  internalTempC()   { return (int)temperatureRead(); }

void attentionLed(bool) {}   // no user-controllable LED

// Software wall-clock (no RTC hardware): seed from the bridge time push,
// project with millis(). Math lives in logic/clock_time_logic.h so it
// unit-tests natively.
static SoftClock _clk = { false, 0, 0 };

void setClock(const struct tm* lt) { softClockSeed(_clk, lt, millis()); }
bool getClock(struct tm* out)      { return softClockGet(_clk, millis(), out); }
bool clockValid()                  { return _clk.set; }

}  // namespace board
#endif  // BOARD_STICKS3
