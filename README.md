# StickS3 Buddy

A desk pet that lives on your M5StickC Plus S3 and reacts to Claude Code
and Codex sessions over Bluetooth LE — sleeps when nothing's happening,
wakes when a session starts, gets visibly impatient when a permission
prompt is waiting, and lets you approve or deny right from the device.

<p align="center">
  <img src="docs/s3/approval.jpg" alt="Approval prompt for a Bash command on M5StickS3" width="240">
  <img src="docs/s3/pet-stats.jpg" alt="Pet stats page showing mood, fed, energy, level and token counters" width="240">
  <img src="docs/s3/credits.jpg" alt="Credits page showing hardware line M5StickC Plus S3 / ESP32-S3 + BMI270" width="240">
</p>

This is a unified fork: the original ESP32 firmware and BLE protocol from
[anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy),
merged with an independent ESP32-S3 port and a long tail of community
fixes and features. See [Credits](#credits) for the full list.

## Features

- **Multi-host.** Pairs with more than one desktop/bridge; the device
  soft-pins to one active host and slow-retries the rest (see
  [PROTOCOL_V2.md](PROTOCOL_V2.md)).
- **Multi-session.** A single host can surface several concurrent CLI
  sessions (`claude` and `codex`), each with its own state and token
  count.
- **Multi-board.** M5StickC Plus S3 today; classic M5StickC Plus support
  is planned (see [PROTOCOL_V2.md](PROTOCOL_V2.md) and open CI matrix
  entries in `.github/workflows/ci.yml`).
- **GIF and ASCII pets.** Eighteen built-in ASCII species, or drag a
  custom GIF character pack onto the bridge's drop target.
- **Extras.** Optional notification cards (`ntfy`) for CI results, GitHub
  events, and more, on top of the core approval/session flow.
- **OTA-friendly updates.** Releases publish both a full-flash image (for
  first-time setup) and an app-only image (for updating an already-paired
  device without losing BLE bonds or settings).

## Quickstart

### 1. Flash the firmware

**Easiest: web flasher.** Open
[the web installer](https://arikyeo.github.io/sticks3-buddy/) in desktop
Chrome, Edge, or Opera, connect the StickS3 over USB-C, and click
**Install firmware**. No toolchain needed.

**From source:**

```bash
pio run -t upload
```

If you're starting from a previously-flashed device, wipe it first:

```bash
pio run -t erase && pio run -t upload
```

**First-time flash:** the StickS3 has no GPIO-0 boot button. To enter
download mode, long-press the side power button for ~6 seconds to power
off, then plug in USB while holding **BOOT** until the green LED flashes.

### 2. Pair with Claude or Codex

Enable developer mode in the desktop app
(**Help → Troubleshooting → Enable Developer Mode**), open
**Developer → Open Hardware Buddy…**, click **Connect**, and pick your
device from the scan list. Once paired, the bridge auto-reconnects
whenever both sides are awake.

### 3. Install the bridge

The bridge speaks [REFERENCE.md](REFERENCE.md) (v1) with the optional v2
additions in [PROTOCOL_V2.md](PROTOCOL_V2.md). See `host/` for the
reference bridge implementation and its own setup instructions.

## Hardware

| | |
| --- | --- |
| **Board** | M5StickC Plus S3 |
| **SoC** | ESP32-S3, 160 MHz dual-core |
| **Flash** | 8 MB |
| **PSRAM** | 8 MB, OPI |
| **Display** | 1.14" 135×240 IPS |
| **IMU** | BMI270 |
| **Connectivity** | BLE (Nordic UART Service) |
| **USB** | Native USB-CDC (no driver needed on Windows 11) |
| **Battery** | 200 mAh Li-Po |

## Building

Install [PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/),
then:

```bash
pio run -e m5stick-s3            # build
pio run -e m5stick-s3 -t upload  # build and flash
```

Other envs: `m5stickc-plus`, `m5stickc-plus2`, and the OTA-capable
variants below.

CI builds and size-guards every push and PR to `main` and `track-*`
branches; see `.github/workflows/ci.yml`.

### Over-the-air updates (optional)

The default build is BLE-only. An opt-in variant adds a WiFi OTA flow
that pulls the latest GitHub release from the device's own menu:

```bash
pio run -e m5stick-s3-ota -t upload       # S3 with menu > update...
pio run -e m5stickc-plus2-ota -t upload   # Plus2 variant (no PSRAM — tighter heap)
```

One-time WiFi provisioning (credentials go to device NVS; wiped by
factory reset, never echoed back):

```bash
buddy-bridge wifi "MySSID"    # reference bridge; prompts for the password
```

or `python tools/test_protocol.py --skip-debug --wifi "MySSID" "MyPass"`
over USB serial, or send `{"cmd":"wifi","ssid":"...","pass":"..."}`
yourself over the encrypted BLE link after `hello`. Then on the device:
**hold A → update...** — it joins WiFi, checks the latest release,
flashes the inactive slot, reboots, and turns WiFi back off. If the new
firmware fails to come up healthy, the bootloader rolls back to the
previous slot on the next reboot. Details, asset naming, and the TLS
caveat: [PROTOCOL_V2.md](PROTOCOL_V2.md), "WiFi provisioning + OTA".
The 4MB `m5stickc-plus` has no second app slot and no OTA variant.

## Project layout

```
src/
  main.cpp       — loop, state machine, UI screens
  buddy.cpp      — ASCII species dispatch + render helpers
  buddies/       — one file per species, seven anim functions each
  ble_bridge.cpp — Nordic UART service, line-buffered TX/RX
  character.cpp  — GIF decode + render
  data.h         — wire protocol, JSON parse
  xfer.h         — folder push receiver
  stats.h        — NVS-backed stats, settings, owner, species choice
characters/      — example GIF character packs
host/            — reference desktop bridge
tools/           — generators, converters, release helpers
web/             — GitHub Pages web flasher
```

## Protocol

- [REFERENCE.md](REFERENCE.md) — the v1 wire protocol: Nordic UART
  Service UUIDs, JSON schemas, folder push transport, pairing and
  security model. Any device that speaks this is compatible — you don't
  need anything else from this repo.
- [PROTOCOL_V2.md](PROTOCOL_V2.md) — optional v2 additions: capability
  negotiation, multi-session heartbeats, structured `ask` questions,
  prompt cancellation, and notification cards. Strictly additive; a v1
  device keeps working unmodified.

## Credits

This repo merges upstream Anthropic firmware with an independent S3 port
and fixes/features pulled from the wider fork community:

| Contributor | Contribution |
| --- | --- |
| [anthropics](https://github.com/anthropics/claude-desktop-buddy) | Original firmware and BLE protocol (upstream) |
| [openalchemy](https://github.com/openalchemy/claude-desktop-buddy-s3) | ESP32-S3 / M5StickC Plus S3 port |
| [CharlexH](https://github.com/CharlexH/CodeBuddy) | CodeBuddy — independent buddy implementation |
| [Swissola](https://github.com/Swissola/claude-desktop-buddy) | Multi-host bonding, brightness persistence, counter widening, scroll preservation, approval timeout, energy floor, RTC re-sync, buffer-overflow guard, BLE ring-buffer race fix, and more |
| [yiduo](https://github.com/yiduo) | README updates for dual-board support (#48) |
| [ttpears](https://github.com/ttpears/claude-desktop-buddy) | CI workflow |
| [bjedwards](https://github.com/bjedwards/claude-desktop-buddy-s3) | S3 port contributions |
| [JohnWong](https://github.com/JohnWong/claude-desktop-buddy-s3) | S3 port contributions |
| [xavierforge](https://github.com/xavierforge/claude-desktop-buddy-s3) | S3 port contributions |
| [MaikEight](https://github.com/MaikEight/claude-desktop-buddy) | Firmware contributions |
| [greedypelican](https://github.com/greedypelican/claude-desktop-buddy-s3-vscode) | VS Code tooling integration |
| [oh001738](https://github.com/oh001738/claude-desktop-buddy-for-tdisplay-s3) | T-Display S3 board port |
| [conallob](https://github.com/conallob/claude-desktop-buddy-s3) | Release workflow, platform upgrade |
| [23atomist](https://github.com/23atomist/claude-desktop-buddy-xorigin) | Cross-origin firmware contributions |
| [srokaw](https://github.com/srokaw/terminal-claude-code-buddy-m5stack) | Terminal Claude Code buddy for M5Stack |
| SnowWarri0r | Firmware contributions |
| Yamiqu | Firmware contributions |

## License

This project inherits its license from upstream: see [LICENSE](LICENSE).
The firmware is MIT-licensed. The bundled `characters/bufo/` GIF assets
are third-party artwork from the community "bufo" emoji set
([bufo.zone](https://bufo.zone)) and are **not** covered by the MIT
license — see [characters/bufo/README.md](characters/bufo/README.md) and
the notice at the bottom of [LICENSE](LICENSE) for details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
