# PORT SPEC — sticks3-buddy on LilyGO e-paper hardware

Design document only. No code changes ride with this file. It grounds a
port of the buddy firmware to four LilyGO devices in the *actual* seams of
this codebase — every claim about "what we have today" cites a real file
and function. Device specs were verified against vendor pages on
**2026-07-12**; discrepancies against the original device shortlist are
flagged rather than papered over.

Fixed points the port must not move (repo invariants):

- **Protocol stays untouched.** The wire format is transport-agnostic
  newline-JSON (`PROTOCOL_V2.md`); acks are already dual-written to USB
  serial and BLE (`protoEmit()` in `src/data.h`, `sendCmd()` in
  `src/main.cpp`). Adding transports adds *feeders*, not message types.
- **Bluedroid stays.** `src/ble_bridge.cpp` is written directly against
  the Arduino-core-2.x Bluedroid surface (`esp_gap_ble_api.h`,
  `esp_ble_get_bond_device_list()`, `BLESecurityCallbacks`,
  `BLEDevice::setCustomGapHandler(_gapKeyWatch)`). NimBLE / Arduino core
  3.x is a no-go; the platform pin is `platform = espressif32@6.12.0`
  (`platformio.ini`).
- **Pre-hello v1 purity, encrypted-link posture, NVS `sv` scheme** hold on
  every new transport exactly as they hold on BLE today.

---

## 0. Where the firmware stands today (codebase anchors)

### 0.1 The HAL seam — `namespace board` (`src/hal/hal.h`)

Everything per-board goes through one header. Current surface, verbatim:

| Group | Functions |
|---|---|
| lifecycle | `begin()`, `update()`, `name()` |
| display | `setBrightness(uint8_t)`, `screenPower(bool)` |
| battery/power | `batteryMilliVolts()`, `batteryPercent()`, `usbMilliVolts()`, `onUsb()`, `isCharging()`, `powerOff()`, `powerOffRequested()`, `screenToggleRequested()` |
| battery extras | `hasBatteryCurrent()`, `batteryCurrentMa()`, `hasCoulomb()`, `coulombEnable()`, `coulombClear()`, `coulombMah()`, `hasInternalTemp()`, `internalTempC()` |
| audio | `beep(uint16_t,uint16_t)`, `playTone(const uint16_t*,size_t)`, `setVolume(uint8_t)` |
| LED | `attentionLed(bool)` |
| clock | `setClock(const struct tm*)`, `getClock(struct tm*)`, `clockValid()` |
| sensors/io | `imuAccel(float*,float*,float*)`, `serialInit()` |

Backends: `src/hal/hal_common.cpp` (shared M5Unified paths — `M5.Display`,
`M5.Power`, `M5.Speaker`, `M5.Imu`) plus per-board
`hal_sticks3.cpp` / `hal_stickc_plus.cpp` / `hal_stickc_plus2.cpp`, each
fully guarded by its `BOARD_*` flag so `build_src_filter` stays `+<*>`.
`hal.h` errors out unless one of `BOARD_STICKS3` / `BOARD_STICKC_PLUS` /
`BOARD_STICKC_PLUS2` is defined.

Two loads the HAL does **not** carry today, which this port must add:

1. **Buttons are not in the HAL.** `src/main.cpp` reads `M5.BtnA` /
   `M5.BtnB` / `M5.BtnPWR` directly (via `board::update()` →
   `M5.update()`). Only the power-button *gestures* are abstracted
   (`powerOffRequested()` / `screenToggleRequested()`).
2. **The display device is not in the HAL.** The global sprite is
   constructed against M5 hardware: `TFT_eSprite spr = TFT_eSprite(&M5.Lcd)`
   (`src/main.cpp:35`), where `src/compat.h` aliases
   `TFT_eSprite = M5Canvas` and `TFT_eSPI = LovyanGFX`.

That second point is also the port's biggest asset: **every pixel of the
~3k-line UI is drawn into one off-screen LovyanGFX sprite** and reaches
the panel at exactly three `spr.pushSprite(0, 0)` sites (boot splash in
`setup()`, `otaDrawProgress()`, and the end-of-frame push in `loop()`,
`src/main.cpp:2889`). `compat.h` already performed one full display-stack
swap (TFT_eSPI → M5GFX) without touching the UI code; this port performs
the second the same way.

### 0.2 Environments (`platformio.ini`)

`[common]` pins `m5stack/M5Unified @ 0.2.17`, `m5stack/M5GFX @ 0.2.22`,
`bitbank2/AnimatedGIF @ 2.1.1`, `bblanchon/ArduinoJson @ 7.4.1`. Envs:
`m5stick-s3` (default, `boards/m5stick-s3.json`, 8MB OTA partitions),
`m5stickc-plus` (4MB, no OTA), `m5stickc-plus2`, `native` (host unit
tests), plus `-ota` extends-variants adding `-DBUDDY_OTA`. Capacity knobs
are per-board in `src/config.h` (`BUDDY_RX_CAP`, `BUDDY_LINEBUF`) and the
8MB boards get `-DBUDDY_EXTRAS_FULL`.

**M5Unified/M5GFX auto-detect M5 boards only.** LilyGO hardware is not in
M5Unified's board table; `M5.begin()` has nothing to detect and `M5.Lcd`
has no panel. LilyGO boards therefore do **not** get M5Unified at all —
they get LovyanGFX standalone (for the sprite type `compat.h` already
aliases) plus real per-peripheral drivers (Section B, E).

### 0.3 Transports today

- **BLE NUS** — `src/ble_bridge.cpp` / `ble_bridge.h`: `bleInit()`,
  `bleConnected()`, `bleSecure()`, `blePasskey()`, `bleAvailable()`,
  `bleRead()`, `bleWrite()`, `bleDisconnect()`, `bleConnParams(relaxed)`,
  pairing-window API (`bleAllowPairing()`, `blePairingTick()`,
  `blePairingRemainingMs()`), bond plumbing (`blePeerBda()`,
  `bleBondKeyBda()`, `bleRemoveCurrentBond()`, `bleClearBonds()`).
- **USB serial** — fed through the same parse core.
- Both are drained in `dataPoll()` (`src/data.h`): `_LineBuf::feed()`
  walks `Serial`, a manual loop walks the BLE ring; each transport owns a
  `ProtoState` (`_usbProto`, `_btProto`) tagged `PROTO_TRANSPORT_USB` /
  `PROTO_TRANSPORT_BT` (`src/proto_parse.h:148`). Hello attribution
  already consumes the tag: `h.viaBle = ps->transport ==
  PROTO_TRANSPORT_BT` (`proto_parse.h:239`).
