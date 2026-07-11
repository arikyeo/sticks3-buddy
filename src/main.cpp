#include "compat.h"
#include "config.h"
#include "hal/hal.h"
#include "logic/wrap_text_logic.h"
#include "logic/persona_logic.h"
#include "logic/clock_display_logic.h"
#include "logic/clock_orient_logic.h"
#include "logic/clock_time_logic.h"
#include "logic/pomodoro_logic.h"
#include "logic/gesture_logic.h"
#include <LittleFS.h>
#include <stdarg.h>
#include "esp_task_wdt.h"
#include "ble_bridge.h"
#include "data.h"
#include "buddy.h"
#ifdef BUDDY_EXTRAS_FULL
#include "audio/tunes.h"   // tune engine + slot registry (8MB boards only)
#include "ir/ir_remote.h"  // raw RMT learn/replay (8MB boards only)
#endif
#ifdef BUDDY_OTA
#include "ota/ota.h"       // WiFi OTA from GitHub releases (-ota envs only)
#endif
#include "esp_ota_ops.h"   // rollback mark-valid on healthy boot (all builds)

#ifdef BUDDY_DEBUG
#include "esp_system.h"
// Reset-reason diagnostics (ttpears): survives resets but not power-on —
// RTC_NOINIT memory is garbage on cold boot, so a magic word gates the read.
RTC_NOINIT_ATTR uint32_t g_lastReset, g_lastResetMagic;
#endif

TFT_eSprite spr = TFT_eSprite(&M5.Lcd);

// Advertise as "Claude-XXXX" (last two BT MAC bytes) so multiple sticks
// in one room are distinguishable in the desktop picker. Name persists in
// btName for the BLUETOOTH info page.
static char btName[16] = "Claude";
static void startBt(uint8_t pairMode) {
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_BT);
  snprintf(btName, sizeof(btName), "Claude-%02X%02X", mac[4], mac[5]);
  bleInit(btName, pairMode);
}

#include "character.h"
#include "stats.h"
const int W = 135, H = 240;
const int CX = W / 2;
const int CY_BASE = 120;

// Colors used across multiple UI surfaces
const uint16_t HOT   = 0xFA20;   // red-orange: warnings, impatience, deny
const uint16_t PANEL = 0x2104;   // overlay panel background

// PersonaState / stateNames / derivePersona live in logic/persona_logic.h

TamaState    tama;
PersonaState baseState   = P_SLEEP;
PersonaState activeState = P_SLEEP;
uint32_t     oneShotUntil = 0;
uint32_t     lastShakeCheck = 0;
float        accelBaseline = 1.0f;
GestureState gest;   // flip -> host switch, double-tap -> wake (gesture_logic.h)
unsigned long t = 0;

// Menu
bool    menuOpen    = false;
uint8_t menuSel     = 0;
// brightness level stored in settings().bright (0..4 → ScreenBreath 20..100)
bool    btnALong    = false;
bool    btnBLong    = false;

enum DisplayMode { DISP_NORMAL, DISP_SESSIONS, DISP_PET, DISP_CLOCK, DISP_CARDS, DISP_INFO, DISP_COUNT };
uint8_t displayMode = DISP_NORMAL;
uint8_t cardSel = 0;    // DISP_CARDS view index, 0 = newest
uint8_t infoPage = 0;
uint8_t petPage = 0;
const uint8_t PET_PAGES = 3;
uint8_t msgScroll = 0;
uint16_t lastLineGen = 0;
char     lastPromptId[40] = "";
uint32_t lastInteractMs = 0;
bool     dimmed = false;
bool     screenOff = false;
bool     swallowBtnA = false;
bool     swallowBtnB = false;
bool     buddyMode = false;
bool     gifAvailable = false;
const uint8_t SPECIES_GIF = 0xFF;   // species NVS sentinel: use the installed GIF

// Cycle GIF (if installed) → ASCII species 0..N-1 → GIF. Persisted to the
// existing "species" NVS key; 0xFF means GIF mode.
static void nextPet() {
  uint8_t n = buddySpeciesCount();
  if (!buddyMode) {                          // GIF → species 0
    buddyMode = true;
    buddySetSpeciesIdx(0);
    speciesIdxSave(0);
  } else if (buddySpeciesIdx() + 1 >= n && gifAvailable) {  // last species → GIF
    buddyMode = false;
    speciesIdxSave(SPECIES_GIF);
  } else {                                   // species i → species i+1
    buddyNextSpecies();
  }
  characterInvalidate();
  if (buddyMode) buddyInvalidate();
}
uint32_t wakeTransitionUntil = 0;
const uint32_t SCREEN_OFF_MS = 30000;

bool     napping = false;
uint32_t napStartMs = 0;
uint32_t promptArrivedMs = 0;

// Face-down = Z-axis dominant and negative. Debounced so a toss doesn't count.
static bool isFaceDown() {
  float ax, ay, az;
  board::imuAccel(&ax, &ay, &az);
  return az < -0.7f && fabsf(ax) < 0.4f && fabsf(ay) < 0.4f;
}

// Backlight on the historic AXP192 0..100 scale, remapped to M5GFX's 0..255
// so the tuned dim/breath values carry over unchanged.
static void screenBreath(uint8_t v0_100) { board::setBrightness((uint8_t)((uint16_t)v0_100 * 255 / 100)); }
static void applyBrightness() { screenBreath(20 + settings().bright * 20); }

// --- Screen-off power scaling (Track F P7, ttpears pattern) -----------------
// While the screen is off, drop the core clock and relax the BLE connection
// interval; wake() restores both. 80MHz is the radio-safe floor on every
// supported die — the BT radio needs the APB at >=80MHz (ttpears bench: at
// 40MHz the link goes dark while screen-off) — so the downclock never has to
// differ per board. Overridable from a bench build. esp_pm/light-sleep is a
// separate, deliberately-skipped lever (needs an IDF-tuned build; future).
#ifndef BUDDY_SCREEN_OFF_CPU_MHZ
#define BUDDY_SCREEN_OFF_CPU_MHZ 80
#endif
// Full clock to restore on wake. Captured at boot instead of hardcoding 240:
// the m5stickc-plus env pins f_cpu to 160MHz for its power budget, and wake()
// must not overclock past what the env configured.
static uint32_t cpuFullMhz = 240;

// True while something latency- or timing-critical is running that must veto
// the screen-off downclock: a character transfer (flash writes + BLE
// throughput), an IR learn (RMT capture), or an OTA update. OTA and IR-learn
// only run with the screen on today, but the guard keeps that invariant
// explicit rather than incidental.
static bool powerBusyGuard() {
  if (xferActive()) return true;
#ifdef BUDDY_EXTRAS_FULL
  if (irLearning()) return true;
#endif
#ifdef BUDDY_OTA
  if (otaInProgress()) return true;
#endif
  return false;
}

static void wake() {
  lastInteractMs = millis();
  if (screenOff) {
    // Order matters: full clock first (render + JSON parse speed), then the
    // tight BLE link (prompt/transcript delivery), then light the panel —
    // so by the time pixels appear the pipe behind them is already fast.
    setCpuFrequencyMhz(cpuFullMhz);
    bleConnParams(false);
    board::screenPower(true);
    applyBrightness();
    screenOff = false;
    wakeTransitionUntil = millis() + 12000;
  }
  if (dimmed) { applyBrightness(); dimmed = false; }
}
bool     responseSent = false;

// Volume level 0..4 -> speaker master volume. 0 mutes: beep() drops the
// tone entirely rather than playing it at volume 0.
static const uint8_t VOL_MAP[5] = { 0, 64, 128, 190, 255 };
static void applyVolume() {
  uint8_t v = settings().vol;
  if (v > 4) v = 4;
  board::setVolume(VOL_MAP[v]);
}

static void beep(uint16_t freq, uint16_t dur) {
  if (settings().vol) board::beep(freq, dur);
}

#ifdef BUDDY_EXTRAS_FULL
// Jingle wrapper: play a tune slot when the tunes setting is on, volume is
// up and the slot has data; false = caller falls back to the classic beep.
static bool tunesJingle(const Tune* t) {
  if (!settings().tune || !settings().vol) return false;
  if (!t || !(t->len || t->pcm)) return false;
  jingleStart(t);
  return true;
}
#define TUNE_OR_BEEP(slot, f, d) do { if (!tunesJingle(slot)) beep((f), (d)); } while (0)
#else
// Non-FULL boards keep the classic beep as the only sound engine; the
// slot token is never expanded, so SLOT_* needn't exist here.
#define TUNE_OR_BEEP(slot, f, d) beep((f), (d))
#endif

static void sendCmd(const char* json) {
  Serial.println(json);
  size_t n = strlen(json);
  bleWrite((const uint8_t*)json, n);
  bleWrite((const uint8_t*)"\n", 1);
}
const uint8_t INFO_PAGES = 7;
const uint8_t INFO_PG_BUTTONS = 1;
const uint8_t INFO_PG_HOSTS   = 5;
const uint8_t INFO_PG_CREDITS = 6;

void applyDisplayMode() {
  bool peek = displayMode != DISP_NORMAL;
  characterSetPeek(peek);
  buddySetPeek(peek);
  // Clear the whole sprite on mode switch. drawInfo/drawPet clear their
  // own regions when they run, but when you switch FROM info/pet TO normal,
  // those functions stop running and their stale pixels stay behind. Full
  // clear is cheap and guarantees no leftovers between modes.
  spr.fillSprite(0x0000);
  characterInvalidate();  // redraws character on next tick (text mode path)
}

// OTA builds append an "update..." row (kept just above "close" so the
// muscle-memory indices of everything else survive across build tiers).
#ifdef BUDDY_OTA
const char* menuItems[] = { "hosts", "settings", "extras", "turn off", "help", "about", "demo", "update...", "close" };
const uint8_t MENU_N = 9;
#else
const char* menuItems[] = { "hosts", "settings", "extras", "turn off", "help", "about", "demo", "close" };
const uint8_t MENU_N = 8;
#endif

// extras: pomodoro (and, on BUDDY_EXTRAS_FULL builds, more) live on a
// screen parked OUTSIDE the A-press carousel — reached via menu > extras,
// left with A-long. On it, A and B belong to the active extras page.
const uint8_t DISP_EXTRAS = DISP_COUNT;
bool      extrasOpen = false;   // the menu > extras submenu
uint8_t   extrasSel  = 0;
uint8_t   extrasPage = 0;       // 0 = pomodoro, 1 = IR remote (FULL builds)
PomoState pomo;
#ifdef BUDDY_EXTRAS_FULL
const char* extrasItems[] = { "pomodoro", "ir remote", "back" };
const uint8_t EXTRAS_N = 3;
uint8_t  irSel = 0;             // IR page slot cursor
uint32_t irLearnDeadline = 0;   // learn session timeout tick
#else
const char* extrasItems[] = { "pomodoro", "back" };
const uint8_t EXTRAS_N = 2;
#endif

bool    settingsOpen = false;
uint8_t settingsSel  = 0;
#ifdef BUDDY_EXTRAS_FULL
// FULL builds get a "tunes" row after volume; applySetting/drawSettings
// remap the rows below it so the base indices stay board-independent.
const char* settingsItems[] = { "brightness", "volume", "tunes", "pairing", "host mode", "bluetooth", "wifi", "led", "transcript", "clock rot", "ascii pet", "reset", "back" };
const uint8_t SETTINGS_N = 13;
#else
const char* settingsItems[] = { "brightness", "volume", "pairing", "host mode", "bluetooth", "wifi", "led", "transcript", "clock rot", "ascii pet", "reset", "back" };
const uint8_t SETTINGS_N = 12;
#endif

bool    resetOpen = false;
uint8_t resetSel  = 0;
const char* resetItems[] = { "delete char", "forget all hosts", "factory reset", "back" };
const uint8_t RESET_N = 4;
static uint32_t resetConfirmUntil = 0;
static uint8_t  resetConfirmIdx = 0xFF;

// hosts menu (multi-host P4/P6): registry rows + auto / add host / forget / back
bool    hostsOpen  = false;
uint8_t hostsSel   = 0;
bool    forgetOpen = false;
uint8_t forgetSel  = 0;

// ask overlay (protocol v2, read-only): dismissed locally with B / A-long,
// re-armed when a new ask id arrives. Cursor just scrolls the options.
char    lastAskId[16] = "";
bool    askDismissed  = false;
uint8_t askCursor     = 0;

// Transient toast, drawn last over whatever is on screen.
static char     toastMsg[24] = "";
static uint32_t toastUntil   = 0;
static void showToast(const char* s, uint32_t ms = 1800) {
  strncpy(toastMsg, s, sizeof(toastMsg) - 1);
  toastMsg[sizeof(toastMsg) - 1] = 0;
  toastUntil = millis() + ms;
}

// Is the ask overlay the active bottom-area surface right now? Any pending
// prompt (even one already answered and showing "sent...") outranks it,
// matching drawHUD's dispatch order.
static bool askShowing() {
  return tama.qAskId[0] && !askDismissed && displayMode == DISP_NORMAL &&
         !menuOpen && !settingsOpen && !resetOpen && !hostsOpen && !forgetOpen &&
         !extrasOpen && !tama.promptId[0];
}

