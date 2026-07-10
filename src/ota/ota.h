#pragma once
#include <stddef.h>

// WiFi OTA from this repo's GitHub releases (Track F P8). Compiled only in
// -DBUDDY_OTA envs (m5stick-s3-ota / m5stickc-plus2-ota) — ota.cpp guards
// its whole body, same pattern as ir_remote.cpp — and main.cpp only calls
// in under the same flag. Requires an OTA partition table (app0+app1; the
// 8MB boards have one, the 4MB Plus does not).
//
// Flow (otaRun, blocking): join WiFi with the NVS "wifi" creds (provisioned
// via the v2 {"cmd":"wifi"} command — see PROTOCOL_V2.md) → query the
// latest GitHub release → download firmware-<board>.bin (fallback
// firmware.bin) into the inactive slot → reboot into it. On ANY outcome
// the WiFi stack is torn down again; BLE stays up throughout (coex).
// Update consent is the user walking menu → update... — never automatic.

// status = short lowercase step label for the progress screen;
// pct = 0..100 during the download, -1 for indeterminate steps.
typedef void (*OtaProgressFn)(const char* status, int pct);

// Blocking; returns only on failure or already-up-to-date, with errBuf
// holding a short screen-sized message. Success reboots the device.
bool otaRun(OtaProgressFn progress, char* errBuf, size_t errLen);

// True while otaRun is between its first and last step — main.cpp's
// power-scaling busy guard (belt-and-suspenders: the blocking flow never
// yields to the loop anyway).
bool otaInProgress();
