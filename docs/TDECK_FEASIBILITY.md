# T-DECK FEASIBILITY — build-level go/no-go (T-Deck Pro 4G, T-Deck Max)

Build-feasibility verdict for porting sticks3-buddy to the **LilyGO
T-Deck Pro (A7682E 4G variant)** and **LilyGO T-Deck Max**. This document
goes one level below `docs/PORT_SPEC.md` (the design spec — read it
first; sections cited as PORT_SPEC §B.1 etc. are not repeated here): it
verifies each subsystem against the **actual vendor firmware repos, their
platformio.ini pins, and their vendored library trees**, fetched
**2026-07-22**, and turns that into a purchase decision a buyer can act
on. Scope is the two T-Decks only; the T5 boards keep their PORT_SPEC
treatment.

Method: vendor build files and examples were read from
[Xinyuan-LilyGO/T-Deck-Pro](https://github.com/Xinyuan-LilyGO/T-Deck-Pro)
(branches `master` and `HD-V2-250915`) and
[Xinyuan-LilyGO/T-Deck-MAX](https://github.com/Xinyuan-LilyGO/T-Deck-MAX)
(`master`); library identities from each repo's `lib/` tree; panel/modem
facts from the driver headers and SIMCom documentation. Every claim about
our firmware cites a file in this repo. **No compile was run** — this is
a research spike; the recommended pre-purchase compile smoke is listed in
§7.

---

## 1. Executive verdicts

**T-Deck Pro (4G): GO-WITH-CAVEATS.** The vendor firmware pins
`platform = espressif32@6.5.0` — the same Arduino-ESP32 2.x / Bluedroid
core family as our `espressif32@6.12.0` pin — on both hardware-revision
branches, and the vendor's own `examples/test_bluetooth` runs a
**Bluedroid `BLEDevice.h` GATT server while driving the e-paper panel**.
Our BLE stack and protocol therefore port without a BLE rewrite; every
peripheral (GxEPD2 e-ink, TCA8418 keyboard, BQ25896+BQ27220 power,
TinyGSM-fork cellular) has a vendor-proven driver on our core family.
The caveats: v1 ships **silent** (the 4G variant's speaker hangs off the
modem, not the ESP32 — confirmed at repo level, §2.6), there is no RTC
(SoftClock + SNTP/`AT+CCLK` seeding, PORT_SPEC §D.2), partial refresh
is **700 ms** per the driver constants (slower than PORT_SPEC's
300–500 ms estimate), and it is the first-mover port that carries all
the shared seams.

**T-Deck Max: GO.** Same toolchain pin (`espressif32@6.5.0`, and its
platformio.ini literally reuses `board = T-Deck-Pro`), same panel, same
keyboard controller, same gauge — plus vendored, example-backed drivers
for everything the Pro lacks: **ES8311 audio** (`esp_codec_dev` +
`ESP8266Audio`, `examples/ES8311`), **DRV2605 haptic**
(`Adafruit_DRV2605`, `examples/motor`), XL9555 IO-expander switching,
and SY6970 charging via the same XPowersLib that serves the Pro's
BQ25896. Every `board::` function in our HAL has a concrete backend
path. Residual ambiguity (CST3530 panel-touch vs touch-buttons) does not
gate anything — the keyboard covers the input model. **If buying one
device, buy the Max**: +$15 restores the full `soundEvent()` vocabulary
plus haptic attention, and nothing about it is harder.

**No-go conditions checked and absent:** no vendor branch forces Arduino
core 3.x / NimBLE; no subsystem lacks an Arduino-2.x driver; no RAM or
flash wall (§2.7). The port is gated on effort, not on feasibility.

---

## 2. Subsystem verification

### 2.1 The decisive question: vendor toolchain vs our Bluedroid pin

**Answer: MATCH. The vendor stack is Arduino-ESP32 2.x (Bluedroid), same
family as our pin. No NimBLE rewrite is required or even suggested by
anything in the vendor tree.**

| | Vendor (T-Deck Pro) | Vendor (T-Deck Max) | Ours |
|---|---|---|---|
| platform pin | `espressif32@6.5.0` (`master` **and** `HD-V2-250915` `platformio.ini`) | `espressif32@6.5.0` (`master`) | `espressif32@6.12.0` (`platformio.ini`) |
| Arduino core | 2.0.14 (IDF 4.4.6) | 2.0.14 | 2.0.17 (IDF 4.4.7) |
| BLE stack | Bluedroid (`examples/test_bluetooth/test_bluetooth.ino`: `#include <BLEDevice.h>`, `BLEDevice::init("T-Deck-Pro-BLE")`, GATT server echoing writes to the EPD) | (no BLE example, same core) | Bluedroid — `src/ble_bridge.cpp` (603 lines against `esp_gap_ble_api.h`, `esp_ble_get_bond_device_list()`, `BLESecurityCallbacks`, `BLEDevice::setCustomGapHandler(_gapKeyWatch)`) |
| board def | `boards/T-Deck-Pro.json` (vendor `boards_dir = boards`) | reuses `board = T-Deck-Pro` | vendor it into our `boards/` (PORT_SPEC §E) |

2.0.14 → 2.0.17 is a bugfix-line delta within the same core; we build the
vendor's libraries on **our** (newer) pin, which is the low-risk
direction. The platform-version→core mapping is per
[sivar2311/platform-espressif32-versions](https://github.com/sivar2311/platform-espressif32-versions).

Context that raises the stakes and confirms the strategy: upstream
arduino-esp32 made **NimBLE the default BLE stack in 3.3.0** and plans to
remove Bluedroid in 4.0
([issue #11822](https://github.com/espressif/arduino-esp32/issues/11822),
[#9835](https://github.com/espressif/arduino-esp32/issues/9835)). Staying
on the 2.x line — which the vendor also does, on every branch including
the newer `HD-V2-250915` hardware revision — is the only place
`ble_bridge.cpp` exists unmodified. **Quantified for completeness only**:
if a future constraint ever forced core 3.x, the cost is a full rewrite
of `ble_bridge.cpp` (603 LOC; the bond-list reconcile, security
callbacks, and GAP-key watch have no 1:1 NimBLE equivalents) ≈ 5–8
dev-days plus a pairing/bonding re-soak across all hosts. That cost is
**not** on this port's path.

### 2.2 E-ink display (GDEQ031T10, both devices)

- **Vendor lib:** GxEPD2, vendored at **1.5.5**
  (`T-Deck-Pro/lib/GxEPD2/library.properties`: `version=1.5.5`, author
  Jean-Marc Zingg). Same folder in T-Deck-MAX `lib/`. Note the registry
  `lib_deps` lines in the vendor platformio.ini are **commented out** —
  the vendor builds from the vendored `lib/` copies (PORT_SPEC §1.1's
  "their platformio.ini pins GxEPD2@1.5.5" is true in spirit; at build
  level the copies live in-tree).
- **Panel class:** `GxEPD2_310_GDEQ031T10`
  (`GxEPD2/src/gdeq/GxEPD2_310_GDEQ031T10.h`) — in **mainline** GxEPD2
  since v1.5.4 (Jan 2024), so we can take mainline 1.5.5 from the
  registry rather than the vendored copy. Constants, from the header:
  240×320, controller **UC8253**, `hasPartialUpdate = true`,
  `hasFastPartialUpdate = true`, `full_refresh_time = 1100` ms,
  `partial_refresh_time = 700` ms, power on/off 50 ms.
- **Partial + full-refresh interleave:** supported — partial-window
  refresh is a first-class GxEPD2 API and the class advertises fast
  partials; the full-refresh interleave of PORT_SPEC §B.3 is app policy
  on top, unchanged. **Correction to PORT_SPEC:** budget ~**700 ms** per
  partial and ~**1.1 s** per full (driver constants), not 300–500 ms.
  Approval-overlay latency ≈ 0.7 s from panel-awake, ~1.1–2 s from
  panel-sleep. UX risk H.5 stands with worse numbers.
- **Coexistence with the LovyanGFX render seam:** clean. `src/compat.h`
  is a pure alias layer (`using TFT_eSprite = M5Canvas; using TFT_eSPI =
  LovyanGFX;`) and the whole ~3k-line UI reaches hardware at three
  `spr.pushSprite(0, 0)` sites (`src/main.cpp:638, 2244, 2889`).
  LovyanGFX builds standalone on any ESP32-S3 (no M5Unified); the sprite
  is parentless PSRAM memory. GxEPD2 needs Adafruit_GFX (vendored in
  both repos; registry equivalents fine). **The sprite/render path does
  NOT need rework** — `board::present()` (PORT_SPEC §B.1/§E) converts
  RGB565→1bpp (threshold), diffs against the previous frame, and drives
  `GxEPD2_310_GDEQ031T10` partial/full refresh.
- **Render-seam effort estimate:** `compat.h` non-M5 branch ~30 LOC;
  `hal_common_lgfx.cpp` (`present()` convert + diff + refresh policy)
  ~200–300 LOC; 3 call-site edits; per-board panel init ~50 LOC. The
  UI's 2954-line `main.cpp` is untouched in phase 1 (135×240 canvas
  letterboxed on 240×320 portrait); the native-landscape 320×240 layout
  pass is the later, wide-but-mechanical item (PORT_SPEC §B.4).
- **Effort: MED** (3–5 days incl. ghosting/policy tuning). **Risk: MED**
  (refresh UX, not compilability).

### 2.3 Keyboard (TCA8418, both devices)

- **Vendor lib:** `Adafruit TCA8418` vendored in both repos; examples
  `test_keypad` (Pro) and `keypad` (Max). I2C 0x34, INT-driven — matches
  PORT_SPEC §C.3's plan.
- Maps onto the new `board::inputPoll()` event queue (PORT_SPEC §C.2):
  Enter/`y` approve, Backspace/`n` deny, arrows/space carousel, other
  printables → `EV_TEXT` for the v2.1 `ask_answer` unlock (§C.4 — the
  additive, capability-gated protocol revision; on-device text entry and
  AskUserQuestion answering become reachable exactly as spec'd).
- Touch is **optional, not MVP**: Pro CST328 has the vendor's
  `touch_hyn_core` example (Hynitron custom driver; `HynTouch` lib in
  the MAX repo; `SensorLib` also vendored) — defer.
- **Effort: LOW** (1–2 days incl. the keymap + long-press semantics).
  **Risk: LOW.** Deny-on-Backspace ergonomics risk (PORT_SPEC §H.7)
  unchanged.

### 2.4 Cellular (A7682E, both devices)

- **Modem:** SIMCom A7682E, LTE Cat-1, LTE-FDD **B1/B3/B5/B7/B8/B20** +
  GSM 900/1800, 10 Mbps down / 5 Mbps up
  ([SIMCom product page](https://www.simcom.com/product/A7682E.html),
  [EBV](https://my.avnet.com/ebv/products/new-products/npi/2021/simcom-wireless-solutions-a7682e/)).
- **TinyGSM support: confirmed, with a fork subtlety that matters at
  build time.** The vendored `T-Deck-Pro/lib/TinyGSM` reports
  `library.json` version **0.11.7** (mainline metadata) but contains
  **`TinyGsmClientSIM7672.h`, `TinyGsmHttpsA76xx.h`,
  `TinyGsmMqttA76xx.h`** — headers that do not exist in mainline 0.11.7.
  It is LilyGO's modified fork (the
  [LilyGO-T-A76XX](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX) /
  lewisxhe lineage), built with **`-DTINY_GSM_MODEM_SIM7672`** (the
  vendor's platformio.ini build flag — the A7682E is driven as a
  SIM767x-class device). **Consequence:** vendor this TinyGSM copy into
  our repo like a board def; do not expect the registry `TinyGSM@0.11.7`
  to drive the A7682E. (Mainline TinyGSM master has since grown
  A7670/A7608 and SIM7672/SIM7670G clients plus `TinyGsmClientA76xxSSL`
  (+CCH TLS) — a viable fallback, but the vendored fork is the
  hardware-proven path.)
- **TLS + MQTT path: better than PORT_SPEC assumed.** The fork's
  `TinyGsmMqttA76xx.h` exposes the modem's **native AT-command MQTT(S)
  client** — TLS handshake and session live **on the modem**, zero
  mbedTLS RAM/CPU on the ESP32, which is exactly the "TLS via the
  modem's native SSL stack (preferred)" branch of PORT_SPEC §A.2.
  `link_mqtt.cpp` can bind to it directly, or use PubSubClient over
  `TinyGsmClient` as the portable fallback. CA-validation requirement
  (§A.2 security note) unchanged.
- **Power, refined with datasheet figures:** sleep **< 2 mA** with
  `AT+CSCLK=1` + DTR high (A7682E Hardware Design doc); idle-registered
  low-tens-of-mA class; data bursts 100–300 mA with TX peaks toward 2 A
  (rail is LilyGO's design; brownout-during-BLE-coex is ours to bench,
  §2.7). On the 1400–1500 mAh pack the PORT_SPEC §A.5 duty modes stand:
  **resident ≈ 4–7 days** (CSCLK sleep + MQTT keepalive 300 s, e-ink
  static, ESP32 mostly idle), **duty-cycled ≈ 2 weeks**, desk mode
  indefinite on USB. Data budget unchanged (~2–8 MB/month event-driven).
- **Singapore band check: PASS.** Singtel runs LTE on 900/1800/2600
  (B8/B3/B7), StarHub and M1 on 1800/2600 (B3/B7), with B1 (2100) also
  deployed — the A7682E's B1/B3/B7/B8 covers all three carriers
  ([spectrum-tracker](https://www.spectrum-tracker.com/Singapore),
  [frequencycheck](https://www.frequencycheck.com/countries/singapore)).
  No B28 (700 MHz) on the module — SG's 700 MHz went to 5G n28, so the
  practical cost is slightly weaker deep-indoor reach, not loss of
  service. SG shut down 2G in 2017, so the GSM fallback is dead weight
  there (harmless). Any SG nano-SIM data plan works; at 2–8 MB/month the
  cheapest prepaid/IoT tier suffices.
- **Effort: MED** for the MQTT link (fw + bridge listener, PORT_SPEC
  §A.2/§A.4). **Risk: MED** (power rail + broker infra), unchanged from
  PORT_SPEC §H.3/H.4.

### 2.5 PMU / battery gauge

- **T-Deck Pro: BQ25896 charger + BQ27220 gauge.** Vendor drivers:
  **XPowersLib** (vendored; upstream
  [lewisxhe/XPowersLib](https://github.com/lewisxhe/XPowersLib) supports
  the BQ25896 and SY6970 "PPM" classes alongside the AXP family) plus a
  vendored **BQ27220** library (unversioned, board-support-grade — no
  registry identity; snapshot it like the board json). Vendor examples
  `bq25896_shutdown` and `bq27220` prove charger control (incl. the
  BATFET ship-mode our `board::powerOff()` needs, PORT_SPEC §D.1) and
  gauge reads.
- **T-Deck Max: SY6970 + BQ27220** — same XPowersLib (PowersSY6970
  class), same BQ27220 lib, vendor example `battery`.
- Mapping to `board::` (PORT_SPEC §D.1 table) is direct; a real
  StateOfCharge gauge upgrades today's 3.2–4.2 V linear estimate in
  `hal_common.cpp`. `hasCoulomb()` stays honestly `false`.
- **Effort: LOW-MED** (1.5–2.5 days both chips). **Risk: LOW.**

### 2.6 Audio

- **T-Deck Pro 4G: confirmed effectively silent for us.** The repo-level
  evidence matches PORT_SPEC's flag: the audio-capable examples
  (`test_pcm5102a`, `ESP32-audioI2S` lib) belong to the **PCM5102A
  audio variant**, which is **mutually exclusive** with the A7682E 4G
  variant; on the 4G variant the speaker is wired to the modem's audio
  output. No vendor example drives that speaker from the ESP32. Whether
  modem AT tone/audio playback is usable for notification beeps is
  **unverified** — plan v1 silent with `attentionPulse()` display flash
  (PORT_SPEC §E), treat modem-tone experiments as stretch.
- **T-Deck Max: audio restored, vendor-proven.** ES8311 codec driven via
  vendored **`esp_codec_dev`** + **`ESP8266Audio`**, speaker shared with
  the modem and switched by the **XL9555** expander (vendor examples
  `ES8311`, `XL9555`); **DRV2605** haptic via vendored
  `Adafruit_DRV2605` (`examples/motor`). `board::beep()/playTone()`
  (`src/hal/hal.h:71-72`, consumed by `soundEvent()` at
  `src/main.cpp:216`) get an I2S tone-generator backend — M5Unified's
  Speaker class is absent, so this is a small synth, not a wrapper.
  Front-light control pin: not identified in this pass (likely an XL9555
  bit) — verify at bring-up.
- **Effort:** Pro n/a (silent) / Max **MED** (2–3 days audio + 0.5 day
  haptic). **Risk:** LOW-MED (XL9555 speaker arbitration with the modem
  is a one-time mode switch for us — we never take calls).

### 2.7 BLE + WiFi + cellular coexistence, RAM, flash

**No red flags found; the union is unproven but every pairwise
combination has direct evidence.**

- **BLE + e-ink on this exact board:** vendor `test_bluetooth` (Bluedroid
  GATT server + EPD writes) — §2.1.
- **BLE + WiFi + TLS in this exact codebase:** our OTA path keeps BLE up
  through the whole WiFi+TLS download — `src/ota/ota.cpp:88`: "BLE never
  left — it keeps advertising" — on the same S3/8MB-PSRAM class. The
  RF-coex arbitration (shared 2.4 GHz radio) is core-managed and already
  survives on the sticks.
- **Cellular adds no RF coex** — the A7682E is a UART peripheral with
  its own radio; its cost is the power rail (2 A TX peaks), which is the
  §2.4 bench item, not a software constraint.
- **RAM arithmetic:** internal SRAM (~512 KB) carries Bluedroid
  (~60–100 KB) + WiFi when up (~50–70 KB) + GxEPD2's 1bpp frame
  (240×320/8 = **9.6 KB**, + 9.6 KB previous-frame diff copy). The
  16-bit render canvas goes to PSRAM (135×240×2 = 64.8 KB phase-1;
  320×240×2 = 153.6 KB native-landscape) — trivial against 8 MB. On
  these envs we **drop** M5Unified/M5GFX/AnimatedGIF (PORT_SPEC §B.4/§E)
  and do **not** link the vendor's lvgl, so headroom improves relative
  to the stick build. Cellular TLS offloads to the modem (§2.4), so no
  mbedTLS session RAM on the MQTT path. `BUDDY_RX_CAP 8192` /
  `BUDDY_LINEBUF 4608` (`src/config.h:20-21`, S3 tier) fit unchanged.
- **Flash:** 16 MB vs our 8 MB — new `partitions/lilygo_16mb_ota.csv`
  (PORT_SPEC §E) gives two ~6.5 MB OTA slots + LittleFS with room to
  spare. No constraint.

---

## 3. Subsystem summary table

Effort: LOW ≈ ≤2 days, MED ≈ 2–5 days, HIGH ≈ >5 days. "Compiles vs our
toolchain" = expected on `espressif32@6.12.0` (Arduino 2.0.17) given the
vendor proves it on 6.5.0 (2.0.14); §7's compile smoke is the cheap
confirmation.

| # | Subsystem | Device | Vendor lib (version, where) | Compiles vs ours? | Effort | Risk |
|---|---|---|---|---|---|---|
| 1 | Toolchain/BSP | Pro | `espressif32@6.5.0`, `boards/T-Deck-Pro.json` (master + HD-V2-250915) | **YES — same core 2.x family** | LOW (vendor board json + env) | LOW |
| 1 | Toolchain/BSP | Max | `espressif32@6.5.0`, reuses `board = T-Deck-Pro` | **YES** | LOW | LOW |
| 2 | E-ink GDEQ031T10 | both | GxEPD2 **1.5.5** vendored; class `GxEPD2_310_GDEQ031T10` in **mainline ≥1.5.4** (UC8253, partial 700 ms / full 1100 ms) | YES (mainline lib usable) | MED (present() seam ~250–350 LOC) | MED (refresh UX, ghosting policy) |
| 3 | Keyboard TCA8418 | both | `Adafruit TCA8418` vendored; examples `test_keypad`/`keypad` | YES | LOW | LOW |
| 4 | Cellular A7682E | both | vendored TinyGSM **fork** (0.11.7-base + `TinyGsmClientSIM7672/MqttA76xx/HttpsA76xx`), `-DTINY_GSM_MODEM_SIM7672` | YES (vendor the fork copy) | MED (link_mqtt fw+bridge) | MED (power rail, broker infra) |
| 5 | PMU/gauge | Pro | XPowersLib (BQ25896) + vendored BQ27220 lib; examples `bq25896_shutdown`, `bq27220` | YES | LOW-MED | LOW |
| 5 | PMU/gauge | Max | XPowersLib (SY6970) + BQ27220; example `battery` | YES | LOW-MED | LOW |
| 6 | Audio/attention | Pro | none for 4G variant (speaker behind modem) | n/a — **ship silent** | n/a (display-flash attention) | LOW (product, not build) |
| 6 | Audio/attention | Max | `esp_codec_dev` + `ESP8266Audio` (ES8311), `Adafruit_DRV2605`, XL9555 switch; examples `ES8311`, `motor`, `XL9555` | YES | MED | LOW-MED |
| 7 | BLE+WiFi+cell coex / RAM | both | vendor `test_bluetooth` (BLE+EPD); our `ota.cpp` (BLE+WiFi+TLS); modem = UART | YES (pairwise proven) | — (falls out of bring-up) | MED (union unproven; LTE-TX brownout bench) |

---

## 4. MVP definition and effort

**MVP = "desk buddy, no radio bills":** BLE NUS + protocol v2 (bit-for-bit
today's `ble_bridge.cpp` + `data.h` path) + e-ink dashboard
(`DISP_DASH` home, HUD, sessions, cards, clock, approval/ask/passkey
overlays, letterboxed 135×240) + TCA8418 approve/deny/menu/carousel +
BQ gauge telemetry + SoftClock seeded from host. **Excluded:** cellular,
audio, touch, LoRa/GPS, OTA, native-landscape layout.

| Work item | Days |
|---|---|
| P0 seams on current hardware (`board::present()` + `inputPoll()` refactor, behavior-identical on M5 sticks, native tests) | 3–4 |
| Board def vendoring, env, 16 MB partitions, compile scaffold | 1 |
| E-ink present() backend (convert/diff/policy on GxEPD2) + bring-up | 3–5 |
| TCA8418 → input events | 1–2 |
| PMU (XPowersLib + BQ27220 → `board::` funcs) | 1.5–2.5 |
| BLE bring-up + pairing UX on e-ink (passkey screen, bond flows) | 1–2 |
| Integration, ghosting tuning, soak | 2–3 |
| **MVP total** | **≈ 12–19 dev-days** |

The MVP is identical for Pro and Max (audio/haptic are post-MVP), so the
device choice does not move this number.

## 5. Full build and phasing

Full build = MVP + cellular MQTT link (firmware `link_mqtt.cpp` +
`TinyGsmMqttA76xx` binding 4–6 d, bridge `link_mqtt.py` listener 2–3 d,
provisioning + failover ladder in the §A.3 selector) + Max audio/haptic
(3–4 d) + touch (1–2 d) + OTA slugs/partition test (1–2 d) + power/data
bench incl. LTE-TX brownout (2–3 d): **+13–20 d → ≈ 25–39 dev-days
total.**

Phasing reconciles with PORT_SPEC §G unchanged in shape: P0 (seams on M5
hardware) → P1 (firmware TCP client — independent of the T-Decks, do it
anyway) → P2 (T-Deck bring-up = MVP above) → P3 (cellular) → P4
(audio/haptic delta). **One deviation recommended:** if the purchase is
the Max, P4 collapses into P2's tail — the Max-first path removes the
only GO-WITH-CAVEATS item (silent v1) at zero extra port cost, and the
Pro becomes the delta port (nothing Pro-only exists except its BQ25896
charger class, which XPowersLib covers anyway).

## 6. BOM / where to buy / Singapore notes

| Item | Price (lilygo.cc, live 2026-07-12 per PORT_SPEC §1 — re-verify at checkout) | Notes |
|---|---|---|
| T-Deck Pro **A7682E (4G) variant** | **$94.68** | Pick the A7682E variant, **not** the PCM5102A audio variant — they are mutually exclusive; the audio variant has no modem. |
| T-Deck Max | **$109.87** | Recommended buy (§1). Audio + haptic + front-light; same modem. |
| SIM | ~$0–10/mo | Any SG nano-SIM data plan; 2–8 MB/month at spec'd cadence → cheapest prepaid/IoT tier. |

- **Sources:** [lilygo.cc](https://lilygo.cc) direct, or LilyGO's
  official AliExpress store; both ship to SG. Battery (1400–1500 mAh)
  and enclosure are included on both devices; USB-C.
- **Hardware revisions:** the Pro repo keeps per-revision branches
  (`HD-V1-250326`, `HD-V2-250915`; master documents V1.0/V1.1 firmware).
  A 2026 purchase likely ships the newer revision — at bring-up, match
  the vendor branch to the silkscreen/order page before trusting pin
  maps. Both branches pin the same platform, so the toolchain verdict is
  revision-proof.
- **LoRa SKU (soldered at factory, choose even though LoRa is out of
  port scope):** for Singapore pick the **915 MHz** SX1262 SKU — SG SRD
  usage sits in 920–925 MHz (AS923 plan), inside the 915-SKU's 902–928
  MHz range; the 868 MHz SKU cannot reach it.
- **Modem-band caveat, resolved (§2.4):** A7682E B1/B3/B7/B8 covers
  Singtel/StarHub/M1 LTE. No B28 → modestly weaker deep-indoor. 2G
  fallback is dead in SG (2017 shutdown) — irrelevant.

## 7. Honest risks and what was NOT verified

Top risks (beyond PORT_SPEC §H, which stands):

1. **Unversioned vendored dependencies.** The TinyGSM fork (0.11.7-base
   + A76xx headers), BQ27220 lib, and HynTouch have no registry
   identity. Mitigation: snapshot them into our repo with provenance
   notes (like `boards/*.json`); never satisfy them from the registry.
   GxEPD2, Adafruit TCA8418/DRV2605, XPowersLib can come from the
   registry at pinned versions.
2. **Refresh latency is worse than the design spec assumed** — 700 ms
   partial / 1100 ms full per driver constants. Approve-flow feel and
   the full-refresh interleave policy are the make-or-break UX items;
   bench on hardware in the first display week.
3. **The subsystem union is unproven.** Vendor examples are
   single-subsystem; nobody has run Bluedroid + WiFi + GxEPD2 + TinyGSM
   + keyboard simultaneously on this board. Pairwise evidence (§2.7) is
   strong; the specific bench item is brownout during LTE TX bursts with
   BLE connected, on the shared rail.
4. **Silent Pro v1** (4G variant) — product risk, not build risk; the
   Max purchase removes it.
5. **No compile was run in this spike.** Cheapest de-risk, before or
   right after purchase (~1 h, no hardware needed): scaffold env on
   `espressif32@6.12.0` + vendored board json + GxEPD2 1.5.5 + Adafruit
   TCA8418 + XPowersLib + the TinyGSM fork copy, compile a
   hello-world that instantiates `GxEPD2_310_GDEQ031T10`, `BLEDevice`,
   and `TinyGsmClientSIM7672` together. Any surprise this document
   missed will surface there.

Not verified in this pass: modem AT-tone capability on the Pro's speaker
(assumed unusable); Max front-light control pin; CST3530 panel-touch vs
touch-buttons on the Max (moot for the input model); real power draw and
brownout behavior (datasheet figures only); GDEQ031T10 partial-refresh
quality across temperature; BQ27220 vendored-lib API completeness
(assumed per vendor examples); current stock/prices (10-day-old
figures); whether the HD-V2 hardware revision moved any pins the master
examples don't cover.

---

## Sources (fetched 2026-07-22 unless noted)

Vendor repos and build files:

- https://github.com/Xinyuan-LilyGO/T-Deck-Pro — master `platformio.ini`
  (`espressif32@6.5.0`, `-DTINY_GSM_MODEM_SIM7672`), `lib/` tree,
  `examples/` (incl. `test_bluetooth/test_bluetooth.ino`,
  `bq25896_shutdown`, `bq27220`, `test_keypad`, `test_EPD`,
  `test_pcm5102a`), `lib/GxEPD2/library.properties` (1.5.5),
  `lib/TinyGSM/library.json` (0.11.7 metadata) + `lib/TinyGSM/src/`
  (`TinyGsmClientSIM7672.h`, `TinyGsmMqttA76xx.h`, `TinyGsmHttpsA76xx.h`)
- https://github.com/Xinyuan-LilyGO/T-Deck-Pro/tree/HD-V2-250915 —
  revision branch `platformio.ini` (same `espressif32@6.5.0`)
- https://github.com/Xinyuan-LilyGO/T-Deck-MAX — master `platformio.ini`
  (`espressif32@6.5.0`, `board = T-Deck-Pro`), `lib/` tree
  (`esp_codec_dev`, `ESP8266Audio`, `Adafruit_DRV2605`, `HynTouch`,
  `TDeckMaxBoard`, XPowersLib, BQ27220, GxEPD2, TinyGSM), `examples/`
  (`ES8311`, `motor`, `XL9555`, `battery`, `keypad`, `Elink_paper`,
  `A7682E/test_AT`)

Libraries and cores:

- https://github.com/ZinggJM/GxEPD2 —
  `src/gdeq/GxEPD2_310_GDEQ031T10.h` (240×320, UC8253, partial 700 ms /
  full 1100 ms, `hasFastPartialUpdate`); GDEQ031T10 support added v1.5.4
  (Arduino forum announcement, Jan 2024)
- https://github.com/sivar2311/platform-espressif32-versions —
  espressif32 6.5.0 → Arduino 2.0.14; 6.12.0 → Arduino 2.0.17 (IDF 4.4.7)
- https://github.com/espressif/arduino-esp32/issues/11822 and
  https://github.com/espressif/arduino-esp32/issues/9835 — NimBLE
  default in 3.3.0, Bluedroid removal planned in 4.0
- https://github.com/lewisxhe/XPowersLib — BQ25896 + SY6970 PPM driver
  support
- https://github.com/vshymanskyy/TinyGSM and
  https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX — mainline A76xx /
  SIM7672 client + `TinyGsmClientA76xxSSL` status; LilyGO fork lineage

Modem and bands:

- https://www.simcom.com/product/A7682E.html and
  https://my.avnet.com/ebv/products/new-products/npi/2021/simcom-wireless-solutions-a7682e/
  — A7682E LTE Cat-1, B1/B3/B5/B7/B8/B20, GSM 900/1800
- A7682E Hardware Design V1.05 (SIMCom) — sleep < 2 mA via `AT+CSCLK=1`
  + DTR
- https://www.spectrum-tracker.com/Singapore and
  https://www.frequencycheck.com/countries/singapore — Singtel/StarHub/M1
  LTE band deployments

This repo (grounding): `platformio.ini` (espressif32@6.12.0 pin),
`src/compat.h`, `src/ble_bridge.cpp`, `src/main.cpp:35,50,638,2244,2889`,
`src/ota/ota.cpp:88`, `src/config.h:20-21`, `src/hal/hal.h`,
`docs/PORT_SPEC.md` (design spec this document verifies).