static void applySetting(uint8_t idx) {
  Settings& s = settings();
#ifdef BUDDY_EXTRAS_FULL
  if (idx == 2) {   // "tunes" row, FULL builds only
    s.tune = !s.tune;
    settingsSave();
    if (s.tune) tunesJingle(SLOT_APPROVE);   // preview
    return;
  }
  if (idx > 2) idx--;   // map back to the base table for the switch below
#endif
  switch (idx) {
    case 0:
      settings().bright = (settings().bright + 1) % 5;
      applyBrightness();
      settingsSave();
      return;
    case 1:
      s.vol = (s.vol + 1) % 5;   // 0=mute .. 4=max
      applyVolume();
      settingsSave();
      if (s.vol) board::beep(1800, 60);   // preview the new level
      return;
    case 2:
      // Pairing mode is baked into the BLE stack at bleInit — takes effect
      // on the next boot. Existing bonds stay valid either way.
      s.pair = s.pair ? 0 : 1;
      settingsSave();
      showToast("reboot to apply");
      return;
    case 3: {
      // Host mode shortcut: cycle auto -> host 0 -> ... -> host n-1 -> auto.
      int8_t next = s.hostsel + 1;
      if (next >= (int8_t)hostGlueCount()) next = -1;
      s.hostsel = next;
      settingsSave();
      hostGlueSetPin(next);
      showToast(next < 0 ? "host: auto" : hostGlueGet(next)->name);
      return;
    }
    case 4:
      // BT toggle is a stored preference only — BLE stays live. Turning
      // BLE off cleanly would require tearing down the BLE stack which
      // the Arduino BLE library doesn't do reliably. If we need a
      // hard-off someday, stop advertising via BLEDevice::getAdvertising().
      s.bt = !s.bt;
      break;
    case 5: s.wifi = !s.wifi; break;   // stored only — no WiFi stack linked
    case 6: s.led = !s.led; break;
    case 7: s.hud = !s.hud; break;
    case 8: s.clockRot = (s.clockRot + 1) % 3; break;
    case 9: nextPet(); return;
    case 10: resetOpen = true; resetSel = 0; resetConfirmIdx = 0xFF; return;
    case 11: settingsOpen = false; characterInvalidate(); return;
  }
  settingsSave();
}

// Tap-twice confirm: first tap arms (label flips to "really?"), second
// within 3s executes. Scrolling away clears the arm.
static void applyReset(uint8_t idx) {
  uint32_t now = millis();
  bool armed = (resetConfirmIdx == idx) && (int32_t)(now - resetConfirmUntil) < 0;

  if (idx == 3) { resetOpen = false; return; }

  if (!armed) {
    resetConfirmIdx = idx;
    resetConfirmUntil = now + 3000;
    beep(1400, 60);
    return;
  }

  beep(800, 200);
  if (idx == 1) {
    // forget all hosts: BLE bonds + host registry ("hosts" namespace),
    // back to auto mode. Live operation — no reboot needed; the device
    // just re-advertises for a fresh pairing.
    hostGlueForgetAll();
    settings().hostsel = -1;
    settingsSave();
    resetOpen = false;
    resetConfirmIdx = 0xFF;
    showToast("hosts forgotten");
    return;
  }
  if (idx == 0) {
    // delete char: wipe /characters/, reboot into ASCII mode
    File d = LittleFS.open("/characters");
    if (d && d.isDirectory()) {
      File e;
      while ((e = d.openNextFile())) {
        char path[80];
        snprintf(path, sizeof(path), "/characters/%s", e.name());
        if (e.isDirectory()) {
          File f;
          while ((f = e.openNextFile())) {
            char fp[128];
            snprintf(fp, sizeof(fp), "%s/%s", path, f.name());
            f.close();
            LittleFS.remove(fp);
          }
          e.close();
          LittleFS.rmdir(path);
        } else {
          e.close();
          LittleFS.remove(path);
        }
      }
      d.close();
    }
  } else {
    // factory reset: NVS namespace wipes + filesystem format + BLE bonds.
    // Clears stats, owner, petname, species, settings, the host registry,
    // WiFi networks (LittleFS.format() takes /wifi.dat; wifiCredsWipe()
    // sheds any legacy NVS pair), GIF characters, and any stored LTKs so
    // the next desktop has to re-pair. (Forget-host deliberately does NOT
    // touch the WiFi list — hosts and networks are unrelated concerns;
    // only the full privacy wipe takes them.)
    _prefs.begin("buddy", false);
    _prefs.clear();
    _prefs.end();
    _prefs.begin("hosts", false);
    _prefs.clear();
    _prefs.end();
    wifiCredsWipe();
    LittleFS.format();
    bleClearBonds();
  }
  delay(300);
  ESP.restart();
}

// Footer hint row inside a menu panel: "<downLbl> ↓  <rightLbl> →" with
// pixel triangles. Panels add MENU_HINT_H to height and call this at bottom.
const int MENU_HINT_H = 14;
static void drawMenuHints(const Palette& p, int mx, int mw, int hy,
                          const char* downLbl = "A", const char* rightLbl = "B") {
  spr.drawFastHLine(mx + 6, hy - 4, mw - 12, p.textDim);
  spr.setTextColor(p.textDim, PANEL);
  // 6px/glyph at size 1; triangle goes 4px after the label ends
  int x = mx + 8;
  spr.setCursor(x, hy); spr.print(downLbl);
  x += strlen(downLbl) * 6 + 4;
  spr.fillTriangle(x, hy + 1, x + 6, hy + 1, x + 3, hy + 6, p.textDim);
  x = mx + mw / 2 + 4;
  spr.setCursor(x, hy); spr.print(rightLbl);
  x += strlen(rightLbl) * 6 + 4;
  spr.fillTriangle(x, hy, x, hy + 6, x + 5, hy + 3, p.textDim);
}

static void drawSettings() {
  const Palette& p = characterPalette();
  int mw = 118, mh = 16 + SETTINGS_N * 14 + MENU_HINT_H;
  int mx = (W - mw) / 2, my = (H - mh) / 2;
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, p.textDim);
  spr.setTextSize(1);
  Settings& s = settings();
  bool vals[] = { s.bt, s.wifi, s.led, s.hud };
  for (int i = 0; i < SETTINGS_N; i++) {
    bool sel = (i == settingsSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 8 + i * 14);
    spr.print(sel ? "> " : "  ");
    spr.print(settingsItems[i]);
    spr.setCursor(mx + mw - 36, my + 8 + i * 14);
    spr.setTextColor(p.textDim, PANEL);
    int vi = i;   // base-table index (see applySetting's remap)
#ifdef BUDDY_EXTRAS_FULL
    if (i == 2) {
      spr.setTextColor(s.tune ? GREEN : p.textDim, PANEL);
      spr.print(s.tune ? " on" : "off");
      continue;
    }
    if (i > 2) vi = i - 1;
#endif
    if (vi == 0) {
      spr.printf("%u/4", settings().bright);
    } else if (vi == 1) {
      if (s.vol == 0) spr.print("mut");
      else spr.printf("%u/4", s.vol);
    } else if (vi == 2) {
      spr.print(s.pair ? " jw" : " pk");   // just-works / passkey
    } else if (vi == 3) {
      if (s.hostsel < 0) spr.print("auto");
      else spr.printf("%.5s", hostGlueGet((uint8_t)s.hostsel)
                                ? hostGlueGet((uint8_t)s.hostsel)->name : "?");
    } else if (vi >= 4 && vi <= 7) {
      spr.setTextColor(vals[vi-4] ? GREEN : p.textDim, PANEL);
      spr.print(vals[vi-4] ? " on" : "off");
    } else if (vi == 8) {
      static const char* const RN[] = { "auto", "port", "land" };
      spr.print(RN[s.clockRot]);
    } else if (vi == 9) {
      uint8_t total = buddySpeciesCount() + (gifAvailable ? 1 : 0);
      uint8_t pos   = buddyMode ? buddySpeciesIdx() + 1 : total;
      spr.printf("%u/%u", pos, total);
    }
  }
  drawMenuHints(p, mx, mw, my + mh - 12, "Next", "Change");
}

static void drawReset() {
  const Palette& p = characterPalette();
  int mw = 118, mh = 16 + RESET_N * 14 + MENU_HINT_H;
  int mx = (W - mw) / 2, my = (H - mh) / 2;
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, HOT);
  spr.setTextSize(1);
  for (int i = 0; i < RESET_N; i++) {
    bool sel = (i == resetSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 8 + i * 14);
    spr.print(sel ? "> " : "  ");
    bool armed = (i == resetConfirmIdx) &&
                 (int32_t)(millis() - resetConfirmUntil) < 0;
    if (armed) spr.setTextColor(HOT, PANEL);
    spr.print(armed ? "really?" : resetItems[i]);
  }
  drawMenuHints(p, mx, mw, my + mh - 12);
}

#ifdef BUDDY_OTA
// OTA progress screen. The whole flow is blocking (otaRun), so this
// callback is the only thing painting; the full-sprite redraw + single
// pushSprite per change is inherently flicker-free (the oh001738 fix, but
// on a sprite instead of per-glyph LCD writes), and the dedupe of repeat
// (status,pct) pairs happens in ota.cpp.
static void otaDrawProgress(const char* status, int pct) {
  const Palette& p = characterPalette();
  spr.fillSprite(p.bg);
  spr.setTextSize(1);
  spr.setTextColor(p.body, p.bg);
  spr.setCursor(8, 56);  spr.print("FIRMWARE UPDATE");
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(8, 96);  spr.printf("%.20s", status);
  const int BX = 8, BW = W - 16, BH = 10, BY = 120;
  spr.drawRect(BX, BY, BW, BH, p.textDim);
  if (pct >= 0) {
    int fill = (int)((int32_t)(BW - 2) * pct / 100);
    if (fill > 0) spr.fillRect(BX + 1, BY + 1, fill, BH - 2, p.body);
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(8, 138); spr.printf("%d%%", pct);
  }
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(8, 184);  spr.print("keep power on");
  spr.setCursor(8, 194);  spr.print("ble stays paired");
  spr.pushSprite(0, 0);
}

// menu > update... — the consent step. Blocks until flashed (device
// reboots inside otaRun) or failed/up-to-date, which lands back here.
static void otaStart() {
  char err[64];
  otaRun(otaDrawProgress, err, sizeof(err));
  // Only failure paths return. Leave the message up long enough to read,
  // then hand the screen back to the normal render loop.
  otaDrawProgress(err[0] ? err : "update failed", -1);
  delay(2500);
  showToast(err[0] ? err : "update failed", 2600);
  applyDisplayMode();
  characterInvalidate();
  if (buddyMode) buddyInvalidate();
}
#endif

void menuConfirm() {
  switch (menuSel) {
    case 0: hostsOpen = true; menuOpen = false; hostsSel = 0; break;
    case 1: settingsOpen = true; menuOpen = false; settingsSel = 0; break;
    case 2: extrasOpen = true; menuOpen = false; extrasSel = 0; break;
    case 3: board::powerOff(); break;
    case 4:
    case 5:
      menuOpen = false;
      displayMode = DISP_INFO;
      infoPage = (menuSel == 4) ? INFO_PG_BUTTONS : INFO_PG_CREDITS;
      applyDisplayMode();
      characterInvalidate();
      break;
    case 6: dataSetDemo(!dataDemo()); break;
#ifdef BUDDY_OTA
    case 7: menuOpen = false; otaStart(); break;
    case 8: menuOpen = false; characterInvalidate(); break;
#else
    case 7: menuOpen = false; characterInvalidate(); break;
#endif
  }
}

void drawMenu() {
  const Palette& p = characterPalette();
  int mw = 118, mh = 16 + MENU_N * 14 + MENU_HINT_H;
  int mx = (W - mw) / 2, my = (H - mh) / 2;
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, p.textDim);
  spr.setTextSize(1);
  for (int i = 0; i < MENU_N; i++) {
    bool sel = (i == menuSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 8 + i * 14);
    spr.print(sel ? "> " : "  ");
    spr.print(menuItems[i]);
    if (i == 6) spr.print(dataDemo() ? "  on" : "  off");
  }
  drawMenuHints(p, mx, mw, my + mh - 12);
}

// extras submenu: same panel pattern as the main menu.
static void drawExtras() {
  const Palette& p = characterPalette();
  int mw = 118, mh = 16 + EXTRAS_N * 14 + MENU_HINT_H;
  int mx = (W - mw) / 2, my = (H - mh) / 2;
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, p.textDim);
  spr.setTextSize(1);
  for (int i = 0; i < EXTRAS_N; i++) {
    bool sel = (i == extrasSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 8 + i * 14);
    spr.print(sel ? "> " : "  ");
    spr.print(extrasItems[i]);
  }
  drawMenuHints(p, mx, mw, my + mh - 12, "Next", "Select");
}

static void extrasConfirm() {
  uint8_t pages = EXTRAS_N - 1;   // every row but "back" opens a page
  if (extrasSel < pages) {
    extrasOpen = false;
    extrasPage = extrasSel;
    displayMode = DISP_EXTRAS;
    applyDisplayMode();
  } else {   // back
    extrasOpen = false;
    characterInvalidate();
  }
}

// Hosts panel: one row per registry entry (* = connected, > = pinned), then
// auto / add host / forget / back. Row count is dynamic but bounded:
// BUDDY_MAX_HOSTS + 4 = 11 rows fits the 240px panel budget.
static uint8_t hostsRowCount() { return hostGlueCount() + 4; }

static void drawHosts() {
  const Palette& p = characterPalette();
  uint8_t n = hostGlueCount();
  uint8_t rows = hostsRowCount();
  int mw = 118, mh = 16 + rows * 14 + MENU_HINT_H;
  int mx = (W - mw) / 2, my = (H - mh) / 2;
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, p.textDim);
  spr.setTextSize(1);
  int connSlot = hostGlueConnectedSlot();
  int8_t pin = hostGluePin();
  for (uint8_t i = 0; i < rows; i++) {
    bool sel = (i == hostsSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 8 + i * 14);
    spr.print(sel ? "> " : "  ");
    if (i < n) {
      const HostRec* r = hostGlueGet(i);
      char row[20];
      snprintf(row, sizeof(row), "%.13s%s%s", r->name,
               (int)i == connSlot ? "*" : "",
               (int8_t)i == pin ? ">" : "");
      spr.print(row);
    } else if (i == n) {
      spr.print("auto");
      if (pin < 0) spr.print(" >");
    } else if (i == n + 1) {
      uint32_t left = blePairingRemainingMs();
      if (left) {
        spr.setTextColor(GREEN, PANEL);
        spr.printf("pairing %lus", (unsigned long)((left + 999) / 1000));
      } else {
        spr.print("add host...");
      }
    } else if (i == n + 2) {
      spr.print("forget...");
    } else {
      spr.print("back");
    }
  }
  drawMenuHints(p, mx, mw, my + mh - 12, "Next", "Select");
}