- **WiFi/TCP device link — host side exists, firmware side pending.**
  `host/src/buddy_bridge/link_tcp.py` implements `DeviceLinkServer`
  (default port 48902): the device is the TCP *client*, first line is the
  HMAC-authenticated `{"hello_link":1,"mac":...,"ts":...,"mac_sig":...}`
  (reference builder `build_hello_link()`, verifier `parse_hello_link()`,
  freshness ±`FRESH_WINDOW_SECS` = 120 s), ack is
  `{"ack":"hello_link","ok":true}`, then a plain newline-JSON pipe with
  semantics identical to BLE. It exposes the LinkManager-parity surface
  the daemon consumes: `send_line`, `on_line`, `connected`,
  `drop_connection`, `stop`, `on_up`/`on_down`.

### 0.4 Screens

`enum DisplayMode { DISP_DASH, DISP_NORMAL, DISP_SESSIONS, DISP_PET,
DISP_CLOCK, DISP_CARDS, DISP_INFO, DISP_COUNT }` plus
`DISP_EXTRAS = DISP_COUNT` parked outside the A-press carousel
(`src/main.cpp:84,281`). Draw functions dispatched at the end of `loop()`
(`src/main.cpp:2864-2889`): `drawDash()`, `drawHUD()` (DISP_NORMAL),
`drawSessions()`, `drawPet()` (+`drawPetStats/Usage/HowTo`),
`drawClockScreen()`/`drawClock()`, `drawCards()`, `drawInfo()`,
`drawExtrasScreen()` (`drawPomodoro()`, `drawIrPage()`), overlays
`drawApproval()`, `drawAsk()`, `drawPasskey()`, `drawMenu()`,
`drawSettings()`, `drawHosts()`, `drawForget()`, `drawReset()`,
`drawToast()`, `otaDrawProgress()`. The GIF pet renders via
`buddyTick()` (`src/buddy.cpp`, AnimatedGIF) when `buddyMode == false`.
Canvas is portrait 135×240 (`const int W = 135, H = 240`,
`src/main.cpp:50`), 16-bit (`spr.setColorDepth(16)` in `setup()`).

---

## 1. Target devices (verified 2026-07-12)

### 1.1 LilyGO T-Deck Pro — A7682E (4G) variant

