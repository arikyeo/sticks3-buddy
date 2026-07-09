#pragma once
// Hardware abstraction seam for the buddy firmware (namespace board).
//
// All supported boards run on M5Unified; this seam hides what genuinely
// differs between them:
//
//   M5StickS3       (BOARD_STICKS3)      : ESP32-S3 + M5PM1 PMIC, no RTC,
//                                          ES8311 speaker, no user LED,
//                                          BMI270 IMU, USB CDC serial
//   M5StickC Plus   (BOARD_STICKC_PLUS)  : ESP32 + AXP192 PMIC (coulomb
//                                          counter, current sense, temp),
//                                          BM8563 RTC, buzzer, LED GPIO10
//   M5StickC Plus2  (BOARD_STICKC_PLUS2) : ESP32 + AXP2101 PMIC (basics
//                                          only), BM8563 RTC, buzzer,
//                                          LED GPIO19
//
// The board is picked at compile time by the PlatformIO env flag. Shared
// M5Unified paths live in hal_common.cpp; per-board backends live in
// hal_sticks3.cpp / hal_stickc_plus.cpp / hal_stickc_plus2.cpp, each fully
// guarded by its flag so build_src_filter stays a plain +<*>.
//
// Pure type/name shims (TFT_eSprite and friends) live in src/compat.h —
// this header is behavior only.
#include <stdint.h>
#include <stddef.h>
#include <time.h>

#if !defined(BUDDY_NATIVE_TEST)
#if !defined(BOARD_STICKS3) && !defined(BOARD_STICKC_PLUS) && !defined(BOARD_STICKC_PLUS2)
#error "no board flag: define BOARD_STICKS3 / BOARD_STICKC_PLUS / BOARD_STICKC_PLUS2 in the env"
#endif
#endif

namespace board {

// ---- lifecycle --------------------------------------------------------------
void begin();                    // M5.begin + speaker + per-board power setup
void update();                   // once per loop; runs M5.update()
const char* name();              // human-readable board name

// ---- display ----------------------------------------------------------------
void setBrightness(uint8_t v0_255);
void screenPower(bool on);       // backlight + panel sleep/wake

// ---- battery / power ----------------------------------------------------------
int  batteryMilliVolts();
int  batteryPercent();           // coarse linear 3.2V..4.2V -> 0..100
int  usbMilliVolts();
bool onUsb();
bool isCharging();
void powerOff();                 // PMIC off on battery; deep-sleep on USB

// Power-button gestures, valid for the loop iteration after update().
// powerOffRequested: S3 double-click (its PMIC reserves the long hold for
// download mode); false on AXP boards where the 6s hardware hold powers off.
// screenToggleRequested: single click on every board.
bool powerOffRequested();
bool screenToggleRequested();

// ---- battery extras (false / 0 where the PMIC can't) -------------------------
bool  hasBatteryCurrent();
int   batteryCurrentMa();        // signed: + charging, - discharging
bool  hasCoulomb();              // AXP192 hardware coulomb counter
void  coulombEnable();
void  coulombClear();
float coulombMah();              // net mAh (charge - discharge) since clear
bool  hasInternalTemp();
int   internalTempC();           // PMIC temp (AXP192) or MCU sensor (S3)

// ---- audio --------------------------------------------------------------------
void beep(uint16_t freq, uint16_t durMs);        // non-blocking single tone
void playTone(const uint16_t* seq, size_t n);    // interleaved freq,dur pairs; blocks
void setVolume(uint8_t v0_255);                  // master volume

// ---- LED ------------------------------------------------------------------------
void attentionLed(bool on);      // no-op on boards without a user LED

// ---- clock / time-of-day ----------------------------------------------------
// setClock() seeds local time from the bridge's {"time":[...]} push (fields
// already localized). Battery-backed BM8563 on the StickC Plus / Plus2; a
// millis-projected software clock on the StickS3 (no RTC — resets on reboot
// until the bridge re-pushes).
void setClock(const struct tm* lt);
bool getClock(struct tm* out);   // false until a valid time source exists
bool clockValid();

// ---- sensors / io ---------------------------------------------------------------
void imuAccel(float* ax, float* ay, float* az);
void serialInit();               // per-board serial quirks (S3: CDC no-block TX)

}  // namespace board