static void drawForget() {
  const Palette& p = characterPalette();
  uint8_t n = hostGlueCount();
  uint8_t rows = n + 1;   // hosts + back
  int mw = 118, mh = 16 + rows * 14 + MENU_HINT_H;
  int mx = (W - mw) / 2, my = (H - mh) / 2;
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, HOT);
  spr.setTextSize(1);
  for (uint8_t i = 0; i < rows; i++) {
    bool sel = (i == forgetSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 8 + i * 14);
    spr.print(sel ? "> " : "  ");
    if (i < n) spr.printf("%.14s", hostGlueGet(i)->name);
    else spr.print("back");
  }
  drawMenuHints(p, mx, mw, my + mh - 12, "Next", "Forget");
}

static void hostsConfirm() {
  uint8_t n = hostGlueCount();
  if (hostsSel < n) {
    // Pin this host. Selecting it again keeps the pin (use "auto" to clear).
    settings().hostsel = (int8_t)hostsSel;
    settingsSave();
    hostGlueSetPin((int8_t)hostsSel);
    showToast(hostGlueGet(hostsSel)->name);
  } else if (hostsSel == n) {
    settings().hostsel = -1;
    settingsSave();
    hostGlueSetPin(-1);
    showToast("host: auto");
  } else if (hostsSel == n + 1) {
    // add host: toggle the 60s pairing window. Opening it evicts the
    // currently connected host and rejects bonded peers for the duration —
    // the window exclusively courts the NEW machine (see ble_bridge.h).
    // The passkey screen takes over automatically once a peer starts
    // pairing (blePasskey() path); a successful pairing ends the window
    // early (the countdown row above flips back to "add host...") and
    // toasts "paired: <name>" from loop().
    bool open = blePairingRemainingMs() > 0;
    hostGluePairWindow(!open);
    if (!open) showToast("pairing open 60s");
  } else if (hostsSel == n + 2) {
    if (n > 0) { forgetOpen = true; forgetSel = 0; }
    else showToast("no hosts");
  } else {
    hostsOpen = false;
    characterInvalidate();
  }
}

static void forgetConfirm() {
  uint8_t n = hostGlueCount();
  if (forgetSel < n) {
    hostGlueForget(forgetSel);
    // Forgetting shifts slots; mirror the glue's remapped pin into settings.
    settings().hostsel = hostGluePin();
    settingsSave();
    showToast("forgotten");
    if (hostGlueCount() == 0) forgetOpen = false;
    else if (forgetSel > hostGlueCount()) forgetSel = hostGlueCount();
  } else {
    forgetOpen = false;
  }
}

// Bottom-center toast, drawn last so it floats over every surface.
static void drawToast() {
  if (!toastMsg[0]) return;
  if ((int32_t)(millis() - toastUntil) >= 0) { toastMsg[0] = 0; return; }
  const Palette& p = characterPalette();
  int tw = (int)strlen(toastMsg) * 6 + 12;
  int tx = (W - tw) / 2;
  int ty = H - 56;
  spr.fillRoundRect(tx, ty, tw, 14, 3, PANEL);
  spr.drawRoundRect(tx, ty, tw, 14, 3, p.textDim);
  spr.setTextSize(1);
  spr.setTextColor(p.text, PANEL);
  spr.setCursor(tx + 6, ty + 4);
  spr.print(toastMsg);
}

// Clock orientation: gravity along the in-plane X axis means the stick is
// on its side. Signed counter for hysteresis on both transitions — same
// pattern as face-down nap.
//   0 = portrait (sprite path, pet sleeps underneath)
//   1 = landscape, BtnA-side down (M5.Lcd rotation 1)
//   3 = landscape, USB-side down (M5.Lcd rotation 3)
static uint8_t clockOrient     = 0;
static int8_t  orientFrames    = 0;
static int8_t  clockSwapFrames = 0;
static uint8_t paintedOrient   = 0;
// RTC and IMU share an I2C bus. Reading the RTC at 60fps starves the IMU
// reads in clockUpdateOrient — orientation detection gets noisy. Cache the
// time once per second; mood logic and drawClock both read from here.
// (On the S3 the "RTC" is the software clock and the read is free, but the
// 1Hz cadence keeps behavior identical across boards.)
static struct tm _clk        = {};
uint32_t         _clkLastRead = 0;   // zeroed by data.h on time-sync
static bool      _onUsb      = false;
static void clockRefreshRtc() {
  if (millis() - _clkLastRead < 1000) return;
  _clkLastRead = millis();
  _onUsb = board::onUsb();
  struct tm lt;
  if (board::getClock(&lt)) _clk = lt;
}

static void clockUpdateOrient() {
  float ax, ay, az;
  board::imuAccel(&ax, &ay, &az);
  // State machine extracted to logic/clock_orient_logic.h (unit-tested
  // natively); ax is the in-plane long axis on every supported board.
  clockOrientUpdate(&clockOrient, &orientFrames, &clockSwapFrames,
                    ax, ay, az, settings().clockRot);
}

// Clock face: shown when charging on USB with nothing else going on, and
// as the carousel DISP_CLOCK screen (withStrip adds the host/session row).
// Portrait paints the lower area of the sprite; pet renders above.
// Landscape draws direct to LCD with rotation — sprite stays untouched.
// Month/weekday labels + placeholder-hardened formatting live in
// logic/clock_display_logic.h.

static uint8_t clockDow() { return (uint8_t)_clk.tm_wday % 7; }

// Host/session status strip: connected host's name (or "no host") on the
// left, one pip per session on the right — wait pips hot, running green,
// idle/done dim outlines. Same vocabulary as the HUD badge line. Draws to
// either surface (portrait sprite / landscape LCD) via the TFT_eSPI base.
static void drawClockStrip(TFT_eSPI* g, int y) {
  const Palette& p = characterPalette();
  g->setTextSize(1);
  g->setCursor(4, y);
  g->setTextColor(p.textDim, p.bg);
  if (!tama.connected) {
    g->print("no host");
  } else {
    const char* hn = hostGlueActiveName();
    char row[16];
    snprintf(row, sizeof(row), "%.13s", hn[0] ? hn : "host");
    g->print(row);
  }
  int wpx = g->width();
  for (uint8_t i = 0; i < _sessions.count; i++) {
    uint16_t c = _sessions.s[i].state == SES_WAIT ? HOT
               : _sessions.s[i].state == SES_RUN  ? GREEN : p.textDim;
    int px = wpx - 8 - (int)(_sessions.count - 1 - i) * 7;
    if (_sessions.s[i].state == SES_RUN || _sessions.s[i].state == SES_WAIT)
      g->fillCircle(px, y + 3, 2, c);
    else
      g->drawCircle(px, y + 3, 2, c);
  }
}

static void drawClock(bool withStrip = false) {
  const Palette& p = characterPalette();
  char hm[8]; clockFormatHm(hm, sizeof(hm), _clk.tm_hour, _clk.tm_min);
  char ss[8]; clockFormatSeconds(ss, sizeof(ss), _clk.tm_sec);
  char dl[12]; clockFormatDateLine(dl, sizeof(dl), _clk.tm_mon + 1, _clk.tm_mday);

  if (clockOrient == 0) {
    paintedOrient = 0;
    // Bottom half — buddy naturally lives at y=0..82, GIF peeks at top
    // via peek mode. Clearing from 90 leaves both untouched.
    spr.fillRect(0, 90, W, H - 90, p.bg);
    spr.setTextDatum(MC_DATUM);
    spr.setTextSize(4); spr.setTextColor(p.text, p.bg);    spr.drawString(hm, CX, 140);
    spr.setTextSize(2); spr.setTextColor(p.textDim, p.bg); spr.drawString(ss, CX, 175);
    spr.setTextSize(1);                                     spr.drawString(dl, CX, 200);
    spr.setTextDatum(TL_DATUM);
    if (withStrip) drawClockStrip(&spr, H - 14);
    return;
  }

  // Landscape: 240×135 direct-to-LCD. Full fill only on entry; after that
  // text glyph bg cells repaint themselves and the pet box (small, ~90×50)
  // gets a fillRect each pet tick — small enough not to tear.
  M5.Lcd.setRotation(clockOrient);
  static uint8_t lastSec = 0xFF;
  bool repaint = paintedOrient != clockOrient;
  if (repaint) { M5.Lcd.fillScreen(p.bg); paintedOrient = clockOrient; lastSec = 0xFF; }

  // Seconds tick at 1Hz; redrawing 3 strings at 60fps is 180 SPI ops/sec
  // for nothing. Gate on the second changing (or full repaint).
  if (repaint || (uint8_t)_clk.tm_sec != lastSec) {
    lastSec = (uint8_t)_clk.tm_sec;
    char wdl[14];
    clockFormatWeekDateLine(wdl, sizeof(wdl), clockDow(), _clk.tm_mon + 1, _clk.tm_mday);
    // bare seconds, no colon — historic landscape layout
    char ssl[4]; snprintf(ssl, sizeof(ssl), "%.2s", ss + 1);
    M5.Lcd.setTextDatum(MC_DATUM);
    M5.Lcd.setTextSize(3); M5.Lcd.setTextColor(p.text, p.bg);    M5.Lcd.drawString(hm, 170, 42);
    M5.Lcd.setTextSize(2); M5.Lcd.setTextColor(p.textDim, p.bg); M5.Lcd.drawString(ssl, 170, 72);
                                                                  M5.Lcd.drawString(wdl, 170, 102);
    M5.Lcd.setTextDatum(TL_DATUM);
    M5.Lcd.setTextSize(1);
    if (withStrip) {
      // Pips don't self-clear when a session drops off — wipe the row.
      // 1Hz + 12px tall: no visible tear.
      M5.Lcd.fillRect(0, 121, M5.Lcd.width(), 12, p.bg);
      drawClockStrip(&M5.Lcd, 123);
    }
  }

  // Pet on left at 5 fps. Clear includes the overlay-particle zone above
  // the body (y<30) — species draw Zzz/hearts there via BUDDY_Y_OVERLAY=6
  // which doesn't go through _yb, so the box has to cover it.
  static uint32_t lastPetTick = 0;
  if (millis() - lastPetTick >= 200) {
    lastPetTick = millis();
    if (buddyMode) {
      // ASCII glyphs don't self-clear; wipe the box each tick. Species
      // hardcode BUDDY_X_CENTER=67 / BUDDY_Y_OVERLAY=6 for particles so
      // keep portrait coords and just swap the surface — pet lands
      // upper-left of landscape, which is where we want it anyway.
      M5.Lcd.fillRect(0, 0, 115, 90, p.bg);
      buddyRenderTo(&M5.Lcd, activeState);
    } else {
      // Full-frame GIFs paint every pixel (transparent → pal.bg), so a
      // per-tick clear just adds a visible black flash between wipe and
      // last scanline. The entry fillScreen on paintedOrient change
      // already covers the surround.
      characterSetState(activeState);
      characterRenderTo(&M5.Lcd, 57, 45);
    }
  }
  M5.Lcd.setRotation(0);
}

// DISP_CLOCK portrait: carousel clock screen. Header row like PET/INFO
// (title left, weekday right), big HH:MM, seconds + date, host/session
// strip pinned at the bottom. Landscape reuses drawClock's face (identical
// layout) with the strip enabled — the loop routes there directly.
static void drawClockScreen() {
  const Palette& p = characterPalette();
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, TOP + 2); spr.print("Clock");
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 22, TOP + 2); spr.print(clockWeekdayLabel(clockDow()));

  char hm[8]; clockFormatHm(hm, sizeof(hm), _clk.tm_hour, _clk.tm_min);
  char ss[8]; clockFormatSeconds(ss, sizeof(ss), _clk.tm_sec);
  char dl[12]; clockFormatDateLine(dl, sizeof(dl), _clk.tm_mon + 1, _clk.tm_mday);
  spr.setTextDatum(MC_DATUM);
  spr.setTextSize(4); spr.setTextColor(p.text, p.bg);    spr.drawString(hm, CX, 132);
  spr.setTextSize(2); spr.setTextColor(p.textDim, p.bg); spr.drawString(ss, CX, 166);
  spr.setTextSize(1);                                     spr.drawString(dl, CX, 192);
  spr.setTextDatum(TL_DATUM);
  drawClockStrip(&spr, H - 14);
}

