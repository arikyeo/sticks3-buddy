# Credits & Lineage

This repo is a merge point for a small ecosystem. Almost nothing in it has a
single author, and the interesting parts of its history live in other
people's repos. This page records where everything came from — verified
against this tree's git history (cherry-pick trailers, commit bodies,
in-source attribution comments) rather than from memory. Where a
contribution couldn't be pinned to a person, it's described without a name
rather than guessed.

## The family tree

```
anthropics/claude-desktop-buddy          (upstream: firmware + BLE protocol, MIT)
        │
        ├── openalchemy/claude-desktop-buddy-s3   (ESP32-S3 port — the seed)
        │        │
        │        └── arikyeo/sticks3-buddy        (this repo: unified tree)
        │                 ▲    ▲    ▲
        │   cherry-picks ─┘    │    └─ host bridge built on
        │   (Swissola,         │       SnowWarri0r + Yamiqu (both MIT)
        │    SnowWarri0r)      │
        │                 ports & ideas from community forks
        │                 (ttpears, bjedwards, johnwong, xavierforge,
        │                  MaikEight, oh001738, 23atomist, …)
        │
        └── CharlexH/CodeBuddy            (independent Codex adaptation,
                                           no license — reimplemented, not copied)
```

### Upstream: `anthropics/claude-desktop-buddy`

[anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
is where all of this starts: the desk-buddy concept, the original M5StickC
Plus firmware, the ASCII pets, the
[BLE wire protocol](../REFERENCE.md), the folder-push transport, and the
Claude Desktop pairing flow. Initial release and follow-up by
[Felix Rieseberg](https://github.com/felixrieseberg) (the `docs/device.jpg`,
`docs/menu.png`, and `docs/hardware-buddy-window.png` images in this repo
are his). MIT-licensed; this repo's [LICENSE](../LICENSE) is that license,
copyright notice intact.

### The seed: `openalchemy/claude-desktop-buddy-s3`

This repo's history *is* the
[openalchemy/claude-desktop-buddy-s3](https://github.com/openalchemy/claude-desktop-buddy-s3)
fork — same commits, same SHAs — extended. The port commits sitting
directly on top of upstream were authored by
[kazukitu](https://github.com/kazukitu):
[`426a126`](https://github.com/arikyeo/sticks3-buddy/commit/426a126) ported
the firmware to the M5StickC Plus S3 (M5Unified compatibility shim, the
native-USB-CDC serial fix, StickS3 pinout and power-button handling) and
[`76ee575`](https://github.com/arikyeo/sticks3-buddy/commit/76ee575)
contributed the `docs/s3/` hardware photos still on the README.

A StickS3 M5Unified port was also proposed directly upstream by
[yiduo](https://github.com/yiduo) (易铎) as
[anthropics#48](https://github.com/anthropics/claude-desktop-buddy/pull/48);
that PR also documented the S3 USB-CDC `available()` lying-forever failure
mode which later bit this repo's boot path and is now fixed and
regression-noted in
[`fff8b38`](https://github.com/arikyeo/sticks3-buddy/commit/fff8b38).

### The cousin: `CharlexH/CodeBuddy`

[CharlexH/CodeBuddy](https://github.com/CharlexH/CodeBuddy) is an
independent StickS3 companion adapted for Codex, and several good ideas
here trace to it. **CodeBuddy has no license**, so this repo treats it as a
reference, not a source: no file was copied. Firmware behaviors
(clock-face formatting, the clock orientation state machine, UTF-8
codepoint handling) were reimplemented against this repo's conventions
with in-source attribution, its test suites' known-value assertions were
used as specifications for our native tests, and the host-side
functionality was clean-roomed outright
([`32cf3d0`](https://github.com/arikyeo/sticks3-buddy/commit/32cf3d0)).

## Cherry-picked commits

These landed with their original authorship preserved — each carries a
`(cherry picked from …)` trailer in `git log`.

| Author | Commit | What it fixed/added |
|---|---|---|
| [Swissola](https://github.com/Swissola) | [`8299646`](https://github.com/arikyeo/sticks3-buddy/commit/8299646) | portMUX spinlock on the BLE RX ring — real cross-core race on dual-core ESP32 |
| [Swissola](https://github.com/Swissola) | [`6865f5c`](https://github.com/arikyeo/sticks3-buddy/commit/6865f5c) | Clear stale transcript + pending prompt on disconnect (ghost-approval hazard); document stats fields |
| [Swissola](https://github.com/Swissola) | [`b7a9815`](https://github.com/arikyeo/sticks3-buddy/commit/b7a9815) | Entry-ordering fix: keep the *newest* 8 transcript lines, correct REFERENCE.md |
| [Swissola](https://github.com/Swissola) | [`102b37d`](https://github.com/arikyeo/sticks3-buddy/commit/102b37d) | Path-buffer overflow guard in the folder-push `file` command |
| [Swissola](https://github.com/Swissola) | [`e61c3fe`](https://github.com/arikyeo/sticks3-buddy/commit/e61c3fe) | Force clock re-sync after the bridge disconnects |
| [Swissola](https://github.com/Swissola) | [`19a5309`](https://github.com/arikyeo/sticks3-buddy/commit/19a5309) | 5-minute timeout clears approval prompts orphaned by a crashed desktop |
| [Swissola](https://github.com/Swissola) | [`17089b8`](https://github.com/arikyeo/sticks3-buddy/commit/17089b8) | Preserve transcript scroll position while new lines stream in |
| [Swissola](https://github.com/Swissola) | [`0551f27`](https://github.com/arikyeo/sticks3-buddy/commit/0551f27) | Widen approve/deny counters to `uint32_t` (upstream [PR #25](https://github.com/anthropics/claude-desktop-buddy/pull/25)) |
| [Swissola](https://github.com/Swissola) | [`c51491c`](https://github.com/arikyeo/sticks3-buddy/commit/c51491c) | Persist brightness across reboots (upstream [PR #24](https://github.com/anthropics/claude-desktop-buddy/pull/24)) |
| [Swissola](https://github.com/Swissola) | [`427d06b`](https://github.com/arikyeo/sticks3-buddy/commit/427d06b) | Per-host unpair instead of nuking all bonds — the groundwork multi-host was built on |
| [Swissola](https://github.com/Swissola) | [`79426f0`](https://github.com/arikyeo/sticks3-buddy/commit/79426f0) | Pet energy floors at 1 while on USB (a charging pet shouldn't look dead) |
| [SnowWarri0r](https://github.com/SnowWarri0r) | [`f3aa7aa`](https://github.com/arikyeo/sticks3-buddy/commit/f3aa7aa) | Two-note "task complete" chime on entering the celebrate state |
| [SnowWarri0r](https://github.com/SnowWarri0r) | [`3356c54`](https://github.com/arikyeo/sticks3-buddy/commit/3356c54) | Format LittleFS on first mount failure so character push works out of the box |

Swissola's eleven commits above are the reason a second bonded host was
even conceivable, and several of them (the RX-ring spinlock, the ghost
approval, the overflow guard) are the unglamorous kind of correctness work
that deserves to be named.

## Ported from community forks

Features re-landed here from fork branches, adapted to this tree — each
attributed in the porting commit and usually in a source-file comment too.

| From | What | Where it landed |
|---|---|---|
| [ttpears](https://github.com/ttpears/claude-desktop-buddy) | Screen-off CPU downclock + relaxed BLE connection parameters (their ~8.6 mA idle work, incl. the bench finding that 40 MHz kills the BT link); AXP192 coulomb-counter register access; `RTC_NOINIT` reset-reason diagnostics; the StickS3 board manifest | [`80efedd`](https://github.com/arikyeo/sticks3-buddy/commit/80efedd), `src/hal/hal_stickc_plus.cpp`, `src/main.cpp`, `boards/m5stick-s3.json`. Their esp_pm/light-sleep experiments (RTC-WDT failsafe on the PICO-D4) are why full light sleep stays deliberately out. |
| [bjedwards](https://github.com/bjedwards/claude-desktop-buddy-s3) | Three-bar token-usage display (`1ec45c0`); M5PM1 power-button gestures — double-click to power off, with the beep + rail delay (`75729fb`) | [`18bcc0c`](https://github.com/arikyeo/sticks3-buddy/commit/18bcc0c), `src/hal/hal_sticks3.cpp` |
| [JohnWong](https://github.com/JohnWong/claude-desktop-buddy-s3) | Loop-task watchdog so a wedged display loop reboots instead of freezing behind a healthy-looking BLE link; the `wrapInto` width-underflow hardening (`3fa0884`) | [`3fc0019`](https://github.com/arikyeo/sticks3-buddy/commit/3fc0019), `src/logic/wrap_text_logic.h` |
| [xavierforge](https://github.com/xavierforge/claude-desktop-buddy-s3) | The tune engine — async tune player, tune slot registry, `midi2tones` drop-in format | `src/audio/` ([`36331ff`](https://github.com/arikyeo/sticks3-buddy/commit/36331ff)), behind `BUDDY_EXTRAS_FULL` |
| [MaikEight](https://github.com/MaikEight/claude-desktop-buddy) | Typographic-punctuation sanitizer (`_sanitize`, `accd745`) — em-dashes, curly quotes, ellipses stop rendering as tofu | merged into `src/logic/utf8_text_logic.h` ([`b1a4a44`](https://github.com/arikyeo/sticks3-buddy/commit/b1a4a44)) |
| [oh001738](https://github.com/oh001738/claude-desktop-buddy-for-tdisplay-s3) | OTA hardening from their T-Display-S3 OTA track: explicit creds into `WiFi.begin`, isolated TLS clients, redirect handling, flicker-free progress | `src/ota/` ([`b0705fc`](https://github.com/arikyeo/sticks3-buddy/commit/b0705fc)) |
| [23atomist](https://github.com/23atomist/claude-desktop-buddy-xorigin) | Debounced `Button` + A+B `Chord` logic and its native unit-test suite, ported verbatim (`8968f26`) | `src/logic/button_logic.h`, `test/test_button/` ([`f5b40bc`](https://github.com/arikyeo/sticks3-buddy/commit/f5b40bc)) |
| [CharlexH/CodeBuddy](https://github.com/CharlexH/CodeBuddy) | Clock-face formatting + orientation state machine, UTF-8 codepoint helpers, native-test assertion anchors — reimplemented, no license to copy under | `src/logic/clock_display_logic.h`, `clock_orient_logic.h`, `utf8_text_logic.h`, `test/test_clock*` ([`1136d93`](https://github.com/arikyeo/sticks3-buddy/commit/1136d93)) |
| [cerocca](https://github.com/cerocca) | M5StickC Plus2 board definition (ESP32-PICO-V3-02), via upstream [PR #21](https://github.com/anthropics/claude-desktop-buddy/pull/21) | `boards/m5stickc-plus2.json` ([`2cbc5cb`](https://github.com/arikyeo/sticks3-buddy/commit/2cbc5cb)) |

## Host bridge provenance

The `host/` bridge (`buddy-bridge`) was written for this repo, but two
MIT projects gave it its hardest-won parts, and both are vendored or
ported with headers saying so:

- **[SnowWarri0r/cc-buddy-bridge](https://github.com/SnowWarri0r/cc-buddy-bridge)**
  (MIT) — the proof that a hooks→BLE bridge works without the desktop app.
  BLE reconnect/chunking/backoff and the Windows radio power-cycle recovery
  are vendored from `5c5b614` into `host/src/buddy_bridge/ble/`; the
  managed-merge settings installer follows their pattern. SnowWarri0r also
  authored the two firmware cherry-picks above — both sides of this
  project owe them.
- **[Yamiqu/codex-buddy-bridge](https://github.com/Yamiqu/codex-buddy-bridge)**
  (MIT) — the Codex hook stdout-discipline pattern (swap `sys.stdout` at
  import so stray prints can't corrupt the hook JSON protocol), ported
  from `e84d4b3` into `host/src/buddy_bridge/hooks/codex_hook.py`.
- **CharlexH/CodeBuddy** — no license, so its host-side ideas were
  clean-roomed rather than ported.
- The Codex adapter implements the hook contract as documented in
  [OpenAI's Codex hooks reference](https://learn.chatgpt.com/docs/hooks);
  the Claude adapter implements the documented Claude Code hooks API.

## Forks we studied

Work that wasn't merged but shaped thinking, or that deserves a pointer. The wider fork network is worth exploring if you're building your
own buddy:

- **[vthinkxie/claude-desktop-buddy-esp32](https://github.com/vthinkxie/claude-desktop-buddy-esp32)**
  — the most-starred community derivative (an ESP32-S3 touch-AMOLED
  build). Independent codebase; referenced, not merged.
- **[srokaw/terminal-claude-code-buddy-m5stack](https://github.com/srokaw/terminal-claude-code-buddy-m5stack)**
  — explored a multi-session prompt *queue* with promotion/redraw
  handling, territory this repo covers differently (`qn` queue counts +
  oldest-first federation merge).
- **[greedypelican/claude-desktop-buddy-s3-vscode](https://github.com/greedypelican/claude-desktop-buddy-s3-vscode)**
  — a VS Code extension bridge, plus a software-RTC fallback for RTC-less
  boards in the same spirit as this repo's StickS3 software clock.
- **[conallob/claude-desktop-buddy-s3](https://github.com/conallob/claude-desktop-buddy-s3)**
  — ran the arduino-core 3.x + NimBLE experiment and reverted to
  Bluedroid; independent corroboration of the Bluedroid/espressif32@6.x
  pin this firmware treats as an invariant (see
  [PORT_SPEC.md](PORT_SPEC.md)).

## Tools, libraries, art

- **[ESP Web Tools](https://github.com/esphome/esp-web-tools)** by
  [balloob](https://github.com/balloob) and the ESPHome team powers the
  [browser flasher](https://arikyeo.github.io/sticks3-buddy/).
- **[M5Unified / M5GFX](https://github.com/m5stack/M5Unified)** (M5Stack),
  **[AnimatedGIF](https://github.com/bitbank2/AnimatedGIF)** (Larry Bank),
  **[ArduinoJson](https://github.com/bblanchon/ArduinoJson)** (Benoît
  Blanchon), **[PlatformIO](https://platformio.org/)** + Unity for the
  native test rig, and **[bleak](https://github.com/hbldh/bleak)** on the
  host side.
- **[bufo.zone](https://bufo.zone)** — the `characters/bufo/` GIFs are
  community bufo-emoji artwork, redistributed for convenience and *not*
  under this repo's MIT license (see [../LICENSE](../LICENSE) and
  [characters/bufo/README.md](../characters/bufo/README.md)).

## The unified work

Everything not listed above — the `board::` HAL seam, protocol v2 and its
firmware parse core, the multi-host registry and pairing window, the
sessions/dashboard/clock/cards UI, the gesture engine, WiFi multi-network
store, the release-redirect OTA flow, and the whole `buddy-bridge` daemon
(gating, federation, device link, Telegram, cards) — was built in this
repo by [arikyeo](https://github.com/arikyeo) pair-programming with
Anthropic's Claude; the `Co-Authored-By: Claude` trailers throughout the
log are accurate, not decorative. Fitting, for a device whose job is
watching Claude work.

## License notes

Upstream is MIT and this repo honors it the boring way: the original
copyright line ("Copyright 2026 Anthropic, PBC.") ships unchanged in
[LICENSE](../LICENSE), cherry-picked commits keep their authors, ported
code carries attribution comments, MIT-vendored code names its source and
commit, and the one unlicensed neighbor (CodeBuddy) was reimplemented
rather than copied. If you fork this — and
[CONTRIBUTING.md](../CONTRIBUTING.md) hopes you will — please keep the
chain intact.

**Thank you** to everyone above. A desk toy this small having a family
tree this deep is the best thing about open source.