Verified against the [product page](https://lilygo.cc/products/t-deck-pro),
[LilyGO wiki](https://wiki.lilygo.cc/products/t-deck-series/t-deck-pro/),
[CNX Software](https://www.cnx-software.com/2025/04/03/lilygo-t-deck-pro-esp32-s3-lora-messenger-e-paper-touch-display-keyboard-and-4g-lte-or-audio-codec-option/)
and the vendor repo
[Xinyuan-LilyGO/T-Deck-Pro](https://github.com/Xinyuan-LilyGO/T-Deck-Pro).

- ESP32-S3FN16R8, 16 MB flash / 8 MB PSRAM; WiFi 2.4 GHz + BLE 5.
- 3.1" e-paper **GDEQ031T10**, 320×240, B/W, **CST328 capacitive touch
  panel**. TF card slot.
- **A7682E LTE Cat-1(bis)** modem (the "4G" variant; the alternative
  variant carries a PCM5102A audio DAC instead — on the 4G variant audio
  is routed through the modem). SX1262 LoRa and MIA-M10Q GNSS are onboard
  in all variants per the product page.
- BlackBerry-style QWERTY on a **TCA8418** matrix controller (I2C 0x34).
- BHI260AP IMU, LTR-553ALS light/proximity sensor, mic; **no RTC listed,
  no user LED, no haptic**.
- Power: **BQ25896** charger + **BQ27220** fuel gauge; battery **1400 mAh
  per product page, 1500 mAh per wiki** — flagged, assume 1400.
- Enclosed handheld, 120×66×13.5 mm. **$94.68** live price (shortlist said
  ~$92 — close).
- Vendor firmware stack (their `platformio.ini`): **`platform =
  espressif32@6.5.0`** (same Arduino-2.x core family as our 6.12.0 pin),
  `GxEPD2@1.5.5`, `TinyGSM@^0.12.0`, `Adafruit TCA8418@^1.0.1`,
  `RadioLib@6.4.2`, `lvgl@~8.3.9`, board def `T-Deck-Pro`.

### 1.2 LilyGO T-Deck Max

Verified against the [product page](https://lilygo.cc/products/t-deck-max),
[LilyGO wiki](https://wiki.lilygo.cc/products/t-deck-series/t-deck-max/) and
[LinuxGizmos](https://linuxgizmos.com/lilygo-t-deck-max-is-an-esp32-s3-handheld-with-lora-gps-and-e-paper/).

- Same core: ESP32-S3, 16 MB / 8 MB, WiFi + BLE 5, GDEQ031T10 320×240
  e-paper — here **front-lit** ("3.1 inch Front-light Low Power E-Paper",
  product page).
- **Everything-radio**: SX1262 LoRa (+22 dBm; 433/868/915/920 variants)
  **and** A7682E 4G **and** MIA-M10Q GPS, simultaneously.
- QWERTY on TCA8418. **No trackball** — the shortlist premise
  "QWERTY+trackball" is wrong; no vendor source shows one (the trackball
  belongs to the older LCD T-Deck/Plus). LinuxGizmos instead lists
  "three touch buttons"; the wiki lists a CST3530 touch controller (I2C
  0x1A). Whether CST3530 serves a touch *panel* or only the three touch
  *buttons* is genuinely ambiguous across sources — treat panel-touch as
  ABSENT until hardware confirms (LinuxGizmos: no touch display).
- Audio: **ES8311 codec** (same codec M5StickS3 uses) + PA, speaker
  *shared* with the A7682E and switched by an **XL9555 IO expander**;
  **DRV2605 haptic driver** (I2C 0x5A). BHI260AP IMU. TF card.
- Power: **SY6970** charger + **BQ27220** gauge; battery **1500 mAh per
  wiki, 1400 mAh (305070) per LinuxGizmos** — flagged, assume 1400–1500.
- Enclosed handheld. **$109.87** live price. No RTC listed.

### 1.3 LilyGO T5 E-Paper S3 Pro

Verified against the
[product page](https://lilygo.cc/en-us/products/t5-e-paper-s3-pro),
[Hackster](https://www.hackster.io/news/lilygo-s-t5-e-paper-s3-pro-pairs-espressif-s-esp32-s3-with-4-7-display-and-wireless-charging-da589d328542)
and vendor repos
[T5S3-4.7-e-paper-PRO](https://github.com/Xinyuan-LilyGO/T5S3-4.7-e-paper-PRO),
[LilyGo-EPD47](https://github.com/Xinyuan-LilyGO/LilyGo-EPD47).

- ESP32-S3-WROOM-1, 16 MB / 8 MB. 4.7" **ED047TC1** e-paper, **960×540,
  16 grayscale levels**, two-point capacitive touch, ~234 PPI.
- **PCF8563 RTC** (the same register map as the BM8563 in the StickC
  Plus/Plus2). BQ25896 charger + BQ27220 gauge. TF card.
- **SX1262 LoRa onboard**; GPS optional per variant (with/without ×
  433/868/915/920 MHz). Wireless (MagSafe-style) charging coil per
  Hackster.
- **No keyboard, no speaker.** Battery not included/specified (JST-style
  pack expected) — flagged.
- Form factor: display module with mounting frame — **not an enclosed
  consumer handheld**; sold as a dev kit (Hackster calls it a handheld,
  the product page a development board — flagged; assume desk-stand or
  printed case needed). **$84.21** live price.
- Display driver stack is **not** GxEPD2: ED047TC1 is a parallel-bus
  panel driven by **LilyGo-EPD47** (LilyGO's fork of
  [vroland/epdiy](https://github.com/vroland/epdiy)).

### 1.4 LilyGO T5 E-Paper S3 (base) / "S3 Lite"

Two adjacent products satisfy the shortlist's fourth slot; both share the
ED047TC1/EPD47 display stack so the port work is identical:

- **T5 e-Paper 4.7" V2.3 (S3)** —
  [product page](https://lilygo.cc/products/t5-4-7-inch-e-paper-v2-3):
  ESP32-S3-WROOM-1-N16R8, ED047TC1 540×960 16-gray, PCF8563 RTC, touch
  and non-touch variants, bare dev board, battery not included,
  **$39.99**. The page carries LilyGO's own warning that the panel
  "cannot be partially refreshed for a long time" without a full-refresh
  interleave — direct input for our refresh policy (B.3).
- **T5 E-Paper S3 Lite** — currently documented only in LilyGO's
  [documentation repo](https://github.com/Xinyuan-LilyGO/documentation/blob/master/en/products/t5-series/t5-e-paper-s3-lite/index.md):
  same ED047TC1 4.7" 540×960 + ESP32-S3-WROOM-1-N16R8 + PCF8563, adds
  **GT911 two-point touch, AG anti-glare glass and a warm front-light**,
  drops GPS/LoRa vs the Pro, JST-PH 2.0 battery connector, 121×67×12 mm.
  **Price/availability unverified** (a third-party directory lists ~$71;
  no lilygo.cc store page found) — flagged. Spec below treats "T5 base"
  as: buy whichever of these two is purchasable at port time.

### 1.5 Comparison table

| | T-Deck Pro (4G) | T-Deck Max | T5 S3 Pro | T5 S3 base/Lite |
|---|---|---|---|---|
| Price (live 2026-07-12) | $94.68 | $109.87 | $84.21 | $39.99 (V2.3) / ~$71 unverified (Lite) |
| Panel | GDEQ031T10 3.1" 320×240 B/W | same, **front-lit** | ED047TC1 4.7" 960×540 16-gray | same; Lite adds warm front-light |
| Touch | CST328 panel touch | 3 touch buttons; panel touch unconfirmed | 2-point capacitive | Lite: GT911; V2.3: touch variant |
| Display lib | GxEPD2 (`GxEPD2_310_GDEQ031T10`) | GxEPD2 (same) | LilyGo-EPD47 (epdiy) | LilyGo-EPD47 (epdiy) |
| MCU / mem | S3FN16R8 16/8 | S3 16/8 | S3-WROOM-1 16/8 | S3-WROOM-1-N16R8 16/8 |
| Keyboard | QWERTY (TCA8418) | QWERTY (TCA8418) | — | — |
| Cellular | A7682E Cat-1 | A7682E Cat-1 | — | — |
| LoRa / GPS | SX1262 / MIA-M10Q | SX1262 / MIA-M10Q | SX1262 / optional | — |
| Audio / haptic | spk behind modem; mic | ES8311 + PA + spk (XL9555-switched) + DRV2605 haptic | — | — |
| PMU | BQ25896 + BQ27220 | SY6970 + BQ27220 | BQ25896 + BQ27220 | unspecified (Lite), ADC sense (V2.3) |
| RTC | none listed | none listed | PCF8563 | PCF8563 |
| Battery | 1400 mAh (wiki says 1500) | 1400–1500 mAh | not included | not included |
| Enclosure | enclosed handheld | enclosed handheld | frame/dev-kit (flagged) | bare board (V2.3) / cased-ish 121×67×12 (Lite) |
| Port effort (G) | HIGH (first mover) | LOW-MED after Pro | MED | LOW after T5 Pro |

---

## A. Transport abstraction

### A.1 What exists, what's pending, what's new

Today the firmware speaks **BLE NUS + USB serial** simultaneously
(`dataPoll()`, dual-write `protoEmit()`). The host already ships the
**WiFi/TCP device link** server (`link_tcp.py` `DeviceLinkServer`); the
firmware TCP *client* is the pending counterpart and is a prerequisite of
this port (the T5 boards have no cellular, and WiFi/TCP is the natural
desk transport for a mains-powered e-ink dashboard). This spec adds a
**third radio transport: cellular MQTT** for the A7682E T-Decks — both
the device and the bridge connect *outbound* to a broker, so neither end
needs a reachable address; payloads stay newline-JSON protocol lines.

Nothing in `PROTOCOL_V2.md` changes. The additive firmware-side work is:
two new `ProtoState` transport tags (`PROTO_TRANSPORT_TCP = 2`,
`PROTO_TRANSPORT_MQTT = 3` in `src/proto_parse.h`), two new line feeders
in `dataPoll()` following the `_btLine`/`_btProto` pattern, and
`protoHelloAccept()` / `hostGlueOnHello()` widening `viaBle` into a
transport enum (today `proto_parse.h:239` computes `h.viaBle`).

### A.2 The link interface (firmware)

Every transport satisfies one interface, `src/link/link.h`, whose method
names deliberately parallel both `ble_bridge.h` and `DeviceLinkServer`:

```cpp
struct BuddyLink {                    // one instance per transport backend
  bool        begin();                // bleInit / tcpLinkInit / mqttLinkInit
  void        tick(uint32_t nowMs);   // reconnect backoff, keepalive, blePairingTick-style housekeeping
  bool        connected();            // bleConnected / DeviceLinkServer.connected
  size_t      available();            // bleAvailable
  int         read();                 // bleRead      (byte stream, '\n'-framed by data.h)
  size_t      write(const uint8_t* p, size_t n);   // bleWrite / send_line
  void        disconnect();           // bleDisconnect / drop_connection
  void        setRelaxed(bool);       // bleConnParams(relaxed) analog: TCP keepalive stretch / MQTT cadence
  const char* name();                 // "usb" / "ble" / "tcp" / "mqtt" — feeds dataScenarioName()
};
```

`ble_bridge.cpp` keeps its concrete API (bond/pairing surface is
BLE-only); a thin `link_ble.cpp` adapter maps it onto `BuddyLink`.
New backends:

- **`src/link/link_tcp.cpp`** (firmware mirror of `link_tcp.py`): joins
  WiFi via the existing `wifiJoinBest()` (`src/wifi_store.h`, LittleFS
  list provisioned over `{"cmd":"wifi",...}`), dials
  `[device_link] host:port` (provisioned via a new
  `{"cmd":"link","host":...,"port":...,"token":...}` — token delivered
  only over the encrypted BLE pipe, matching link_tcp.py's security
  model), sends `build_hello_link()`'s exact frame
  (`{"hello_link":1,"mac":"<bt-mac>","ts":<epoch>,"mac_sig":"<hmac>"}`,
  HMAC-SHA256 over canonical JSON minus `mac_sig` — mbedTLS provides the
  HMAC), waits for `{"ack":"hello_link","ok":true}`, then streams lines.
  Reconnect with backoff; the ±120 s `ts` freshness needs a valid wall
  clock — `board::clockValid()` gates dialing (PCF8563 on the T5s; SNTP
  on WiFi join or modem `AT+CCLK` on the T-Decks, see D/E).
- **`src/link/link_mqtt.cpp`** (cellular): TinyGSM (`TinyGsmClient` over
  the A7682E — the vendor repo already pins `TinyGSM@^0.12.0` and ships
  `examples/A7682E/test_AT`) + an MQTT client (PubSubClient or
  ArduinoMqttClient) + TLS via the modem's native SSL stack (preferred —
  offloads the handshake) or mbedTLS over the TCP client. Two topics per
  device, device-id = BT MAC (`buddy/<mac>/d2h` device→host,
  `buddy/<mac>/h2d` host→device), one protocol line per PUBLISH, QoS 1,
  no retain, LWT on `buddy/<mac>/stat` for presence. Broker credentials
  provisioned like the TCP token: over the encrypted BLE link only.

Security note, stated honestly: BLE is link-encrypted; the TCP link is
HMAC-gated plaintext on a trusted LAN (link_tcp.py's documented model);
**MQTT adds a third party (the broker) that sees every prompt and
decision** — so the spec requires TLS with CA validation (the
`setInsecure()` tradeoff documented for OTA in `src/ota/ota.cpp` is NOT
acceptable here) and recommends a self-hosted broker (Mosquitto) over a
public one. An optional later hardening mirrors `relay.py`'s
`sign_frame()` per-message HMAC end-to-end so the broker becomes
untrusted pipe only.

### A.3 Auto-select + failover ladder

One *active* radio transport at a time (USB serial is always-on and
free). Selection runs in a small pure-logic module
(`src/logic/link_select_logic.h`, native-testable like
`pairing_gate_logic.h`):

1. **USB powered + LAN provisioned → WiFi/TCP.** Mains covers WiFi's
   power cost; lowest-latency prompt delivery; frees the BLE slot.
2. **On battery + bonded host in range → BLE.** Cheapest radio
   (`bleConnParams(relaxed)` idle ≈ 1–2 mA class); the today-default.
3. **No LAN/BLE + cellular fitted → MQTT.** Away mode, duty-cycled
   (A.5).

Demotion/promotion with 30–60 s hysteresis on link loss (mirrors the
existing 30 s liveness rule in `dataConnected()`); `sel`/soft-pin host
arbitration is untouched because the host registry is hello-scoped, not
transport-scoped. The bridge side already arbitrates BLE-pause/resume via
`DeviceLinkServer`'s `on_up`/`on_down` — the MQTT listener plugs into the
same hooks.

### A.4 Bridge-side MQTT listener

`host/src/buddy_bridge/link_mqtt.py`, class `DeviceMqttLink`, exposing
**exactly** `DeviceLinkServer`'s LinkManager-parity surface (`send_line`,
`on_line`, `connected`, `drop_connection`, `stop`, `on_up`, `on_down`,
`status()`) so `daemon.py` treats it as one more device pipe. It connects
outbound to the configured broker (asyncio-mqtt/paho), subscribes
`buddy/<mac>/d2h`, publishes `buddy/<mac>/h2d`, treats the device LWT
topic as the up/down edge. Config block `[device_link_mqtt]` (broker,
port, TLS CA, per-device credentials), disabled unless fully configured —
the `link_tcp.py` "no socket unless enabled AND token non-empty" rule,
transplanted.

### A.5 LTE data + power budget (A7682E, 1400–1500 mAh)

Assumptions: heartbeat line with sessions[] ≈ 300–500 B
(`BUDDY_LINEBUF`-bounded); MQTT+TLS+TCP overhead ≈ +100 B per publish;
QoS 1 ack ≈ 90 B down. Order-of-magnitude figures — bench before pricing
a SIM plan:

| Strategy | Airtime pattern | ~Data/month |
|---|---|---|
| Naive: full heartbeat every 10 s (BLE cadence) | 8,640 msg/day | ~130–150 MB |
| Relaxed: heartbeat every 60 s | 1,440 msg/day | ~20–25 MB |
| **Spec'd: event-driven + keepalive** — publish only prompts/asks/session *transitions*; MQTT PINGREQ (2 B + overhead) every 60–300 s carries liveness | ~100 events + 288–1,440 pings/day | **~2–8 MB** |

The event-driven mode is a *bridge-side* cadence policy (the device never
required a fixed heartbeat — 30 s silence just means "host dead", and
over MQTT the LWT + pings replace that signal), so it needs no firmware
protocol change — the bridge simply sends fewer lines on the MQTT pipe.

Power (vendor-class figures for Cat-1bis, flag: verify on hardware):
modem idle-registered ~15–25 mA, `AT+CSCLK` sleep ~1–2 mA, data bursts
100–300 mA with TX peaks toward 2 A (rail design is LilyGO's problem, but
brownout-during-BLE-coex is ours to test). Three duty modes on a 1400 mAh
pack, e-ink static and ESP32 mostly sleeping:

| Mode | Modem | Prompt latency | Est. runtime |
|---|---|---|---|
| Resident | registered + CSCLK sleep, MQTT keepalive 300 s | seconds | ~4–7 days |
| Duty-cycled | off; wake every 15 min, drain queue, sleep | ≤ 15 min | ~2 weeks |
| Off (desk) | powered down (`board::modemPower(false)`) | n/a | BLE/TCP budget |

---

## B. Display / e-ink — the biggest item

### B.1 The render seam we already have

All UI code draws into `spr` (a `TFT_eSprite` = `M5Canvas` =
`lgfx::LGFX_Sprite`) and the frame reaches hardware at
`spr.pushSprite(0, 0)` once per loop pass (`src/main.cpp:2889`; loop
`delay(screenOff ? 100 : 16)` — i.e. up to ~60 fps awake). E-paper cannot
take that push rate; the port therefore replaces the *push*, not the
*drawing*:

- `compat.h` gains a non-M5 branch: `TFT_eSprite` aliases LovyanGFX's
  standalone `LGFX_Sprite` (constructed parentless; the canvas is pure
  PSRAM memory — no panel needed to draw).
- The three `pushSprite` sites become **`board::present(spr, hint)`**
  (Section E). On LCD backends it is exactly `spr.pushSprite(0,0)`. On
  e-ink backends it (a) converts the 16-bit canvas to the panel format
  (threshold to 1-bit for GDEQ031T10; RGB565→luma→4-bit for ED047TC1),
  (b) diffs against the previous presented frame and **skips identical
  frames** — this alone absorbs the 16 ms loop cadence, because the UI
  already repaints the same pixels between state changes, and
  (c) schedules a partial or full refresh per the policy below.

### B.2 Per-board display stacks

- **T-Deck Pro / Max (GDEQ031T10, SPI):** `GxEPD2` — mainline class
  `GxEPD2_310_GDEQ031T10` ([ZinggJM/GxEPD2](https://github.com/ZinggJM/GxEPD2)),
  and the exact library+version LilyGO's own firmware uses
  (`zinggjm/GxEPD2@1.5.5` in the T-Deck-Pro repo). B/W only. Partial
  refresh supported by the panel class; quality varies with temperature —
  verify waveform behavior on hardware.
- **T5 S3 Pro / base (ED047TC1, parallel):**
  [LilyGo-EPD47](https://github.com/Xinyuan-LilyGO/LilyGo-EPD47)
  (epdiy-derived). 16 grayscale, fast partial updates, but LilyGO's own
  product page warns against long runs of partials without a clearing
  full refresh (ghosting/damage) — the policy below is not optional.
- LovyanGFX itself drives neither panel today; we use it only for the
  off-screen sprite + text/primitive API the UI is written against. (If a
  LovyanGFX `Panel_*` for these panels lands upstream later, `present()`
  collapses; do not block on it.)

### B.3 Refresh policy (`board::present` + `board::displayFullRefresh`)

- **Partial refresh** for ordinary frame changes (rows changing state,
  cursor moves, toast). Budget ≈ 300–500 ms on GDEQ031T10, faster on
  EPD47 — measured, not promised.
- **Full refresh** (the black/white flash): every N partials (start
  N = 10) or T minutes (start 10 min), whichever first; immediately on
  `applyDisplayMode()` screen switches (full-canvas repaints anyway);
  always before `screenPower(false)`.
- **Ghosting-sensitive surfaces** (inverted/filled regions like
  `drawDash()`'s amber waiting rows) get a full refresh one step sooner.
- `PresentHint` from the call site: `PRESENT_NORMAL`,
  `PRESENT_URGENT` (approval overlay — partial now, skip coalescing),
  `PRESENT_STATIC` (clock minute tick — allow deeper panel sleep after).

### B.4 Screen-by-screen: adapt / degrade / drop

E-ink flips the design center from "animated pet with screens" to
"**dashboard that is always readable**" — `DISP_DASH` becomes the default
home (it already is: `settings().home == 1 ? DISP_NORMAL : DISP_DASH`,
`src/main.cpp:2223`) and the flagship surface: its content changes only
on heartbeat/session transitions, a perfect partial-refresh cadence, and
e-ink keeps it visible at zero power.

| Surface | On e-ink |
|---|---|
| `DISP_DASH` | **Keep, first-class.** Waiting-row amber fill → black-fill + white text (B/W) or dark-gray band (16-gray). Bottom marquee ticker (`marqueeOffset`) → static truncated line, refreshed on `tama.lineGen` change only. |
| `DISP_NORMAL` (HUD + character + overlays) | Keep. ASCII character (`characterTick()`) redraws only on state change — add a dirty-gate so persona blinks don't tick partials (blink animation disabled on e-ink). |
| **GIF pet** (`buddyTick()`, AnimatedGIF) | **Drop on e-ink.** Frame-rate animation is ghosting + waveform abuse. `gifAvailable` forced false on EINK backends (compile-time), `nextPet()` cycles ASCII species only; the pet's *pose* updates on persona change. AnimatedGIF stays out of those builds. |
| `DISP_SESSIONS`, `DISP_CARDS`, `DISP_INFO` | Keep as-is — page-oriented, change-driven. |
| `DISP_CLOCK` / `drawClockScreen()` | Keep — 1 partial/min, full on the hour. The classic e-ink win. Landscape direct-to-LCD path (`drawClock(withStrip)` via `TFT_eSPI*`) routes through the same present seam. |
| `DISP_EXTRAS` (`drawPomodoro()`, `drawIrPage()`) | Pomodoro: keep (1/min + phase edges). IR page: N/A (no IR hardware on these boards — `BUDDY_EXTRAS_FULL` splits, see E). |
| `drawApproval()` / `drawAsk()` / `drawPasskey()` | Keep, `PRESENT_URGENT`. Attention comes from sound/haptic/front-light pulse instead of animation (Max: DRV2605 + frontlight; Pro: display flash — see D). |
| `drawToast()` | Keep; coalesce into the next partial. |
| `otaDrawProgress()` | Percent bar throttled to 5–10% steps on e-ink (it already dedupes repeat (status,pct) pairs in `ota.cpp`). |
| screen-off | `screenPower(false)` = panel deep-sleep with image retained: draw a small "asleep" glyph + last state, then sleep. A stale dashboard must *look* stale — spec: sleep glyph mandatory. |

Canvas geometry: keep the portrait 135×240 UI as-is for phase 1 on the
T-Decks (centered letterbox on 240×320 portrait — pixel-double where it
helps), then a layout pass to native 320×240 landscape for `drawDash()` /
`drawSessions()` (the session-row layout in `ui_layout_logic.h` is where
W/H assumptions live; `const int W, H` in `main.cpp:50` becomes
per-board `BUDDY_DISP_W/H` in `config.h`). The 960×540 T5 renders the
same canvas 2× and reserves the freed area for a fuller dashboard later.
Honest cost: the W/H constants leak into most `draw*` functions —
mechanical but wide (this is the "real display work" item).

### B.5 Color → e-ink mapping

Palette vocabulary today (`src/main.cpp:55-60` + `characterPalette()`):
`AMBER` waiting, `GREEN` running, `DONEBLU` done, `HOT` warnings, dim
idle. B/W panels: waiting = black-filled row + white text (highest
contrast wins the "come look" job the big amber count does today),
running = bold/outlined, done/idle = plain/dotted. 16-gray panels: map
palette luminance (amber→dark gray, green→mid, dim→light). Mapping lives
in `characterPalette()`'s palette table gated by a new
`BUDDY_DISPLAY_EINK`/`BUDDY_DISPLAY_GRAY16` capability define — draw
call-sites unchanged.

---

## C. Input

### C.1 Today

Two buttons + power button, read directly from M5Unified in `loop()`:
`M5.BtnA` short = approve `{"decision":"once"}` / carousel next; long =
menu. `M5.BtnB` short = deny / select / page; long = context action
(session pin, card dismiss, sessions shortcut) (`src/main.cpp:2575-2745`).
Power gestures via `board::powerOffRequested()` /
`screenToggleRequested()`. Plus IMU gestures (`gesture_logic.h`: flip =
host switch, double-tap = wake; shake in `checkShake()`) — the BHI260AP
covers `imuAccel()` on all four targets... minus the T5s, which have **no
IMU** (flag: gestures become no-ops there).

### C.2 The input seam (new — buttons enter the HAL)

`main.cpp` stops touching `M5.Btn*`; a per-board event queue replaces it:

```cpp
enum InputEvent : uint8_t {
  EV_NONE, EV_A_SHORT, EV_A_LONG, EV_B_SHORT, EV_B_LONG,
  EV_NAV_NEXT, EV_NAV_PREV, EV_OK, EV_BACK,        // richer inputs collapse here
  EV_TEXT,                                          // + char payload (QWERTY)
};
bool board::inputPoll(InputEvent* ev, char* ch);    // drained once per loop()
```

M5 backends synthesize `EV_A_*`/`EV_B_*` from `M5.BtnA/BtnB` preserving
the exact `pressedFor(600)`/`wasReleased()`/`swallowBtnA` semantics
(logic moves verbatim from `loop()` into `hal_*`). The dispatch ladder in
`loop()` keys off events instead of buttons — a mechanical rewrite of
`main.cpp:2575-2745`, no behavior change on M5 boards.

### C.3 Per-device mapping

- **T-Deck Pro / Max QWERTY (TCA8418, `Adafruit TCA8418` lib, INT-driven):**
  Enter/`y` → `EV_A_SHORT` (approve), Backspace/`n` → `EV_B_SHORT`
  (deny), Space/→ → `EV_NAV_NEXT` (carousel), `m` or held Enter →
  `EV_A_LONG` (menu), held Backspace → `EV_B_LONG`. All other printable
  keys emit `EV_TEXT`. Deny-on-Backspace is deliberate-but-cheap — same
  one-press weight as BtnB today; risk noted in H.
- **T-Deck Pro touch panel (CST328):** tap right-half = A, left-half =
  B, long-press = long variants, swipe = carousel. Secondary to keys.
- **T-Deck Max:** the three capacitive touch buttons map 1:1 to
  A / B / power-gesture; panel touch only if hardware confirms it.
- **T5 Pro / Lite (2-pt touch, GT911 or equivalent):** touch is the
  *only* input — tap zones as above, swipe next/prev, two-finger tap =
  menu. No keyboard → no `EV_TEXT`.

### C.4 The unlock: on-device answering + text entry

Protocol v2.0 pins `ask` as **read-only** — `PROTOCOL_V2.md`: "No
on-device answering path exists in v2.0… A future protocol revision may
add an answer message." A QWERTY device makes that revision worth
speccing now (as **v2.1, capability-gated, additive** — consistent with
"every v2 field is optional"):

- Device hello-ack caps gain `"askanswer"`. When both sides negotiate it:
  - `{"cmd":"ask_answer","id":"ask_xyz789","option":2}` — choice pick
    (index into the displayed `questions[0].options`; `drawAsk()`'s
    cursor already scrolls options via `askCursor`).
  - `{"cmd":"ask_answer","id":"...","text":"..."}` — free-text via
    `EV_TEXT` + a new on-device line-editor overlay (`drawTextEntry()`),
    length-capped to the negotiated `maxLine` budget.
  - Host acks `{"ack":"ask_answer","ok":true}`; cancel semantics reuse
    `ask_cancel`. Bridge maps it onto the CLI's AskUserQuestion answer.
- **Owner + WiFi entry on-device** (no protocol change): today owner
  name arrives only via bridge push and WiFi via `{"cmd":"wifi"}` from
  the host. With a keyboard, `settings > wifi` grows "add network…"
  (SSID/pass typed locally into `wifiStoreUpsert()` — passwords still
  never echoed, matching `data.h`'s no-logging rule) and first-boot can
  ask the owner's name directly.

---

## D. Power / PMU

### D.1 Per-board reality vs the `board::` battery API

| hal.h function | T-Deck Pro | T-Deck Max | T5 Pro | T5 base/Lite |
|---|---|---|---|---|
| `batteryPercent()` | BQ27220 StateOfCharge (real gauge — better than today's 3.2–4.2 V linear estimate in `hal_common.cpp`) | same | same | ADC divider estimate (no gauge listed) |
| `batteryMilliVolts()` | BQ27220 Voltage() | same | same | ADC |
| `hasBatteryCurrent()` / `batteryCurrentMa()` | **true** — BQ27220 AverageCurrent (signed) | true | true | false |
| `hasCoulomb()` | **false** — BQ27220's RemainingCapacity is absolute, not AXP192-style net-since-clear; keep the honest `false` rather than fake the semantics | false | false | false |
| `onUsb()` / `usbMilliVolts()` / `isCharging()` | BQ25896 VBUS/PG + CHRG status regs | SY6970 equivalents | BQ25896 | charger unspecified — GPIO sense |
| `hasInternalTemp()` / `internalTempC()` | BQ27220 temp or S3 die (`temperatureRead()`, the `hal_sticks3.cpp` precedent) | same | same | S3 die |
| `powerOff()` | charger **ship-mode** (BATFET disable: BQ25896 REG09[5] / SY6970 equivalent) on battery; deep-sleep on USB — the exact split `hal.h` documents for the S3 | same | same | deep-sleep only |

Charger/gauge drivers: small register-level I2C units (the LilyGO vendor
repos carry working init sequences to crib from); no XPowersLib (that's
AXP-family only).

### D.2 Clock

- **T5 Pro / base: PCF8563** — same register map as the BM8563 the
  Plus/Plus2 already use, so `setClock()/getClock()/clockValid()` port
  1:1 (without M5Unified's `M5.Rtc` wrapper — a ~100-line I2C driver).
- **T-Deck Pro / Max: no RTC.** Reuse the S3's `SoftClock` path
  (`softClockSeed`/`softClockGet`, `logic/clock_time_logic.h`) — plus two
  new seeds the S3 never had: SNTP once per WiFi join, and modem
  `AT+CCLK?` when cellular attaches. That also satisfies `link_tcp`'s
  ±120 s `hello_link` freshness away from the bridge (A.2).

### D.3 Sleep ladder (e-ink changes the game)

Today: awake 16 ms loop → 30 s idle `screenPower(false)` + downclock to
`BUDDY_SCREEN_OFF_CPU_MHZ` + `bleConnParams(true)` (`src/main.cpp:2918-2934`).
E-ink adds:

- **Screen-off ≠ blank**: panel deep-sleeps retaining the last frame
  (~0 µA panel). The wake render is a full refresh (~1–2 s) — acceptable
  because prompts force `PRESENT_URGENT` partials from panel-on state
  before idle kicks in.
- **Deep-sleep mode (cellular duty-cycle, A.5):** `esp_deep_sleep` with
  RTC-GPIO wake on TCA8418 INT / touch INT / timer. BLE bonds survive in
  NVS (Bluedroid store), the link itself does not — reserved for the
  away/MQTT profile where BLE is already down. Not used while BLE-resident
  (the existing light-path stays for that).

---

## E. HAL additions (exact names)

New `namespace board` functions (all with no-op/false defaults on M5
backends so existing boards compile unchanged):

```cpp
// display (B)
enum DisplayKind : uint8_t { DISPLAY_LCD, DISPLAY_EINK_BW, DISPLAY_EINK_GRAY16 };
enum PresentHint : uint8_t { PRESENT_NORMAL, PRESENT_URGENT, PRESENT_STATIC };
DisplayKind displayKind();
void  present(TFT_eSprite& spr, PresentHint hint);  // replaces raw spr.pushSprite(0,0)
void  displayFullRefresh();                          // force the clearing flash
void  frontlight(uint8_t v0_255);                    // Max/T5-Lite; no-op elsewhere

// input (C)
bool  inputPoll(InputEvent* ev, char* ch);           // buttons/keys/touch, unified

// cellular modem lifecycle (A)
bool  hasModem();                                    // compiled per BOARD_*
void  modemPower(bool on);                           // PWRKEY sequencing
bool  modemReady();                                  // registered + PDP up
int   modemRssi();                                   // for DISP_INFO / dashboard
bool  modemClock(struct tm* out);                    // AT+CCLK -> SoftClock seed

// attention (C/B — replaces LED-only vocabulary)
void  attentionPulse(uint8_t kind);                  // Max: DRV2605 haptic; Pro: display flash
                                                     // + frontlight blink; M5: attentionLed
```

Per-device backends and build plumbing, following the existing pattern
exactly (`hal_sticks3.cpp` + `boards/m5stick-s3.json` + `[env:m5stick-s3]`):

| Device | flag | HAL file | board def | env |
|---|---|---|---|---|
| T-Deck Pro 4G | `BOARD_TDECK_PRO` | `src/hal/hal_tdeck_pro.cpp` | `boards/t-deck-pro.json` (vendored from the LilyGO repo's `T-Deck-Pro` def) | `[env:t-deck-pro]` |
| T-Deck Max | `BOARD_TDECK_MAX` | `src/hal/hal_tdeck_max.cpp` | `boards/t-deck-max.json` | `[env:t-deck-max]` |
| T5 S3 Pro | `BOARD_T5S3_PRO` | `src/hal/hal_t5s3_pro.cpp` | `boards/t5-epaper-s3-pro.json` | `[env:t5-epaper-s3-pro]` |
| T5 S3 base/Lite | `BOARD_T5S3_LITE` | `src/hal/hal_t5s3_lite.cpp` | `boards/t5-epaper-s3-lite.json` | `[env:t5-epaper-s3-lite]` |

- `hal.h`'s no-board-flag `#error` grows the four new flags; `config.h`
  gains per-board `BUDDY_DISP_W/H`, S3-class `BUDDY_RX_CAP 8192` /
  `BUDDY_LINEBUF 4608`, and the display-kind defines (B.5).
- `hal_common.cpp` is M5Unified-only → guard it under the three M5 flags;
  LilyGO boards get `hal_common_lgfx.cpp` for the shared
  sprite/`present()` conversion code. `compat.h` branches its aliases on
  the same guard (0.1/B.1).
- Per-env `lib_deps` extend `[common]` minus M5Unified/M5GFX, plus:
  T-Decks: `zinggjm/GxEPD2@1.5.5`, `adafruit/Adafruit TCA8418@^1.0.1`,
  `vshymanskyy/TinyGSM@^0.12.0` (+ MQTT client lib); T5s:
  `xinyuan-lilygo/LilyGoEPD47`. `AnimatedGIF` dropped from EINK envs
  (B.4). LoRa's `RadioLib` only under the future `BUDDY_LORA` flag (F).
- Partitions: new `partitions/lilygo_16mb_ota.csv` (16 MB: two ~6.5 MB
  OTA slots + ~2.5 MB LittleFS) alongside `sticks3_8mb_ota.csv`.
- OTA: `src/ota/ota.cpp` `OTA_BOARD_SLUG` grows `"tdeckpro"`,
  `"tdeckmax"`, `"t5pro"`, `"t5lite"` → release assets
  `firmware-<slug>.bin`, exactly the multi-board mechanism the slug-probe
  fallback was built for. Note: OTA over cellular ≈ 1.5–2 MB per update —
  gate the menu row on `!linkIsMqtt()` or warn.
- `BUDDY_EXTRAS_FULL` splits: tunes stay, `ir/ir_remote.h` (RMT IR) gets
  its own `BUDDY_IR` flag — no IR hardware on any LilyGO target.

---

## F. Radio extras — future, optional modules

- **LoRa (SX1262 — on T-Deck Pro, Max, T5 Pro):** out of scope for the
  port phases; compiled only under a future `BUDDY_LORA` flag
  (`RadioLib@6.4.2`, the vendor-proven pick). The stretch idea worth
  keeping on the board: **buddy-over-LoRa prompt pager** for
  no-cell/no-WiFi terrain — but newline-JSON does not fit LoRa airtime
  (51–222 B payloads at usable SFs + 1% EU duty cycle), so it would carry
  a *compact framing* of prompts/decisions only (len-prefixed CBOR of
  `id`/`hint`/`decision`), NOT the full protocol; bridge side would need a
  LoRa gateway device. Explicitly a v3 exploration, not a port
  deliverable.
- **CC1101 sub-GHz:** present on **none** of the four boards (it lives on
  LilyGO's T-Embed CC1101 line) — if ever wanted, it arrives as an
  external SPI/Qwiic module. Parked; one sentence is all it earns until
  hardware exists.

## G. Effort + phasing

Shared work (once, benefits all four + the M5 boards):
**T1 transport interface + firmware TCP client** (A.2 — also closes the
pending `link_tcp.py` counterpart for M5 boards), **T2 MQTT transport +
`link_mqtt.py`**, **S1 present/render seam** (B.1/B.3), **S2 input seam**
(C.2), **S3 palette/e-ink mapping** (B.5), **S4 16 MB partitions + OTA
slugs** (E). Per-device work: display bring-up, keyboard/touch map, PMU
driver, board json, audio/attention.

| Device | Effort | Why |
|---|---|---|
| **T-Deck Pro (4G)** | **HIGH** (first mover — carries S1+S2+T2 and the GxEPD2/TCA8418/BQ bring-up) | Everything is new once: e-ink present pipeline, keyboard, gauge/charger, cellular. Mitigated by the vendor repo proving GxEPD2 1.5.5 + TinyGSM + TCA8418 together on espressif32 6.5.0 (same core family as our 6.12.0 pin). Audio caveat: speaker hangs off the modem — ship v1 silent with `attentionPulse()` display-flash, treat sound as stretch (H). |
| **T-Deck Max** | **LOW-MED** (delta on Pro) | Same panel/keyboard/gauge; deltas: SY6970 vs BQ25896, ES8311+PA audio (restores the full `soundEvent()` vocabulary — M5StickS3 uses the same codec), DRV2605 haptic, frontlight, touch buttons. |
| **T5 S3 Pro** | **MED** | New display stack (EPD47/epdiy 16-gray) + touch-only input backend; PCF8563 + BQ pair are ports of known quantities; no keyboard/cell work. |
| **T5 base/Lite** | **LOW** (delta on T5 Pro) | Drop LoRa/GPS, add GT911 + frontlight (Lite) or nothing (V2.3). Cheapest hardware, good "wall dashboard" build. |

Phase order (each phase ships a releasable firmware):

1. **P0 — seams on existing hardware.** S1+S2 land as refactors verified
   on `m5stick-s3` (behavior-identical: `present()` = pushSprite, events
   = M5 buttons) + `native` tests for the new pure logic
   (`link_select_logic.h`). De-risks everything downstream.
2. **P1 — T1 firmware TCP client** (M5 boards benefit immediately; T5
   desk mode depends on it).
3. **P2 — T-Deck Pro bring-up**: display → input → PMU → BLE+TCP parity.
   *Recommended first device* — it exercises every shared seam, it's
   enclosed (daily-drive-able), and Max/T5 become deltas.
4. **P3 — cellular**: T2 MQTT transport + `link_mqtt.py` + failover
   ladder + data/power budget bench on the Pro.
5. **P4 — T-Deck Max** (delta port; restores audio, adds haptic).
6. **P5 — T5 S3 Pro**, **P6 — T5 base/Lite** (EPD47 stack, touch input,
   big-dashboard layout pass from B.4).
7. **P7 (stretch)** — v2.1 `askanswer` + on-device text entry (C.4);
   LoRa exploration (F).

## H. Risks

1. **M5Unified non-support on LilyGO** (the structural risk): today
   `hal.h` states "All supported boards run on M5Unified" — that
   assumption dies here. Exposure beyond the HAL: `spr(&M5.Lcd)`
   (`main.cpp:35`), `M5.BtnA/B/PWR` ladder (`main.cpp:2575-2745`),
   `M5.Display` uses in `setup()`/boot-trace, `M5.Rtc` in
   `hal_stickc_plus*.cpp`, `M5.Imu/Power/Speaker` in `hal_common.cpp`.
   All are enumerated seams in this spec (B.1, C.2, D, E); the port fails
   only if an unlisted M5 dependency surfaces — grep gate before P0
   merge: `M5\.` outside `src/hal/` + `compat.h` must be zero.
2. **espressif32@6.12.0 vs LilyGO board defs.** Vendor T-Deck-Pro pins
   espressif32@6.5.0 (Arduino core 2.x — compatible with our Bluedroid
   surface), but newer LilyGO examples (esp. T5/EPD47 S3 work) drift
   toward pioarduino/core 3.x. Our BLE stack cannot follow (0. fixed
   points). Mitigation: vendor the board jsons into `boards/` and build
   everything on 6.12.0 in CI from day one; if a LilyGO lib requires core
   3.x APIs, patch or pin backward, never bump the platform.
3. **LTE power on a 1400–1500 mAh pack.** Resident modem ≈ days, not
   weeks (A.5), TX bursts near 2 A risk brownout during WiFi/BLE coex.
   Mitigation: duty-cycle profile default when on battery, resident only
   on USB; bench `modemPower()` sequencing early in P3.
4. **MQTT broker dependency.** New always-on infrastructure the BLE/TCP
   paths never needed, and a third party that reads prompt traffic unless
   self-hosted + TLS-pinned (A.2). Also a fresh cert-validity/wall-clock
   coupling (D.2). Mitigation: self-host default, CA pin required,
   per-device credentials, LWT for honest presence.
5. **E-ink UX regression.** Approve-flow latency (~0.3–0.5 s partial +
   1–2 s wake-from-sleep full), ghosting from the amber-fill vocabulary,
   loss of the GIF pet (a product-identity feature — mitigation: ASCII
   pet poses + haptic/frontlight personality on the Max, and DASH-first
   framing where e-ink *wins*). LilyGO's own partial-refresh damage
   warning makes the B.3 full-refresh interleave mandatory, not tunable.
6. **Spec uncertainties still open** (verify on hardware/at purchase):
   Max panel-touch vs touch-buttons (CST3530 ambiguity), both T-Decks'
   1400-vs-1500 mAh, Pro-4G speaker path (audio through A7682E — v1 may
   ship silent), T5 Pro enclosure reality (dev-kit frame vs handheld),
   T5 Lite price/availability (docs-repo only), T5 boards ship without
   battery, no IMU on T5s (flip/shake/double-tap gestures degrade to
   no-ops), GDEQ031T10 partial-refresh quality across temperature.
7. **Keyboard ergonomics of deny.** Backspace-as-deny is one mispress
   from denying a prompt (same weight as BtnB today, but a QWERTY invites
   resting fingers). Consider requiring `EV_B_LONG` for deny on keyboard
   devices, or a two-key chord — decide in P2 usability pass, not now.
8. **Scope creep via vendor libs.** lvgl (vendor UI), RadioLib, GPS libs
   are all one `lib_deps` line away — the port deliberately takes GxEPD2 /
   EPD47 / TCA8418 / TinyGSM and nothing else until a phase needs it.

---

## Source URLs

- https://lilygo.cc/products/t-deck-pro — price/variants/battery (fetched 2026-07-12)
- https://wiki.lilygo.cc/products/t-deck-series/t-deck-pro/ — Pro chip-level specs
- https://lilygo.cc/products/t-deck-max — Max price/frontlight
- https://wiki.lilygo.cc/products/t-deck-series/t-deck-max/ — Max chip-level specs
- https://linuxgizmos.com/lilygo-t-deck-max-is-an-esp32-s3-handheld-with-lora-gps-and-e-paper/ — Max battery/touch-buttons cross-check
- https://www.cnx-software.com/2025/04/03/lilygo-t-deck-pro-esp32-s3-lora-messenger-e-paper-touch-display-keyboard-and-4g-lte-or-audio-codec-option/ — Pro variants context
- https://lilygo.cc/en-us/products/t5-e-paper-s3-pro — T5 Pro specs/price
- https://www.hackster.io/news/lilygo-s-t5-e-paper-s3-pro-pairs-espressif-s-esp32-s3-with-4-7-display-and-wireless-charging-da589d328542 — T5 Pro wireless charging/form factor
- https://lilygo.cc/products/t5-4-7-inch-e-paper-v2-3 — T5 base specs/price/partial-refresh warning
- https://github.com/Xinyuan-LilyGO/documentation/blob/master/en/products/t5-series/t5-e-paper-s3-lite/index.md — T5 S3 Lite specs
- https://github.com/Xinyuan-LilyGO/T-Deck-Pro — vendor firmware: GxEPD2 1.5.5, TinyGSM 0.12, TCA8418, RadioLib 6.4.2, espressif32@6.5.0
- https://github.com/Xinyuan-LilyGO/LilyGo-EPD47 and https://github.com/Xinyuan-LilyGO/T5S3-4.7-e-paper-PRO — ED047TC1 driver stack (epdiy)
- https://github.com/vroland/epdiy — upstream EPD47 driver
- https://github.com/ZinggJM/GxEPD2 — `GxEPD2_310_GDEQ031T10` panel class