// DISP_EXTRAS page 0 — pomodoro. A = start/pause/resume, B = skip phase,
// B-long = reset to idle, A-long = leave the screen. Big mm:ss remaining,
// progress bar, one dot per completed focus of the 4-block cycle.
static const char* const POMO_PHASE_NAME[] = { "idle", "focus", "break", "long break" };
static void drawPomodoro() {
  const Palette& p = characterPalette();
  const int TOP = 70;
  uint32_t now = millis();
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, TOP + 2); spr.print("Extras");
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 52, TOP + 2); spr.print("pomodoro");

  int y = TOP + 22;
  uint16_t phCol = pomo.phase == POMO_FOCUS ? HOT
                 : pomo.phase == POMO_IDLE  ? p.textDim : GREEN;
  spr.setTextSize(2);
  spr.setTextColor(phCol, p.bg);
  spr.setCursor(6, y);
  spr.print(POMO_PHASE_NAME[pomo.phase]);
  if (pomo.paused) {
    spr.setTextSize(1);
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(W - 40, y + 4);
    spr.print("paused");
  }
  y += 26;

  // Remaining mm:ss. Idle previews the focus block a press of A buys.
  uint32_t r = (pomo.phase == POMO_IDLE ? POMO_FOCUS_MS
                                        : pomoRemainMs(pomo, now)) / 1000;
  char rem[8];
  snprintf(rem, sizeof(rem), "%02lu:%02lu",
           (unsigned long)(r / 60), (unsigned long)(r % 60));
  spr.setTextDatum(MC_DATUM);
  spr.setTextSize(4);
  spr.setTextColor(pomo.phase == POMO_IDLE ? p.textDim : p.text, p.bg);
  spr.drawString(rem, CX, y + 16);
  spr.setTextDatum(TL_DATUM);
  spr.setTextSize(1);
  y += 42;

  const int BX = 8, BW = W - 16, BH = 8;
  spr.drawRect(BX, y, BW, BH, p.textDim);
  if (pomo.phase != POMO_IDLE) {
    int fill = (int)((uint64_t)(BW - 2) * pomoElapsedMs(pomo, now)
                     / pomoPhaseDurMs(pomo.phase));
    if (fill > 0) spr.fillRect(BX + 1, y + 1, fill, BH - 2, phCol);
  }
  y += 18;

  for (uint8_t i = 0; i < POMO_CYCLE_LEN; i++) {
    int px = CX - 21 + i * 14;
    if (i < pomo.focusDone)
      spr.fillCircle(px, y + 3, 3, p.body);
    else if (i == pomo.focusDone && pomo.phase == POMO_FOCUS)
      spr.fillCircle(px, y + 3, 3, phCol);
    else
      spr.drawCircle(px, y + 3, 3, p.textDim);
  }

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(4, H - 10);
  spr.print(pomo.phase == POMO_IDLE ? "A:start  hold-A:exit"
          : pomo.paused             ? "A:resume B:skip"
                                    : "A:pause B:skip");
}

#ifdef BUDDY_EXTRAS_FULL
// DISP_EXTRAS page 1 — IR remote (FULL builds only). Four learned-code
// slots: A = next slot, B = blast the slot, B-long = learn (captures the
// next burst from a real remote), A-long = leave. Codes are raw timings —
// no protocol decoding, so any 38 kHz remote works.
static void drawIrPage() {
  const Palette& p = characterPalette();
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, TOP + 2); spr.print("Extras");
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 58, TOP + 2); spr.print("ir remote");

  int y = TOP + 20;
  for (uint8_t i = 0; i < IR_SLOTS; i++) {
    bool sel = i == irSel;
    spr.setTextColor(sel ? p.text : p.textDim, p.bg);
    spr.setCursor(6, y);
    spr.printf("%s slot %u", sel ? ">" : " ", i + 1);
    spr.setCursor(66, y);
    if (irLearning() && irLearnSlot() == (int8_t)i) {
      spr.setTextColor(HOT, p.bg);
      spr.print("listening");
    } else {
      uint16_t n = irSlotEdges(i);
      if (n) { spr.setTextColor(GREEN, p.bg); spr.printf("%u edges", n); }
      else spr.print("empty");
    }
    y += 14;
  }
  y += 8;
  spr.setTextColor(p.textDim, p.bg);
  if (irLearning()) {
    uint32_t leftMs = (int32_t)(irLearnDeadline - millis()) > 0
                    ? irLearnDeadline - millis() : 0;
    spr.setCursor(6, y);      spr.print("point remote at rx,");
    spr.setCursor(6, y + 10); spr.printf("press a key... %lus",
                                         (unsigned long)((leftMs + 999) / 1000));
  } else {
    spr.setCursor(6, y);      spr.print("hold B: learn slot");
    spr.setCursor(6, y + 10); spr.printf("tx G%d  rx G%d", IR_TX_GPIO, IR_RX_GPIO);
  }
  spr.setCursor(4, H - 10);
  spr.print("A:slot  B:send");
}
#endif

// DISP_EXTRAS router: page 0 pomodoro everywhere; page 1 IR on FULL builds.
static void drawExtrasScreen() {
#ifdef BUDDY_EXTRAS_FULL
  if (extrasPage == 1) { drawIrPage(); return; }
#endif
  drawPomodoro();
}

// Persona base-state mapping extracted to logic/persona_logic.h; pack the
// bridge fields it consumes and let the pure header decide.
static PersonaState derive(const TamaState& s) {
  PersonaInputs in = { s.connected, s.sessionsRunning, s.sessionsWaiting, s.recentlyCompleted };
  return derivePersona(in);
}

void triggerOneShot(PersonaState s, uint32_t durMs) {
  activeState = s;
  oneShotUntil = millis() + durMs;
}

bool checkShake() {
  float ax, ay, az;
  board::imuAccel(&ax, &ay, &az);
  float mag = sqrtf(ax*ax + ay*ay + az*az);
  float delta = fabsf(mag - accelBaseline);
  accelBaseline = accelBaseline * 0.95f + mag * 0.05f;
  return delta > 0.8f;
}




// Persistent screen-level title row ("INFO  n/3") matching the PET header,
// then a per-page section label below it. The fixed title is the cue that
// B cycles pages here just like it does on PET.
static void _infoHeader(const Palette& p, int& y, const char* section, uint8_t page) {
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, y); spr.print("Info");
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 28, y); spr.printf("%u/%u", page + 1, INFO_PAGES);
  y += 12;
  spr.setTextColor(p.body, p.bg);
  spr.setCursor(4, y); spr.print(section);
  y += 12;
}

void drawPasskey() {
  const Palette& p = characterPalette();
  spr.fillSprite(p.bg);
  spr.setTextSize(1);
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(8, 56);  spr.print("BLUETOOTH PAIRING");
  spr.setCursor(8, 184); spr.print("enter on desktop:");
  spr.setTextSize(3);
  spr.setTextColor(p.text, p.bg);
  char b[8]; snprintf(b, sizeof(b), "%06lu", (unsigned long)blePasskey());
  spr.setCursor((W - 18 * 6) / 2, 110);
  spr.print(b);
}

void drawInfo() {
  const Palette& p = characterPalette();
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  int y = TOP + 2;
  auto ln = [&](const char* fmt, ...) {
    char b[32]; va_list a; va_start(a, fmt); vsnprintf(b, sizeof(b), fmt, a); va_end(a);
    spr.setCursor(4, y); spr.print(b); y += 8;
  };

  if (infoPage == 0) {
    _infoHeader(p, y, "ABOUT", infoPage);
    spr.setTextColor(p.textDim, p.bg);
    ln("I watch your Claude");
    ln("desktop sessions.");
    y += 6;
    ln("I sleep when nothing's");
    ln("happening, wake when");
    ln("you start working,");
    ln("get impatient when");
    ln("approvals pile up.");
    y += 6;
    spr.setTextColor(p.text, p.bg);
    ln("Press A on a prompt");
    ln("to approve from here.");
    y += 6;
    spr.setTextColor(p.textDim, p.bg);
    ln("18 species. Settings");
    ln("> ascii pet to cycle.");
    y += 6;
    ln("fw %s", BUDDY_FW_VERSION);
    ln("protocol v%d", BUDDY_PROTO_VER);

  } else if (infoPage == 1) {
    _infoHeader(p, y, "BUTTONS", infoPage);
    spr.setTextColor(p.text, p.bg);    ln("A   front");
    spr.setTextColor(p.textDim, p.bg); ln("    next screen");
    ln("    approve prompt"); y += 4;
    spr.setTextColor(p.text, p.bg);    ln("B   right side");
    spr.setTextColor(p.textDim, p.bg); ln("    next page");
    ln("    deny prompt"); y += 4;
    spr.setTextColor(p.text, p.bg);    ln("hold A");
    spr.setTextColor(p.textDim, p.bg); ln("    menu"); y += 4;
    spr.setTextColor(p.text, p.bg);    ln("Power  left side");
    spr.setTextColor(p.textDim, p.bg); ln("    tap = screen off");
#if defined(BOARD_STICKS3)
    ln("    2-tap = power off");   // long hold is download mode on the S3
#else
    ln("    hold 6s = off");
#endif

  } else if (infoPage == 2) {
    _infoHeader(p, y, "CLAUDE", infoPage);
    spr.setTextColor(p.textDim, p.bg);
    ln("  sessions  %u", tama.sessionsTotal);
    ln("  running   %u", tama.sessionsRunning);
    ln("  waiting   %u", tama.sessionsWaiting);
    y += 8;
    spr.setTextColor(p.text, p.bg);
    ln("LINK");
    spr.setTextColor(p.textDim, p.bg);
    ln("  via       %s", dataScenarioName());
    ln("  ble       %s", !bleConnected() ? "-" : bleSecure() ? "encrypted" : "OPEN");
    uint32_t age = (millis() - tama.lastUpdated) / 1000;
    ln("  last msg  %lus", (unsigned long)age);
    ln("  state     %s", stateNames[activeState]);

  } else if (infoPage == 3) {
    _infoHeader(p, y, "DEVICE", infoPage);

    int vBat_mV = board::batteryMilliVolts();
    int iBat_mA = board::batteryCurrentMa();
    int vBus_mV = board::usbMilliVolts();
    int pct = board::batteryPercent();
    bool usb = vBus_mV > 4000;
    // With real current sense (AXP192) keep the historic thresholds; other
    // PMICs report a charge flag instead.
    bool charging = usb && (board::hasBatteryCurrent() ? iBat_mA > 1 : board::isCharging());
    bool full = usb && vBat_mV > 4100 &&
                (board::hasBatteryCurrent() ? iBat_mA < 10 : !board::isCharging());

    spr.setTextColor(p.text, p.bg);
    spr.setTextSize(2);
    spr.setCursor(4, y);
    spr.printf("%d%%", pct);
    spr.setTextSize(1);
    spr.setTextColor(full ? GREEN : (charging ? HOT : p.textDim), p.bg);
    spr.setCursor(60, y + 4);
    spr.print(full ? "full" : (charging ? "charging" : (usb ? "usb" : "battery")));
    y += 20;

    spr.setTextColor(p.textDim, p.bg);
    ln("  battery  %d.%02dV", vBat_mV/1000, (vBat_mV%1000)/10);
    if (board::hasBatteryCurrent()) ln("  current  %+dmA", iBat_mA);
    if (usb) ln("  usb in   %d.%02dV", vBus_mV/1000, (vBus_mV%1000)/10);
    y += 8;

    spr.setTextColor(p.text, p.bg);
    ln("SYSTEM");
    spr.setTextColor(p.textDim, p.bg);
    if (ownerName()[0]) ln("  owner    %s", ownerName());
    uint32_t up = millis() / 1000;
    ln("  uptime   %luh %02lum", up / 3600, (up / 60) % 60);
    ln("  heap     %uKB", ESP.getFreeHeap() / 1024);
    ln("  bright   %u/4", settings().bright);
    ln("  bt       %s", settings().bt ? (dataBtActive() ? "linked" : "on") : "off");
    // count only — names stay off-screen, and a pass never leaves the store
    ln("  wifi     %u nets", (unsigned)wifiStoreCount());
    if (board::hasInternalTemp()) ln("  temp     %dC", board::internalTempC());

  } else if (infoPage == 4) {
    _infoHeader(p, y, "BLUETOOTH", infoPage);
    bool linked = settings().bt && dataBtActive();

    spr.setTextColor(linked ? GREEN : (settings().bt ? HOT : p.textDim), p.bg);
    spr.setTextSize(2);
    spr.setCursor(4, y);
    spr.print(linked ? "linked" : (settings().bt ? "discover" : "off"));
    spr.setTextSize(1);
    y += 20;

    spr.setTextColor(p.textDim, p.bg);
    spr.setTextColor(p.text, p.bg);
    ln("  %s", btName);
    spr.setTextColor(p.textDim, p.bg);
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_BT);
    ln("  %02X:%02X:%02X:%02X:%02X:%02X",
       mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
    y += 8;

    if (linked) {
      uint32_t age = (millis() - tama.lastUpdated) / 1000;
      ln("  last msg  %lus", (unsigned long)age);
    } else if (settings().bt) {
      spr.setTextColor(p.text, p.bg);
      ln("TO PAIR");
      spr.setTextColor(p.textDim, p.bg);
      ln(" Open Claude desktop");
      ln(" > Developer");
      ln(" > Hardware Buddy");
      y += 4;
      ln(" auto-connects via BLE");
    }

  } else if (infoPage == INFO_PG_HOSTS) {
    _infoHeader(p, y, "HOSTS", infoPage);
    uint8_t n = hostGlueCount();
    int connSlot = hostGlueConnectedSlot();
    int8_t pin = hostGluePin();
    if (n == 0) {
      spr.setTextColor(p.textDim, p.bg);
      ln("  none paired");
      y += 4;
      ln("  menu > hosts >");
      ln("  add host...");
    } else {
      for (uint8_t i = 0; i < n; i++) {
        const HostRec* r = hostGlueGet(i);
        spr.setTextColor((int)i == connSlot ? GREEN : p.textDim, p.bg);
        ln("%c %.15s%s", (int)i == connSlot ? '*' : ' ', r->name,
           (int8_t)i == pin ? " >" : "");
      }
    }
    y += 8;
    spr.setTextColor(p.text, p.bg);
    ln("MODE");
    spr.setTextColor(p.textDim, p.bg);
    ln("  select   %s", pin < 0 ? "auto" : "pinned");
    ln("  pairing  %s", settings().pair ? "justworks" : "passkey");
    uint32_t left = blePairingRemainingMs();
    if (left) {
      spr.setTextColor(GREEN, p.bg);
      ln("  window   %lus left", (unsigned long)((left + 999) / 1000));
    }

  } else {
    _infoHeader(p, y, "CREDITS", infoPage);
    spr.setTextColor(p.textDim, p.bg);
    ln("made by");
    y += 4;
    spr.setTextColor(p.text, p.bg);
    ln("Felix Rieseberg");
    y += 12;
    spr.setTextColor(p.textDim, p.bg);
    ln("source");
    y += 4;
    spr.setTextColor(p.text, p.bg);
    ln("github.com/anthropics");
    ln("/claude-desktop-buddy");
    y += 12;
    spr.setTextColor(p.textDim, p.bg);
    ln("hardware");
    y += 4;
    ln("%s", board::name());
#if defined(BOARD_STICKS3)
    ln("ESP32-S3 + BMI270");
#else
    ln("ESP32 + MPU6886");
#endif
  }
}


