#pragma once
#include <stdint.h>
#include <stddef.h>
#include "logic/wifi_store_logic.h"

// Device-side WiFi network list: LittleFS persistence around the pure model
// in logic/wifi_store_logic.h, plus the OTA join helper.
//
// Storage is the file /wifi.dat on the LittleFS data partition (~1.8 MB,
// shared with GIF character packs) — NOT NVS, whose 20 KB partition is far
// too small for up to WIFI_MAX (256) credentials. Binary format, little-
// endian, fixed-size records:
//   [0..3]  magic "BWF1"
//   [4..5]  uint16 count
//   [6..7]  reserved (0)
//   [8..]   count x WifiCred (97 bytes: ssid[33] + pass[64], NUL-padded)
// Writes go to /wifi.tmp then rename into place; the loader adopts a
// stranded tmp so a power cut mid-save loses at most the in-flight change.
//
// The model loads lazily on first use (heap; PSRAM when the board has it)
// and stays resident. Every board tier compiles this — a plain build can be
// provisioned first and flashed with an -ota env later — but only BUDDY_OTA
// builds ever join.
//
// Legacy migration: pre-list firmware kept a single ssid/pass in the NVS
// "wifi" namespace. wifiStoreInit() (call once at boot, AFTER LittleFS is
// mounted) imports those keys into the list, then deletes them. Factory
// reset covers both worlds: LittleFS.format() takes /wifi.dat and
// wifiCredsWipe() clears the legacy namespace.
//
// The pass is a secret — nothing here or downstream ever emits it (the
// format helper is names-only), and /wifi.dat is not reachable over the
// protocol (xfer.h has no file-read command and writes only under
// /characters/).

// One-shot legacy NVS -> LittleFS migration. Safe to call every boot: it
// no-ops unless the old single-cred keys exist.
void wifiStoreInit();

// Cheap: header read (cached) until the model is loaded, then exact.
uint16_t wifiStoreCount();

// Mutations persist before reporting success. Returns/semantics match the
// pure model (new count | WIFI_LIST_FULL | WIFI_LIST_ERR); a failed save
// resyncs the in-RAM model from disk so RAM never claims what flash lost.
int16_t wifiStoreUpsert(const char* ssid, const char* pass);
int16_t wifiStoreRemove(const char* ssid);
bool    wifiStoreClear();

// Names-only JSON string literals ("a","b") — see wifiListFormatSsids.
uint16_t wifiStoreFormatSsids(char* out, size_t outLen);

#ifdef BUDDY_OTA
// Scan, match stored creds against visible networks, try strongest RSSI
// first, fall through the matches until one joins or the budget is spent.
// tick (nullable) runs every poll step — the OTA screen repaint + watchdog
// pet. On failure errBuf gets a short screen-sized message that names (only
// the names of) the ssids tried. Leaves WiFi in STA mode on success; the
// caller owns teardown on every path.
bool wifiJoinBest(uint32_t timeoutMs, void (*tick)(void), char* errBuf, size_t errLen);
#endif
