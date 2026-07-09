#pragma once
// Pure type/name shims — nothing here has behavior.
//
// The firmware was originally written against the board-specific
// `M5StickCPlus` library (TFT_eSPI display types, AXP192 RTC structs). It
// now builds against M5Unified on every target; this header re-creates the
// legacy names the render code still uses on top of M5GFX so the ~3k lines
// of UI code stay untouched.
//
// Anything behavioral (power, clock, LED, audio, IMU) lives behind the
// board:: seam in src/hal/hal.h.
#include <M5Unified.h>

// TFT_eSprite -> M5Canvas (off-screen sprite, same drawing API).
// TFT_eSPI    -> LovyanGFX, the common base of both M5.Lcd and M5Canvas —
//                the *RenderTo() helpers take a pointer to it so they can
//                draw to either the sprite or the live LCD (landscape clock).
using TFT_eSprite = M5Canvas;
using TFT_eSPI    = LovyanGFX;

// Legacy AXP192-era RTC struct names on top of M5Unified's equivalents.
typedef m5::rtc_time_t RTC_TimeTypeDef;
typedef m5::rtc_date_t RTC_DateTypeDef;