static void drawApproval() {
  const Palette& p = characterPalette();
  const int AREA = 78;
  spr.fillRect(0, H - AREA, W, AREA, p.bg);
  spr.drawFastHLine(0, H - AREA, W, p.textDim);

  spr.setTextSize(1);
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(4, H - AREA + 4);
  uint32_t waited = (millis() - promptArrivedMs) / 1000;
  if (waited >= 10) spr.setTextColor(HOT, p.bg);
  spr.printf("approve? %lus", (unsigned long)waited);

  // Size 2 only if it fits one line (~10 chars at 12px on 135px screen)
  int toolLen = strlen(tama.promptTool);
  spr.setTextColor(p.text, p.bg);
  spr.setTextSize(toolLen <= 10 ? 2 : 1);
  spr.setCursor(4, H - AREA + (toolLen <= 10 ? 14 : 18));
  spr.print(tama.promptTool);
  spr.setTextSize(1);

  // Hint wraps at ~21 chars to two lines under the tool name
  spr.setTextColor(p.textDim, p.bg);
  int hlen = strlen(tama.promptHint);
  spr.setCursor(4, H - AREA + 34);
  spr.printf("%.21s", tama.promptHint);
  if (hlen > 21) {
    spr.setCursor(4, H - AREA + 42);
    spr.printf("%.21s", tama.promptHint + 21);
  }

  // v2: owning-session badge. With a focus pin active, prompts from other
  // sessions are flagged instead of hidden — approvals are too important to
  // filter away. "+Nq" = more prompts queued behind this one (host caps at 3).
  if (tama.promptSid[0]) {
    const Session* ss = sessionTabBySid(&_sessions, tama.promptSid);
    const char* title = (ss && ss->title[0]) ? ss->title : tama.promptSid;
    bool other = _sessions.pinSid[0] &&
                 strcmp(_sessions.pinSid, tama.promptSid) != 0;
    spr.setTextColor(other ? HOT : p.body, p.bg);
    spr.setCursor(4, H - AREA + 52);
    if (other) spr.printf("[other: %.10s]", title);
    else       spr.printf("[%.16s]", title);
    if (tama.promptQn > 0) {
      char qb[8];
      snprintf(qb, sizeof(qb), "+%uq", tama.promptQn);
      spr.setTextColor(p.textDim, p.bg);
      spr.setCursor(W - (int)strlen(qb) * 6 - 4, H - AREA + 52);
      spr.print(qb);
    }
  }

  if (responseSent) {
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(4, H - 12);
    spr.print("sent...");
  } else {
    spr.setTextColor(GREEN, p.bg);
    spr.setCursor(4, H - 12);
    spr.print("A: approve");
    spr.setTextColor(HOT, p.bg);
    spr.setCursor(W - 48, H - 12);
    spr.print("B: deny");
  }
}

static void tinyHeart(int x, int y, bool filled, uint16_t col) {
  if (filled) {
    spr.fillCircle(x - 2, y, 2, col);
    spr.fillCircle(x + 2, y, 2, col);
    spr.fillTriangle(x - 4, y + 1, x + 4, y + 1, x, y + 5, col);
  } else {
    spr.drawCircle(x - 2, y, 2, col);
    spr.drawCircle(x + 2, y, 2, col);
    spr.drawLine(x - 4, y + 1, x, y + 5, col);
    spr.drawLine(x + 4, y + 1, x, y + 5, col);
  }
}

static void drawPetStats(const Palette& p) {
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  int y = TOP + 16;

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(6, y - 2); spr.print("mood");
  uint8_t mood = statsMoodTier();
  uint16_t moodCol = (mood >= 3) ? RED : (mood >= 2) ? HOT : p.textDim;
  for (int i = 0; i < 4; i++) tinyHeart(54 + i * 16, y + 2, i < mood, moodCol);

  y += 20;
  spr.setCursor(6, y - 2); spr.print("fed");
  uint8_t fed = statsFedProgress();
  for (int i = 0; i < 10; i++) {
    int px = 38 + i * 9;
    if (i < fed) spr.fillCircle(px, y + 1, 2, p.body);
    else spr.drawCircle(px, y + 1, 2, p.textDim);
  }

  y += 20;
  spr.setCursor(6, y - 2); spr.print("energy");
  uint8_t en = statsEnergyTier(_onUsb);
  uint16_t enCol = (en >= 4) ? 0x07FF : (en >= 2) ? 0xFFE0 : HOT;
  for (int i = 0; i < 5; i++) {
    int px = 54 + i * 13;
    if (i < en) spr.fillRect(px, y - 2, 9, 6, enCol);
    else spr.drawRect(px, y - 2, 9, 6, p.textDim);
  }

  y += 24;
  spr.fillRoundRect(6, y - 2, 42, 14, 3, p.body);
  spr.setTextColor(p.bg, p.body);
  spr.setCursor(11, y + 1); spr.printf("Lv %u", stats().level);

  y += 20;
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(6, y);
  spr.printf("approved %u", stats().approvals);
  spr.setCursor(6, y + 10);
  spr.printf("denied   %u", stats().denials);
  uint32_t nap = stats().napSeconds;
  spr.setCursor(6, y + 20);
  spr.printf("napped   %luh%02lum", nap/3600, (nap/60)%60);
  auto tokFmt = [&](const char* label, uint32_t v, int yPx) {
    spr.setCursor(6, yPx);
    if (v >= 1000000)   spr.printf("%s%lu.%luM", label, v/1000000, (v/100000)%10);
    else if (v >= 1000) spr.printf("%s%lu.%luK", label, v/1000, (v/100)%10);
    else                spr.printf("%s%lu", label, v);
  };
  tokFmt("tokens   ", stats().tokens, y + 30);
  tokFmt("today    ", tama.tokensToday, y + 40);
}

// Token-usage page: three bars (context / hourly / weekly), ported from
// bjedwards 1ec45c0 onto the pet screen. The bridge doesn't send real
// usage telemetry yet, so the percentages are the same heuristics the
// original used, derived from the tokens-today counter — swap in bridge
// fields when the host grows them (PROTOCOL_V2 optional-field superset).
static void drawTokenBar(const Palette& p, int y, const char* label,
                         uint32_t used, uint32_t budget) {
  const uint16_t USE_GREEN = 0x07E0, USE_YELLOW = 0xFFE0, USE_RED = 0xF800;
  uint32_t pct = budget ? (uint32_t)((uint64_t)used * 100 / budget) : 0;
  if (pct > 100) pct = 100;
  uint16_t col = pct >= 85 ? USE_RED : pct >= 60 ? USE_YELLOW : USE_GREEN;

  const int BX = 32, BW = 66, BH = 6;
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(6, y);
  spr.print(label);
  spr.drawRect(BX, y - 1, BW, BH + 2, p.textDim);
  int fill = (int)((uint32_t)(BW - 2) * pct / 100);
  if (fill > 0) spr.fillRect(BX + 1, y, fill, BH, col);
  spr.setCursor(BX + BW + 4, y);
  spr.printf("%lu%%", (unsigned long)pct);
}

static void drawPetUsage(const Palette& p) {
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  int y = TOP + 20;

  spr.setTextColor(p.body, p.bg);
  spr.setCursor(6, y); spr.print("TOKEN USAGE");
  y += 16;

  // Heuristic budgets (bjedwards): ~100K/hour, ~1M/week; context proxied
  // by progress through a 200K window. Real telemetry needs bridge fields.
  drawTokenBar(p, y, "ctx", tama.tokensToday % 200000, 200000);  y += 22;
  drawTokenBar(p, y, "hr",  tama.tokensToday,          100000);  y += 22;
  drawTokenBar(p, y, "wk",  tama.tokensToday,         1000000);  y += 28;

  spr.setTextColor(p.textDim, p.bg);
  auto tokFmt = [&](const char* label, uint32_t v, int yPx) {
    spr.setCursor(6, yPx);
    if (v >= 1000000)   spr.printf("%s%lu.%luM", label, v/1000000, (v/100000)%10);
    else if (v >= 1000) spr.printf("%s%lu.%luK", label, v/1000, (v/100)%10);
    else                spr.printf("%s%lu", label, v);
  };
  tokFmt("today    ", tama.tokensToday, y);
  tokFmt("lifetime ", stats().tokens, y + 10);
}

static void drawPetHowTo(const Palette& p) {
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  int y = TOP + 2;
  auto ln = [&](uint16_t c, const char* s) {
    spr.setTextColor(c, p.bg); spr.setCursor(6, y); spr.print(s); y += 9;
  };
  auto gap = [&]() { y += 4; };

  y += 12;  // room for the PET header drawn by drawPet()

  ln(p.body,    "MOOD");
  ln(p.textDim, " approve fast = up");
  ln(p.textDim, " deny lots = down"); gap();

  ln(p.body,    "FED");
  ln(p.textDim, " 50K tokens =");
  ln(p.textDim, " level up + confetti"); gap();

  ln(p.body,    "ENERGY");
  ln(p.textDim, " face-down to nap");
  ln(p.textDim, " refills to full"); gap();

  ln(p.textDim, "idle 30s = off");
  ln(p.textDim, "any button = wake"); gap();

  ln(p.textDim, "A: screens  B: page");
  ln(p.textDim, "hold A: menu");
}

void drawPet() {
  const Palette& p = characterPalette();
  int y = 70;

  if (petPage == 0) drawPetStats(p);
  else if (petPage == 1) drawPetUsage(p);
  else drawPetHowTo(p);

  // Header on top of whichever page drew — title left, counter right
  spr.setTextSize(1);
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, y + 2);
  if (ownerName()[0]) {
    spr.printf("%s's %s", ownerName(), petName());
  } else {
    spr.print(petName());
  }
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 28, y + 2);
  spr.printf("%u/%u", petPage + 1, PET_PAGES);
}

// Compact token count for one-line rows: 842 / 18k / 90k / 1.2M
static void fmtTok(char* out, size_t cap, uint32_t v) {
  if (v >= 1000000)   snprintf(out, cap, "%lu.%luM", (unsigned long)(v / 1000000),
                               (unsigned long)((v / 100000) % 10));
  else if (v >= 1000) snprintf(out, cap, "%luk", (unsigned long)(v / 1000));
  else                snprintf(out, cap, "%lu", (unsigned long)v);
}

// DISP_SESSIONS: per-session list from the v2 heartbeat. Two rows each:
// [cursor][state][agent] title ....tok / dimmed last-line. State glyphs are
// ASCII because the on-device fonts stop at 0x7E: > run, ! wait, . idle,
// * done. BtnB short moves the selection, BtnB long pins/unpins focus.
static void drawSessions() {
  const Palette& p = characterPalette();
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  int y = TOP + 2;

  // Header: connected host's name left, session count right. (Spec sketch
  // says "<hostname> · N sess" — the middot isn't in the ASCII font.)
  const char* hn = hostGlueActiveName();
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, y);
  spr.printf("%.13s", hn[0] ? hn : (tama.connected ? "host" : "no host"));
  char cnt[10];
  snprintf(cnt, sizeof(cnt), "%u sess", _sessions.count);
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - (int)strlen(cnt) * 6 - 4, y);
  spr.print(cnt);
  y += 14;

  int selIdx = sessionTabSelIdx(&_sessions);
  int pinIdx = sessionTabPinIdx(&_sessions);
  for (uint8_t i = 0; i < _sessions.count; i++) {
    const Session& s = _sessions.s[i];
    bool sel = (int)i == selIdx;
    char g = s.state == SES_RUN  ? '>' :
             s.state == SES_WAIT ? '!' :
             s.state == SES_DONE ? '*' : '.';
    char a = (s.agent[0] == 'x' || strncmp(s.agent, "codex", 5) == 0) ? 'X' : 'C';

    // Row 1: cursor, state glyph (waiting highlighted), agent, title, tokens
    spr.setCursor(2, y);
    spr.setTextColor(sel ? p.text : p.textDim, p.bg);
    spr.print(sel ? '>' : ' ');
    spr.setTextColor(s.state == SES_WAIT ? HOT : (sel ? p.text : p.textDim), p.bg);
    spr.print(g);
    spr.setTextColor(sel ? p.text : p.textDim, p.bg);
    spr.print(a);
    spr.print(' ');
    spr.printf("%.12s", s.title[0] ? s.title : s.sid);
    char tok[8];
    fmtTok(tok, sizeof(tok), s.tok);
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(W - (int)strlen(tok) * 6 - 4, y);
    spr.print(tok);
    y += 9;

    // Row 2: last activity line, dimmed; focus-pin marker at the far right
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(8, y);
    spr.printf("%.19s", s.last);
    if ((int)i == pinIdx) {
      spr.setTextColor(p.body, p.bg);
      spr.setCursor(W - 10, y);
      spr.print("#");
    }
    y += 11;
  }

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(4, H - 10);
  spr.print(pinIdx >= 0 ? "B:next  hold:unpin" : "B:next  hold:pin");
}

