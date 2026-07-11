#pragma once
// Central firmware configuration: version, protocol, and per-board capacity
// knobs. Boards are selected by the PlatformIO env flag (-DBOARD_STICKS3 /
// -DBOARD_STICKC_PLUS / -DBOARD_STICKC_PLUS2); anything not board-specific
// lives in the shared section below.

// Release builds inject the tag version (-DBUDDY_FW_VERSION=\"x.y.z\"),
// which the OTA update gate compares against the latest release tag.
#ifndef BUDDY_FW_VERSION
#define BUDDY_FW_VERSION "0.2.0-dev"
#endif
#define BUDDY_PROTO_VER  2

// --- Per-board buffer capacities --------------------------------------------
// The S3 has 8MB flash + PSRAM and half a meg of SRAM to spare, so it gets
// roomy buffers: a bigger BLE RX ring (bursty transcript snapshots + GIF
// transfer chunks) and a line buffer big enough for a full base64 xfer chunk
// line. The ESP32 sticks keep the historic sizes.
#if defined(BOARD_STICKS3)
  #define BUDDY_RX_CAP   8192   // ble_bridge.cpp RX ring
  #define BUDDY_LINEBUF  4608   // data.h JSON line accumulator (USB + BT each)
#else
  #define BUDDY_RX_CAP   2048
  #define BUDDY_LINEBUF  1024
#endif

// --- Multi-host / session bounds (protocol v2) -------------------------------
// Cap on bonded desktop hosts tracked by the firmware, and on per-host
// sessions shown in the UI. Sized for the 8-line HUD with headroom.
#define BUDDY_MAX_HOSTS    7
#define BUDDY_MAX_SESSIONS 6