// Ask overlay (protocol v2, read-only): first question of an ask event.
// Priority sits just below the approval overlay — a pending permission
// prompt always wins the bottom area. A scrolls the option cursor, B or
// A-long dismisses; answering happens on the desktop.
static void drawAsk() {
  const Palette& p = characterPalette();
  const int AREA = 104;
  spr.fillRect(0, H - AREA, W, AREA, p.bg);
  spr.drawFastHLine(0, H - AREA, W, p.textDim);
  spr.setTextSize(1);
  int y = H - AREA + 3;

  spr.setTextColor(p.body, p.bg);
  spr.setCursor(4, y);
  if (tama.qHeader[0]) spr.printf("%.21s", tama.qHeader);
  else spr.print("Question");
  if (tama.qMulti) {
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(W - 22, y);
    spr.print("m+");   // multi-select hint
  }
  y += 10;

  // Question text, wrapped to at most 3 rows
  char rows[3][24];
  uint8_t nRows = wrapInto(tama.qText, rows, 3, 21);
  spr.setTextColor(p.text, p.bg);
  for (uint8_t i = 0; i < nRows; i++) {
    spr.setCursor(4, y);
    spr.print(rows[i]);
    y += 8;
  }
  y = H - AREA + 3 + 10 + 3 * 8 + 2;   // options at a fixed y for stability

  for (uint8_t i = 0; i < tama.qNOpts; i++) {
    bool cur = i == askCursor;
    spr.setTextColor(cur ? p.text : p.textDim, p.bg);
    spr.setCursor(4, y);
    spr.printf("%c%.20s", cur ? '>' : ' ', tama.qOpts[i]);
    y += 9;
  }

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(4, H - 10);
  spr.print("answer on desktop");
  spr.setCursor(W - 40, H - 10);
  spr.print("B:ok");
}

// DISP_CARDS: ntfy notification cards — auto-inserted in the carousel
// while the ring holds any. One card per view: kind tag + local HH:MM
// header, title, wrapped body. B = next card, B-long = dismiss. Rendering
// the screen clears the HUD unread badge (cards stay until dismissed or
// rung out by newer ones).
static uint16_t cardKindColor(const char* kind, const Palette& p) {
  if (strcmp(kind, "gh") == 0) return p.body;
  if (strcmp(kind, "ci") == 0) return GREEN;
  if (strcmp(kind, "weather") == 0 || strcmp(kind, "wx") == 0) return 0x07FF;
  // firmware-update-available badge (bridge-pushed; see PROTOCOL_V2.md).
  // Hot tag = "worth a look", but it's still just a card — updating stays
  // a deliberate walk to menu > update... on OTA builds.
  if (strcmp(kind, "update") == 0) return HOT;
  return p.textDim;   // unknown kinds render generic, per spec
}

static void drawCards() {
  const Palette& p = characterPalette();
  const int TOP = 70;
  spr.fillRect(0, TOP, W, H - TOP, p.bg);
  spr.setTextSize(1);
  int y = TOP + 2;
  _ntfy.unread = 0;                       // viewed — badge clears
  if (cardSel >= _ntfy.count) cardSel = 0;
  const NtfyCard* c = ntfyCardAt(&_ntfy, cardSel);

  spr.setTextColor(p.text, p.bg);
  spr.setCursor(4, y); spr.print("Cards");
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 28, y); spr.printf("%u/%u", cardSel + 1, _ntfy.count);
  y += 14;
  if (!c) {   // ring emptied under us — carousel will skip next cycle
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(4, y); spr.print("no cards");
    return;
  }

  spr.setTextColor(cardKindColor(c->kind, p), p.bg);
  spr.setCursor(4, y);
  spr.printf("[%.6s]", c->kind[0] ? c->kind : "*");
  if (c->ts && dataRtcValid()) {
    // Bridge epoch (UTC) + its tz offset -> local wall time for the stamp.
    struct tm ct;
    clockEpochToTmUtc((time_t)((int64_t)c->ts + dataTzOffset()), &ct);
    char hm[8]; clockFormatHm(hm, sizeof(hm), ct.tm_hour, ct.tm_min);
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(W - 34, y);
    spr.print(hm);
  }
  y += 12;

  char rows[2][24];
  uint8_t nR = wrapInto(c->title, rows, 2, 21);
  spr.setTextColor(p.text, p.bg);
  for (uint8_t i = 0; i < nR; i++) { spr.setCursor(4, y); spr.print(rows[i]); y += 9; }
  y += 4;

  char brows[8][24];
  uint8_t nB = wrapInto(c->body, brows, 8, 21);
  spr.setTextColor(p.textDim, p.bg);
  for (uint8_t i = 0; i < nB; i++) { spr.setCursor(4, y); spr.print(brows[i]); y += 9; }

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(4, H - 10);
  spr.print("B:next  hold:dismiss");
}

void drawHUD() {
  if (tama.promptId[0]) { drawApproval(); return; }
  if (tama.qAskId[0] && !askDismissed) { drawAsk(); return; }
  const Palette& p = characterPalette();
  const int SHOW = 3, LH = 8, WIDTH = 21;
  const int BADGE = 10;
  const int AREA = SHOW * LH + 4 + BADGE;
  spr.fillRect(0, H - AREA, W, AREA, p.bg);
  spr.setTextSize(1);

  // Host badge line (v2): connected host's name — or the focus-pinned
  // session's title — plus ntfy count and one pip per session (host sends
  // waiting-first; wait pips run hot, running green).
  {
    int by = H - AREA + 1;
    int pinIdx = sessionTabPinIdx(&_sessions);
    spr.setCursor(4, by);
    if (pinIdx >= 0) {
      spr.setTextColor(p.body, p.bg);
      spr.printf("#%.11s", _sessions.s[pinIdx].title);
    } else if (!tama.connected) {
      spr.setTextColor(p.textDim, p.bg);
      spr.print("no host");
    } else {
      const char* hn = hostGlueActiveName();
      spr.setTextColor(p.textDim, p.bg);
      spr.printf("%.11s", hn[0] ? hn : "host");
    }
    if (_ntfy.unread) {   // clears once the cards screen is viewed
      char nb[6];
      snprintf(nb, sizeof(nb), "n%u", _ntfy.unread);
      spr.setTextColor(p.body, p.bg);
      spr.setCursor(W - 8 - (int)_sessions.count * 7 - (int)strlen(nb) * 6, by);
      spr.print(nb);
    }
    for (uint8_t i = 0; i < _sessions.count; i++) {
      uint16_t c = _sessions.s[i].state == SES_WAIT ? HOT
                 : _sessions.s[i].state == SES_RUN  ? GREEN : p.textDim;
      int px = W - 8 - (int)(_sessions.count - 1 - i) * 7;
      if (_sessions.s[i].state == SES_RUN || _sessions.s[i].state == SES_WAIT)
        spr.fillCircle(px, by + 3, 2, c);
      else
        spr.drawCircle(px, by + 3, 2, c);
    }
  }

  if (tama.lineGen != lastLineGen) { lastLineGen = tama.lineGen; wake(); }

  if (tama.nLines == 0) {
    spr.setTextColor(p.text, p.bg);
    spr.setCursor(4, H - LH - 2);
    // Focus pin filters the idle line: show the pinned session's last
    // activity instead of the global summary when transcripts are off.
    int pinIdx = sessionTabPinIdx(&_sessions);
    if (pinIdx >= 0 && _sessions.s[pinIdx].last[0]) {
      spr.printf("%.21s", _sessions.s[pinIdx].last);
    } else {
      spr.print(tama.msg);
    }
    return;
  }

  // Wrap all transcript lines into a flat display buffer. Track which
  // transcript index each display row came from, so we can dim older ones.
  static char disp[32][24];
  static uint8_t srcOf[32];
  uint8_t nDisp = 0;
  for (uint8_t i = 0; i < tama.nLines && nDisp < 32; i++) {
    uint8_t got = wrapInto(tama.lines[i], &disp[nDisp], 32 - nDisp, WIDTH);
    for (uint8_t j = 0; j < got; j++) srcOf[nDisp + j] = i;
    nDisp += got;
  }

  uint8_t maxBack = (nDisp > SHOW) ? (nDisp - SHOW) : 0;
  if (msgScroll > maxBack) msgScroll = maxBack;

  int end = (int)nDisp - msgScroll;
  int start = end - SHOW; if (start < 0) start = 0;
  uint8_t newest = tama.nLines - 1;
  for (int i = 0; start + i < end; i++) {
    uint8_t row = start + i;
    bool fresh = (srcOf[row] == newest) && (msgScroll == 0);
    spr.setTextColor(fresh ? p.text : p.textDim, p.bg);
    spr.setCursor(4, H - AREA + 2 + i * LH);
    spr.print(disp[row]);
  }
  if (msgScroll > 0) {
    spr.setTextColor(p.body, p.bg);
    spr.setCursor(W - 18, H - LH - 2);
    spr.printf("-%u", msgScroll);
  }
}

// Boot-stage tracing for bring-up debugging: build with -DBUDDY_BOOT_TRACE
// (plus -DCORE_DEBUG_LEVEL=3 for the M5GFX autodetect log) to get per-stage
// serial checkpoints and an early red test-fill. Compiles away otherwise.
#ifdef BUDDY_BOOT_TRACE
#define BOOT_TRACE(tag) do { \
    Serial.printf("[boot] %-12s t=%lums heap=%u\n", tag, \
                  (unsigned long)millis(), (unsigned)ESP.getFreeHeap()); \
    Serial.flush(); \
  } while (0)
#else
#define BOOT_TRACE(tag)
#endif

void setup() {
  board::begin();        // M5.begin + speaker + per-board power/LED setup
  board::serialInit();
#ifdef BUDDY_BOOT_TRACE
  { uint32_t t0 = millis(); while (!Serial && millis() - t0 < 3000) delay(10); }
  Serial.printf("[boot] board=%d disp=%dx%d rot=%d\n",
                (int)M5.getBoard(), M5.Display.width(), M5.Display.height(),
                (int)M5.Display.getRotation());
  M5.Display.fillScreen(0xF800 /* red test fill */);
  delay(400);
  BOOT_TRACE("m5+serial");
#endif
  // The env-configured full clock (S3/Plus2 240MHz, Plus 160MHz) — what
  // wake() restores after the screen-off downclock.
  cpuFullMhz = getCpuFrequencyMhz();
  M5.Display.setRotation(0);
  settingsLoad();                 // before startBt: bleInit needs s_pair
  BOOT_TRACE("settings");
#ifdef BUDDY_BOOT_TRACE
  M5.Display.fillScreen(0xFFE0 /* yellow: settings loaded, entering BT */); delay(700);
#endif
  startBt(settings().pair);
  BOOT_TRACE("bt");
#ifdef BUDDY_BOOT_TRACE
  M5.Display.fillScreen(0xF81F /* magenta: BT up */); delay(700);
#endif
  // Registry after bleInit (the Bluedroid bond store must be up for the
  // boot reconcile), then mirror any pin remap back into settings.
  hostGlueInit(settings().hostsel, settings().hostfb);
  if (hostGluePin() != settings().hostsel) {
    settings().hostsel = hostGluePin();
    settingsSave();
  }
  BOOT_TRACE("hosts");
  applyBrightness();
  applyVolume();
  pomoInit(pomo);
  gestureInit(gest);
  lastInteractMs = millis();
  statsLoad();
  petNameLoad();
  buddyInit();
  BOOT_TRACE("stats+buddy");

  // BLE stays always-on; s.bt is stored as a preference only.
  spr.setColorDepth(16);
  spr.createSprite(W, H);
  BOOT_TRACE("sprite");
  characterInit(nullptr);
  BOOT_TRACE("character");
  // Legacy WiFi cred migration (NVS single ssid/pass -> LittleFS /wifi.dat
  // list). Must follow characterInit — that's where LittleFS gets mounted.
  // No-op every boot after the keys are gone.
  wifiStoreInit();
  gifAvailable = characterLoaded();
  // species NVS: 0..N-1 = ASCII species, 0xFF = use GIF (also the default,
  // so a fresh install lands on the GIF). With no GIF installed, 0xFF falls
  // through to buddyInit()'s clamped default.
  buddyMode = !(gifAvailable && speciesIdxLoad() == SPECIES_GIF);
  applyDisplayMode();

  {
    const Palette& p = characterPalette();
    spr.fillSprite(p.bg);
    spr.setTextDatum(MC_DATUM);
    spr.setTextSize(2);
    if (ownerName()[0]) {
      char line[40];
      snprintf(line, sizeof(line), "%s's", ownerName());
      spr.setTextColor(p.text, p.bg);   spr.drawString(line, W/2, H/2 - 12);
      spr.setTextColor(p.body, p.bg);   spr.drawString(petName(), W/2, H/2 + 12);
    } else {
      // First boot, no owner pushed yet — say hi.
      spr.setTextColor(p.body, p.bg);   spr.drawString("Hello!", W/2, H/2 - 12);
      spr.setTextSize(1);
      spr.setTextColor(p.textDim, p.bg);
      spr.drawString("a buddy appears", W/2, H/2 + 12);
    }
    spr.setTextDatum(TL_DATUM); spr.setTextSize(1);
    spr.pushSprite(0, 0);
    delay(1800);
  }

  Serial.printf("buddy: %s\n", buddyMode ? "ASCII mode" : "GIF character loaded");

#ifdef BUDDY_DEBUG
  {
    int cur = (int)esp_reset_reason();
    if (cur != ESP_RST_POWERON && cur != ESP_RST_SW) {
      g_lastReset = (uint32_t)cur; g_lastResetMagic = 0xB0D1;  // latch a real crash
    } else if (g_lastResetMagic != 0xB0D1) {
      g_lastReset = 0;                                         // cold boot: garbage
    }
    Serial.printf("[DBG] boot: reset_reason=%d last_crash=%lu\n",
                  cur, (unsigned long)g_lastReset);
  }
#endif

  // Self-heal watchdog (johnwong 3fa0884): if loop() ever wedges (e.g. a
  // corrupt render path), the BLE stack task keeps ACKing writes so the
  // bridge sees a healthy link while the screen is frozen — only a manual
  // power-cycle recovered it. Subscribe the loop task to the TWDT with
  // panic=reboot so any >12s stall reboots the device, which then
  // re-advertises and the bridge reconnects on its own.
  esp_task_wdt_init(12, true);   // 12s timeout, panic+reboot on elapse
  esp_task_wdt_add(NULL);        // watch this (loop) task
}

void loop() {
  esp_task_wdt_reset();   // pet the watchdog; loop normally cycles <100ms
  board::update();
#ifdef BUDDY_EXTRAS_FULL
  tuneTick();   // async jingle sequencer; no-op while idle
#endif
  t++;
  uint32_t now = millis();

  blePairingTick(now); // pairing-window expiry + post-evict adv fallback
  hostGluePoll(now);   // multi-host policy engine (soft-pin, pairing window)
  // A NEW host completed bonding (pairing window or out-of-box): confirm on
  // screen. Name is the adopt-time placeholder (Host-N) until its hello
  // lands and renames it in the registry.
  {
    char pairedName[24];
    if (hostGlueTakePaired(pairedName, sizeof(pairedName))) {
      char t[32];
      snprintf(t, sizeof(t), "paired: %s", pairedName);
      showToast(t, 2400);
    }
  }
  dataPoll(&tama);
  if (statsPollLevelUp()) triggerOneShot(P_CELEBRATE, 3000);
  baseState = derive(tama);

  // Pomodoro tick. Phase transitions chime through beep() (vol 0 = mute):
  // focus done = two-note descent into the break, break over = single
  // nudge back to work. Note 2 is millis-scheduled — tone() one-at-a-time.
  // No wake(): the chime is the notification, the screen can stay off.
  static uint32_t pomoNote2At = 0;
  PomoEvent pev = pomoTick(pomo, now);
  if (pev == POMO_EV_FOCUS_DONE) {
    beep(2000, 100);
    pomoNote2At = now + 130;
  } else if (pev == POMO_EV_BREAK_DONE) {
    beep(1400, 180);
  }
  if (pomoNote2At && (int32_t)(now - pomoNote2At) >= 0) {
    beep(1500, 150);
    pomoNote2At = 0;
  }

#ifdef BUDDY_EXTRAS_FULL
  // IR learn session: poll the capture (non-blocking), time it out, and
  // drop it if the user leaves the page — the RX driver only lives while
  // a learn is actually armed.
  if (irLearning()) {
    if (displayMode != DISP_EXTRAS || extrasPage != 1) {
      irLearnCancel();
    } else {
      int r = irLearnPoll();
      if (r == 1)       { showToast("learned"); beep(2400, 60); }
      else if (r == -1) { showToast("no signal"); beep(600, 60); }
      else if ((int32_t)(now - irLearnDeadline) >= 0) {
        irLearnCancel();
        showToast("learn timeout");
      }
    }
  }
#endif

  // Pomodoro focus: look busy while the timer runs and the desk is quiet.
  // Real host state always wins — this only upgrades an otherwise-idle
  // buddy (and skips the idle->sleep demotion below, on purpose).
  if (pomo.phase == POMO_FOCUS && !pomo.paused && baseState == P_IDLE &&
      tama.sessionsRunning == 0 && tama.sessionsWaiting == 0 &&
      !tama.recentlyCompleted) {
    baseState = P_BUSY;
  }

  // After waking the screen, hold sleep for 12s so users see the wake-up
  // animation. Urgent states (attention, celebrate, busy) override this.
  if (baseState == P_IDLE && (int32_t)(now - wakeTransitionUntil) < 0) baseState = P_SLEEP;

  if ((int32_t)(now - oneShotUntil) >= 0) activeState = baseState;

  // 'Task complete' chime: paired with the celebrate animation. The buddy is
  // the visual notification, but without an audio cue you miss it entirely
  // when looking at another screen. Respects settings().vol via beep()
  // (vol 0 = mute drops the tones entirely).
  //
  // Two-note ascending chime (2000 Hz → 2800 Hz, the universal "ta-da" of
  // completion sounds) so it doesn't collide with the existing single-tone
  // palette (600 deny / 1200 prompt / 1800 cycle / 2400 approve). Note 2 is
  // scheduled via millis() because M5.Beep.tone() is one-at-a-time —
  // calling it twice in the same tick stomps the first note.
  static PersonaState prevActiveState = P_SLEEP;
  static uint32_t chimeNote2At = 0;
  if (activeState == P_CELEBRATE && prevActiveState != P_CELEBRATE) {
#ifdef BUDDY_EXTRAS_FULL
    if (tunesJingle(SLOT_DONE)) {
      chimeNote2At = 0;   // the tune replaces both notes
    } else
#endif
    {
      beep(2000, 100);
      chimeNote2At = now + 130;
    }
  }
  if (chimeNote2At && (int32_t)(now - chimeNote2At) >= 0) {
    beep(2800, 150);
    chimeNote2At = 0;
  }
  prevActiveState = activeState;

  // LED: pulse on attention, otherwise off (no-op on LED-less boards)
  board::attentionLed(activeState == P_ATTENTION && settings().led &&
                      (now / 400) % 2 != 0);

  // shake → dizzy + force scenario advance
  if (now - lastShakeCheck > 50) {
    lastShakeCheck = now;
    if (!menuOpen && !screenOff && checkShake() && (int32_t)(now - oneShotUntil) >= 0) {
      wake();
      triggerOneShot(P_DIZZY, 2000);
      Serial.println("shake: dizzy");
    }

    // IMU gestures on the same 50ms cadence (checkShake keeps its own
    // read + EMA; one extra I2C read here is noise next to the render).
    // Gestures run even with the screen off — that's the whole point of
    // double-tap-to-wake. NO gesture ever answers a prompt.
    float gax, gay, gaz;
    board::imuAccel(&gax, &gay, &gaz);
    GestureEvent ge = gestureFeed(gest, gax, gay, gaz, now);
    if (ge == GE_FLIP && !napping && !tama.promptId[0] &&
        hostGlueCount() > 0) {
      // Flip = pin the next bonded host, same cycle as settings > host
      // mode (auto -> h0 -> ... -> auto). No isFaceDown() gate here any
      // more: GE_FLIP now only fires on the return-to-upright edge (see
      // gesture_logic.h), so it's already gone by the time a flip would
      // read as face-down. !napping stays as a belt-and-suspenders no-op
      // guard — nap needs sustained face-down, which a flip-and-return
      // never holds long enough to reach.
      int8_t next = settings().hostsel + 1;
      if (next >= (int8_t)hostGlueCount()) next = -1;
      settings().hostsel = next;
      settingsSave();
      hostGlueSetPin(next);
      char tb[20];
      snprintf(tb, sizeof(tb), "-> %.14s",
               next < 0 ? "auto" : hostGlueGet((uint8_t)next)->name);
      showToast(tb, 2600);   // long enough to survive flipping back over
      beep(1800, 60);
      wake();
      Serial.printf("flip: host %s\n", next < 0 ? "auto" : hostGlueGet((uint8_t)next)->name);
    } else if (ge == GE_DOUBLE_TAP && !napping) {
      wake();   // tap-tap the desk to light the screen without a button
    }
  }

  // BtnA: step through fake scenarios
  // Prompt arrival: beep, reset response flag
  if (strcmp(tama.promptId, lastPromptId) != 0) {
    strncpy(lastPromptId, tama.promptId, sizeof(lastPromptId)-1);
    lastPromptId[sizeof(lastPromptId)-1] = 0;
    responseSent = false;
    if (tama.promptId[0]) {
      promptArrivedMs = millis();
      wake();
      TUNE_OR_BEEP(SLOT_ALERT, 1200, 80);   // alert
      // Jump to the approval screen no matter what was open — drawApproval
      // only runs from drawHUD which only runs in DISP_NORMAL.
      displayMode = DISP_NORMAL;
      menuOpen = settingsOpen = resetOpen = hostsOpen = forgetOpen = extrasOpen = false;
      applyDisplayMode();
      characterInvalidate();
      if (buddyMode) buddyInvalidate();
    }
  }

  // ntfy card arrival: short chirp (vol-gated via beep) and deliberately
  // NO wake() — attention without screen burn. If the screen is off it
  // stays off; the unread badge and carousel pick it up later.
  static uint32_t seenNtfyTotal = 0;
  if (_ntfy.total > seenNtfyTotal) {
    seenNtfyTotal = _ntfy.total;
    beep(1600, 50);
  }

  // Ask arrival (v2, read-only): chirp + re-arm the overlay for the new id.
  // Unlike prompts it doesn't hijack the display mode — it renders when the
  // HUD is visible and waits its turn behind a pending approval.
  if (strcmp(tama.qAskId, lastAskId) != 0) {
    strncpy(lastAskId, tama.qAskId, sizeof(lastAskId) - 1);
    lastAskId[sizeof(lastAskId) - 1] = 0;
    askDismissed = false;
    askCursor = 0;
    if (tama.qAskId[0]) {
      wake();
      beep(1000, 80);
    }
  }

  // Clear prompts that have been pending too long. The desktop that sent
  // this likely went offline without clearing it; 5 minutes is long enough
  // to step away and come back without losing a legitimate prompt.
  static const uint32_t PROMPT_TIMEOUT_MS = 5UL * 60UL * 1000UL;
  if (tama.promptId[0] && !responseSent
      && (millis() - promptArrivedMs) > PROMPT_TIMEOUT_MS) {
    protoPromptClear(&tama);   // id + tool/hint + v2 sid/qn together
  }

  bool inPrompt = tama.promptId[0] && !responseSent;

  // Button-press wake. Track which button woke the screen so its full
  // press cycle (including long-press) is swallowed — you don't want
  // BtnA-to-wake to also cycle displayMode or open the menu.
  if (M5.BtnA.isPressed() || M5.BtnB.isPressed()) {
    if (screenOff) {
      if (M5.BtnA.isPressed()) swallowBtnA = true;
      if (M5.BtnB.isPressed()) swallowBtnB = true;
    }
    wake();
  }

  // While the pairing passkey is on screen, swallow both buttons entirely:
  // the menu overlays are hidden but their handlers would otherwise stay
  // live, and a stray press mid-pairing could drive a hidden menu (worst
  // case: forget-host). Pairing is answered on the desktop, not here.
  if (blePasskey()) { swallowBtnA = true; swallowBtnB = true; }

  // Power button (left side): single click toggles the screen. On the S3
  // a double click powers off (its PMIC reserves the long hold for download
  // mode); AXP boards keep the hardware 6s hold as the off gesture.
  if (board::powerOffRequested()) {
    beep(660, 120);
    delay(150);          // let the tone finish before the power rail drops
    board::powerOff();   // PMIC off on battery; deep-sleep on USB
  } else if (board::screenToggleRequested()) {
    if (screenOff) {
      wake();
    } else {
      board::screenPower(false);
      screenOff = true;
    }
  }

  if (M5.BtnA.pressedFor(600) && !btnALong && !swallowBtnA) {
    btnALong = true;
    beep(800, 60);
    if (askShowing()) { askDismissed = true; }   // A-long dismisses the ask card
    else if (forgetOpen) { forgetOpen = false; }
    else if (hostsOpen) { hostsOpen = false; characterInvalidate(); }
    else if (resetOpen) { resetOpen = false; }
    else if (settingsOpen) { settingsOpen = false; characterInvalidate(); }
    else if (extrasOpen) { extrasOpen = false; characterInvalidate(); }
    else if (displayMode == DISP_EXTRAS) {
      // leave the extras screen (pomodoro keeps running in the background)
      displayMode = DISP_NORMAL;
      applyDisplayMode();
    }
    else {
      menuOpen = !menuOpen;
      menuSel = 0;
      if (!menuOpen) characterInvalidate();
    }
    Serial.println(menuOpen ? "menu open" : "menu close");
  }
  if (M5.BtnA.wasReleased()) {
    if (!btnALong && !swallowBtnA) {
      if (inPrompt) {
        char cmd[96];
        snprintf(cmd, sizeof(cmd), "{\"cmd\":\"permission\",\"id\":\"%s\",\"decision\":\"once\"}", tama.promptId);
        sendCmd(cmd);
        responseSent = true;
        uint32_t tookS = (millis() - promptArrivedMs) / 1000;
        statsOnApproval(tookS);
        TUNE_OR_BEEP(SLOT_APPROVE, 2400, 60);
        if (tookS < 5) triggerOneShot(P_HEART, 2000);
      } else if (askShowing()) {
        beep(1800, 30);
        if (tama.qNOpts > 0) askCursor = (uint8_t)((askCursor + 1) % tama.qNOpts);
      } else if (forgetOpen) {
        beep(1800, 30);
        forgetSel = (uint8_t)((forgetSel + 1) % (hostGlueCount() + 1));
      } else if (hostsOpen) {
        beep(1800, 30);
        hostsSel = (uint8_t)((hostsSel + 1) % hostsRowCount());
      } else if (resetOpen) {
        beep(1800, 30);
        resetSel = (resetSel + 1) % RESET_N;
        resetConfirmIdx = 0xFF;
      } else if (settingsOpen) {
        beep(1800, 30);
        settingsSel = (settingsSel + 1) % SETTINGS_N;
      } else if (extrasOpen) {
        beep(1800, 30);
        extrasSel = (extrasSel + 1) % EXTRAS_N;
      } else if (menuOpen) {
        beep(1800, 30);
        menuSel = (menuSel + 1) % MENU_N;
      } else if (displayMode == DISP_EXTRAS) {
        beep(1800, 30);
#ifdef BUDDY_EXTRAS_FULL
        if (extrasPage == 1) irSel = (uint8_t)((irSel + 1) % IR_SLOTS);
        else
#endif
        pomoStartPause(pomo, millis());   // pomodoro page: A drives the timer
      } else {
        beep(1800, 30);
        // Skip screens with nothing to show: the session list while empty,
        // the clock until a time source exists. DISP_NORMAL is never
        // skipped, so the walk always terminates.
        do {
          displayMode = (displayMode + 1) % DISP_COUNT;
        } while ((displayMode == DISP_SESSIONS && _sessions.count == 0) ||
                 (displayMode == DISP_CLOCK && !board::clockValid()) ||
                 (displayMode == DISP_CARDS && _ntfy.count == 0));
        if (displayMode == DISP_CARDS) cardSel = 0;   // land on the newest
        applyDisplayMode();
      }
    }
    btnALong = false;
    swallowBtnA = false;
  }

  // BtnB: short = act on release (deny / select / next); long (600ms) =
  // session focus controls. Release-based so the long press doesn't also
  // fire the short action — same pattern as BtnA.
  // Only claim the press as "long" when a long action actually exists in
  // this context — otherwise a slow B press must still fire its short
  // action on release (a held B during a prompt still denies, etc).
  if (M5.BtnB.pressedFor(600) && !btnBLong && !swallowBtnB &&
      !inPrompt && !menuOpen && !settingsOpen && !resetOpen &&
      !hostsOpen && !forgetOpen && !extrasOpen && !askShowing()) {
    if (displayMode == DISP_SESSIONS) {
      // pin/unpin focus on the selected session
      btnBLong = true;
      beep(800, 60);
      bool pinned = sessionTabTogglePin(&_sessions);
      showToast(pinned ? "focus pinned" : "focus cleared");
    } else if (displayMode == DISP_EXTRAS) {
      btnBLong = true;
      beep(800, 60);
#ifdef BUDDY_EXTRAS_FULL
      if (extrasPage == 1) {
        // IR page: B-long arms a learn session on the selected slot
        if (!irLearning() && irLearnStart(irSel)) {
          irLearnDeadline = millis() + 8000;
          showToast("listening 8s");
        }
      } else
#endif
      {
        // pomodoro page: B-long resets the timer to idle
        pomoStop(pomo);
        showToast("pomodoro reset");
      }
    } else if (displayMode == DISP_CARDS) {
      // dismiss the shown card; an empty ring drops us back home
      btnBLong = true;
      beep(800, 60);
      if (ntfyDismiss(&_ntfy, cardSel)) {
        if (cardSel >= _ntfy.count) cardSel = 0;
        showToast("dismissed");
        if (_ntfy.count == 0) {
          displayMode = DISP_NORMAL;
          applyDisplayMode();
        }
      }
    } else if (displayMode == DISP_NORMAL) {
      btnBLong = true;
      beep(800, 60);
      if (_sessions.count > 0) {
        displayMode = DISP_SESSIONS;
        applyDisplayMode();
      } else {
        showToast("no sessions");
      }
    }
  }
  if (M5.BtnB.wasReleased()) {
    if (swallowBtnB || btnBLong) {
      // swallowed wake press or long-press already handled
    } else if (inPrompt) {
      char cmd[96];
      snprintf(cmd, sizeof(cmd), "{\"cmd\":\"permission\",\"id\":\"%s\",\"decision\":\"deny\"}", tama.promptId);
      sendCmd(cmd);
      responseSent = true;
      statsOnDenial();
      TUNE_OR_BEEP(SLOT_DENY, 600, 60);
    } else if (askShowing()) {
      beep(2400, 30);
      askDismissed = true;   // read-only card — B just puts it away
    } else if (forgetOpen) {
      beep(2400, 30);
      forgetConfirm();
    } else if (hostsOpen) {
      beep(2400, 30);
      hostsConfirm();
    } else if (resetOpen) {
      beep(2400, 30);
      applyReset(resetSel);
    } else if (settingsOpen) {
      beep(2400, 30);
      applySetting(settingsSel);
    } else if (extrasOpen) {
      beep(2400, 30);
      extrasConfirm();
    } else if (menuOpen) {
      beep(2400, 30);
      menuConfirm();
    } else if (displayMode == DISP_EXTRAS) {
      beep(2400, 30);
#ifdef BUDDY_EXTRAS_FULL
      if (extrasPage == 1) {
        if (irLearning()) { irLearnCancel(); showToast("learn cancelled"); }
        else showToast(irBlast(irSel) ? "sent" : "empty");
      } else
#endif
      pomoSkip(pomo, millis());   // pomodoro page: B skips the phase
    } else if (displayMode == DISP_INFO) {
      beep(2400, 30);
      infoPage = (infoPage + 1) % INFO_PAGES;
    } else if (displayMode == DISP_PET) {
      beep(2400, 30);
      petPage = (petPage + 1) % PET_PAGES;
      applyDisplayMode();
    } else if (displayMode == DISP_SESSIONS) {
      beep(2400, 30);
      sessionTabSelectNext(&_sessions);
    } else if (displayMode == DISP_CARDS) {
      beep(2400, 30);
      if (_ntfy.count) cardSel = (uint8_t)((cardSel + 1) % _ntfy.count);
    } else {
      beep(2400, 30);
      msgScroll = (msgScroll >= 30) ? 0 : msgScroll + 1;
    }
    btnBLong = false;
    swallowBtnB = false;
  }

  // blink bookkeeping

  // Charging clock: takes over the home screen when on USB power, no
  // overlays, no prompt, no live Claude data, and the RTC has been set
  // by the bridge. Pet sleeps underneath. Exit restores Y via
  // applyDisplayMode() so the next mode-switch isn't visually offset.
  clockRefreshRtc();   // 1Hz internal throttle; also caches _onUsb
  // Show the clock when nothing is happening — bridge heartbeat alone
  // doesn't count as activity (it's the only way to get the RTC synced).
  bool clocking = displayMode == DISP_NORMAL
               && !menuOpen && !settingsOpen && !resetOpen && !inPrompt
               && !hostsOpen && !forgetOpen && !askShowing()
               && tama.sessionsRunning == 0 && tama.sessionsWaiting == 0
               && dataRtcValid() && _onUsb;
  // User-selected clock screen (DISP_CLOCK): same face and orientation
  // machinery as the charging clock, but picked from the carousel. Any
  // overlay (menus, prompt, passkey) drops it back to the portrait sprite
  // path so the overlay stays visible; landscape draws direct to LCD.
  bool clockScreen = displayMode == DISP_CLOCK && !menuOpen && !settingsOpen
                  && !resetOpen && !hostsOpen && !forgetOpen && !inPrompt
                  && !blePasskey();
  if (clocking || clockScreen) clockUpdateOrient();
  else { clockOrient = 0; orientFrames = 0; clockSwapFrames = 0; paintedOrient = 0; }
  bool landscapeClock = (clocking || clockScreen) && clockOrient != 0;

  static bool wasClocking = false;
  static bool wasLandscape = false;
  if (clocking != wasClocking || landscapeClock != wasLandscape) {
    if (clocking && !landscapeClock) characterSetPeek(true);
    else applyDisplayMode();
    characterInvalidate();
    if (buddyMode) buddyInvalidate();
    wasClocking = clocking;
    wasLandscape = landscapeClock;
  }
  if (clocking) {
    uint8_t dow = clockDow();
    bool weekend = (dow == 0 || dow == 6);
    bool friday  = (dow == 5);

    uint8_t h = (uint8_t)_clk.tm_hour;
    if (h >= 1 && h < 7)             activeState = P_SLEEP;
    else if (weekend)                activeState = (now/8000 % 6 == 0) ? P_HEART : P_SLEEP;
    else if (h < 9)                  activeState = (now/6000 % 4 == 0) ? P_IDLE  : P_SLEEP;
    else if (h == 12)                activeState = (now/5000 % 3 == 0) ? P_HEART : P_IDLE;
    else if (friday && h >= 15)      activeState = (now/4000 % 3 == 0) ? P_CELEBRATE : P_IDLE;
    else if (h >= 22 || h == 0)      activeState = (now/7000 % 3 == 0) ? P_DIZZY : P_SLEEP;
    else                             activeState = (now/10000 % 5 == 0) ? P_SLEEP : P_IDLE;
  }

  static uint32_t lastPasskey = 0;
  uint32_t pk = blePasskey();
  if (pk && !lastPasskey) { wake(); beep(1800, 60); }
  lastPasskey = pk;

  // The pet/GIF paths deliberately repaint only their own bands, so pixels
  // from a just-closed overlay would otherwise linger. One full clear on the
  // overlay-close transition keeps them honest.
  static bool prevOverlayOpen = false;
  bool overlayOpen = forgetOpen || hostsOpen || resetOpen || settingsOpen ||
                     extrasOpen || menuOpen || pk != 0;
  if (prevOverlayOpen && !overlayOpen) {
    spr.fillSprite(characterPalette().bg);
  }
  prevOverlayOpen = overlayOpen;

  if (napping || screenOff || landscapeClock) {
    // skip sprite render — face-down, powered off, or landscape clock
    // (which draws direct-to-LCD below)
  } else if (buddyMode) {
    buddyTick(activeState);
  } else if (characterLoaded()) {
    characterSetState(activeState);
    characterTick();
  } else {
    const Palette& p = characterPalette();
    spr.fillSprite(p.bg);
    spr.setTextColor(p.textDim, p.bg);
    spr.setTextSize(1);
    if (xferActive()) {
      uint32_t done = xferProgress(), total = xferTotal();
      spr.setCursor(8, 90);
      spr.print("installing");
      spr.setCursor(8, 102);
      spr.printf("%luK / %luK", done/1024, total/1024);
      int barW = W - 16;
      spr.drawRect(8, 116, barW, 8, p.textDim);
      if (total > 0) {
        int fill = (int)((uint64_t)barW * done / total);
        if (fill > 1) spr.fillRect(9, 117, fill - 1, 6, p.body);
      }
    } else {
      spr.setCursor(8, 100);
      spr.print("no character loaded");
    }
  }
  if (landscapeClock) {
    drawClock(clockScreen);   // strip on the user screen, not while charging
  } else if (!napping && !screenOff) {
    if (blePasskey()) drawPasskey();
    else if (clocking) drawClock();
    else if (displayMode == DISP_CLOCK) drawClockScreen();
    else if (displayMode == DISP_CARDS) drawCards();
    else if (displayMode == DISP_EXTRAS) drawExtrasScreen();
    else if (displayMode == DISP_INFO) drawInfo();
    else if (displayMode == DISP_PET) drawPet();
    else if (displayMode == DISP_SESSIONS) drawSessions();
    else if (settings().hud) drawHUD();
    // Menu overlays yield to an active pairing passkey — the user is mid
    // "add host..." inside the hosts menu when the passkey arrives, and the
    // code must not paint the menu over it.
    if (!blePasskey()) {
      if (forgetOpen) drawForget();
      else if (hostsOpen) drawHosts();
      else if (resetOpen) drawReset();
      else if (settingsOpen) drawSettings();
      else if (extrasOpen) drawExtras();
      else if (menuOpen) drawMenu();
    }
    drawToast();
    spr.pushSprite(0, 0);
  }

  // Face-down nap: dim immediately, pause animations, accumulate sleep time.
  // Skipped during approval — you're holding it to read, not sleeping it.
  // Exit needs sustained not-down so IMU noise at the threshold doesn't
  // bounce brightness between 8 and full every few frames.
  static int8_t faceDownFrames = 0;
  if (!inPrompt) {
    bool down = isFaceDown();
    if (down)       { if (faceDownFrames < 20) faceDownFrames++; }
    else            { if (faceDownFrames > -10) faceDownFrames--; }
  }

  if (!napping && faceDownFrames >= 15) {
    napping = true;
    napStartMs = now;
    screenBreath(8);
    dimmed = true;
  } else if (napping && faceDownFrames <= -8) {
    napping = false;
    statsOnNapEnd((now - napStartMs) / 1000);
    statsOnWake();
    wake();
  }

  // millis() not the cached `now`: wake() runs after `now` is captured,
  // so now - lastInteractMs underflows when a button is held → flicker.
  // No auto-off on USB power — clock face wants to stay visible while charging.
  if (!screenOff && !inPrompt && !_onUsb
      && millis() - lastInteractMs > SCREEN_OFF_MS) {
    board::screenPower(false);
    screenOff = true;
  }

  // P7 power scaling, applied lazily so one site covers every path into
  // screen-off (idle timeout above, the power-button toggle) AND the case
  // where a busy guard (xfer / IR-learn / OTA) vetoed the downclock at the
  // transition and cleared later. BLE keeps serving at 80MHz — its
  // controller clock is the independent 40MHz XTAL — so prompts still
  // arrive and double-tap/flip gestures still run; wake() restores.
  if (screenOff && !powerBusyGuard() &&
      getCpuFrequencyMhz() != BUDDY_SCREEN_OFF_CPU_MHZ) {
    bleConnParams(true);   // relax link first — it's a queued radio request
    setCpuFrequencyMhz(BUDDY_SCREEN_OFF_CPU_MHZ);
  }

#ifdef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  // OTA rollback safety: the arduino core ships with bootloader rollback
  // on, so a slot written by OTA boots as PENDING_VERIFY and the bootloader
  // reverts to the previous slot on the NEXT reboot unless the app marks
  // itself valid. 15s of uptime with BLE up and the loop alive is the
  // "healthy" bar — a boot-crash or a wedged loop trips the 12s TWDT first,
  // and that reboot is exactly what triggers the fallback. Lives in ALL
  // builds, not just -DBUDDY_OTA: an -ota device can flash a plain release
  // image, and that image must self-validate too or it rolls back after
  // its first reboot. No-op (state != pending-verify) on esptool flashes.
  static bool otaSlotValidated = false;
  if (!otaSlotValidated && now > 15000) {
    otaSlotValidated = true;
    esp_ota_mark_app_valid_cancel_rollback();
  }
#endif

  delay(screenOff ? 100 : 16);
}
