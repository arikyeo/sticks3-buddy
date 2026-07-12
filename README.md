<div align="center">

# StickS3 Buddy

**A desk companion for your coding agents.** It shows every Claude Code and
Codex session on a tiny M5 stick, gets visibly impatient when one is waiting
on you, and lets you approve or deny permission prompts with a physical
button — from your desk, another machine on your tailnet, or your phone.

[![CI](https://github.com/arikyeo/sticks3-buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/arikyeo/sticks3-buddy/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/arikyeo/sticks3-buddy)](https://github.com/arikyeo/sticks3-buddy/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-ESP32--S3-e7352c.svg)](#hardware)
[![Flash it](https://img.shields.io/badge/web%20flasher-flash%20in%20browser-brightgreen.svg)](https://arikyeo.github.io/sticks3-buddy/)

<img src="docs/s3/approval.jpg" alt="Approval prompt for a Bash command on the StickS3" width="230">
<img src="docs/s3/pet-stats.jpg" alt="Pet stats page: mood, fed, energy, level, token counters" width="230">
<img src="docs/s3/credits.jpg" alt="Credits page identifying the board as M5StickC Plus S3" width="230">

</div>

Your agent hits a permission gate. Instead of alt-tabbing to find the right
terminal, your desk buddy chirps, shows *which* session wants *what*
(`Bash: git push…`), and waits. **A** approves once. **B** denies. If you're
not at your desk, the same prompt is a tap away in Telegram. If the gate
times out, the CLI's native prompt takes over — the buddy can never brick
your session.

This is a unified fork: the original firmware and BLE protocol from
[anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy),
rebuilt on the ESP32-S3 port it was seeded from, with features and fixes
drawn from a dozen community forks — plus a from-scratch host bridge,
protocol v2, multi-host BLE, cross-machine federation, and a Telegram bot.
The full family tree lives in [docs/CREDITS.md](docs/CREDITS.md).

## Features

🤖 **Two agents, one buddy** — bridges both **Claude Code** and **Codex
CLI** via their hook systems; sessions carry a `C`/`X` badge so you know
who's asking. The stock Claude Desktop app still pairs and works as a
plain v1 host.

🔘 **Physical approve / deny** — an opt-in `PreToolUse` gate routes
permission prompts to the stick. **Fail-open by design:** timeout,
disconnect, or a dead daemon all fall back to the CLI's native prompt.
Every decision lands in an append-only audit log.

📊 **Glanceable dashboard** — the home screen shows a waiting count you
can read across the room, one color-coded row per session, and a live
transcript ticker. Drill into per-session detail, token counts, and a
last-activity line.

🖥️ **Multi-host** — the stick remembers up to 7 bonded machines with an
LRU registry, soft-pins to one, and courts new machines through a 60-second
pairing window. Flip the stick over and back to switch hosts.

🌐 **Federation** — run a bridge on every machine; they discover each
other over LAN broadcast (or static peers on **Tailscale**/WireGuard),
merge every machine's sessions onto whichever bridge holds the stick, and
route remote approvals home over HMAC-signed TCP. One stick, all your
computers.

📱 **Telegram bot** (opt-in) — pushes prompts, completions, and cards to
allowlisted chats; inline **Allow / Deny** buttons resolve pending gates
exactly like a stick button, and multiple-choice `AskUserQuestion`s become
tappable keyboards — question answering and notifications work even with
the stick asleep in a drawer.

🐣 **It's still a pet** — 18 built-in ASCII species plus GIF character
packs ([bufo](characters/bufo/) included). It sleeps when idle, celebrates
finished work, gets dizzy when you shake it, and naps face-down.

⏰ **Extras** — clock screen (portrait and charging-dock landscape),
pomodoro timer, notification cards (GitHub, CI, weather, firmware updates),
a distinct sound motif per event with an on-device legend, token-usage
bars, a learn/replay IR remote, and double-tap-to-wake.

📶 **WiFi OTA** (opt-in build) — `menu > update...` pulls the latest
GitHub release straight to the inactive flash slot, with bootloader
rollback if the new image fails to boot. Stores up to 256 WiFi networks
and joins the strongest.

🔌 **Multi-board** — M5StickS3 (the daily driver), M5StickC Plus2, and
the classic M5StickC Plus build from one source tree behind a small
hardware seam. An e-ink/cellular port is specced — see
[roadmap](#roadmap).

🔒 **Security posture** — BLE bonding with a passkey by default and
encrypted-only protocol characteristics; shared-secret HMAC with freshness
windows on all federation traffic; secrets never printed, echoed, or
logged. There is deliberately **no shake-to-deny**: a misread gesture must
never answer a consent prompt.

## Quick start

### 1. Flash

**No toolchain:** open the [web installer](https://arikyeo.github.io/sticks3-buddy/)
in desktop Chrome/Edge/Opera, plug the stick in over USB-C, click
**Install**. It ships the OTA-capable build.

**From source:**

```bash
pio run -e m5stick-s3 -t upload        # or m5stickc-plus2 / m5stickc-plus
```

> **Download mode (StickS3):** there's no GPIO-0 boot button. Long-press
> the side power button ~6 s to power off, then plug in USB while holding
> **BOOT** until the green LED flashes.

### 2. Install the bridge

```powershell
pipx install ./host
buddy-bridge setup          # guided: pair, hooks, gate, autostart
buddy-bridge daemon
```

The bridge tails Claude Code and Codex activity, feeds the stick, and
(opt-in) gates permission prompts on its buttons. Full walkthrough —
modes, federation, Telegram, WiFi tools, threat models:
**[host/README.md](host/README.md)**.

Prefer the stock experience? The Claude Desktop app pairs directly
(**Developer → Open Hardware Buddy…**) and speaks v1 to the same firmware.

### 3. Watch it work

Start a session, run something gated, and the buddy wakes up: amber row,
impatient pet, rising-triple motif, `A = allow once`, `B = deny`.

## Hardware

| | **M5StickS3** (default) | **M5StickC Plus2** | **M5StickC Plus** |
|---|---|---|---|
| SoC | ESP32-S3 @ 240 MHz | ESP32-PICO-V3-02 @ 240 MHz | ESP32-PICO-D4 @ 160 MHz |
| Flash / PSRAM | 8 MB / 8 MB OPI | 8 MB / — | 4 MB / — |
| Display | 1.14″ 135×240 IPS | 1.14″ 135×240 IPS | 1.14″ 135×240 IPS |
| PMIC / battery | M5PM1, 200 mAh | AXP2101 | AXP192 (+ current & coulomb readouts) |
| RTC | software clock | BM8563 | BM8563 |
| IMU | BMI270 | ✓ (via M5Unified) | ✓ (via M5Unified) |
| OTA updates | `m5stick-s3-ota` | `m5stickc-plus2-ota` | — (single app slot) |
| Extras tier | full (tunes + IR) | full (tunes + IR) | core |
| Build env | `m5stick-s3` | `m5stickc-plus2` | `m5stickc-plus` |

All three build in CI on every push; the StickS3 is the hardware-validated
daily driver. Per-board differences live behind the `board::` HAL seam —
adding a board means one backend file, not a source fork
([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## How it works

```
Claude Code ──hooks──┐                        ┌── BLE (NUS, bonded+encrypted)
Codex CLI  ──hooks──┤→ buddy-bridge daemon ──┤
JSONL/session logs ──┘         │              └── stick: dashboard, pet, buttons
                               │
                ┌──────────────┼──────────────┐
        HMAC relay (LAN/    Telegram Bot     GitHub Releases
        Tailscale peers)    (allowlisted)    (WiFi OTA, device-initiated)
```

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — firmware layout, host
  bridge internals, transports, and the full data-flow walkthroughs.
- **[REFERENCE.md](REFERENCE.md)** — the v1 wire protocol (Nordic UART
  Service, JSON schemas, folder push, pairing). Any device speaking this
  is compatible.
- **[PROTOCOL_V2.md](PROTOCOL_V2.md)** — strictly-additive v2: capability
  negotiation, multi-session heartbeats, structured asks, prompt
  cancellation, notification cards, WiFi + OTA commands. A v1 host keeps
  working unmodified — nothing v2 appears on the wire before `hello`.
- **[docs/manual.html](docs/manual.html)** — printable field manual for
  the on-device UI.

## Updating

Releases publish three artifacts: a **merged full-flash image** (first
install, also what the web flasher uses), an **app-only `firmware.bin`**
(update without losing bonds or settings), and a per-board
`firmware-<slug>.bin`. On OTA builds the stick updates itself:
provision WiFi once —

```bash
buddy-bridge wifi "MySSID"        # password via prompt; never echoed or logged
```

— then **hold A → update...** on the device. It joins the strongest stored
network, reads the latest release tag from the redirect (no rate-limited
API), flashes the inactive slot, reboots, and rolls back automatically if
the new firmware doesn't come up healthy. The bridge can also push an
"update available" card when a newer release appears.

## Roadmap

- **LilyGO T-Deck Pro 4G** — e-ink + LTE: the buddy that leaves the desk.
  Transport abstraction (BLE / WiFi-TCP / cellular MQTT with a failover
  ladder), an e-ink render policy, and keyboard input are specced against
  the current code seams in [docs/PORT_SPEC.md](docs/PORT_SPEC.md), with
  T-Deck Max and the T5 e-paper boards behind it.
- **WiFi/TCP device link, firmware side** — the bridge already ships a
  HMAC-authenticated TCP listener the stick will dial into over LAN
  (freeing the BLE radio for other centrals); the firmware client is the
  pending half.
- **On-device ask answering** — scroll-and-pick `AskUserQuestion` answers
  on the stick itself (today the stick displays them read-only; Telegram
  can answer them).

## Credits

This project stands on a lot of shoulders: Anthropic's original
desktop-buddy concept and firmware, the community S3 port it grew from,
and cherry-picks, ports, and ideas from more than a dozen forks — plus two
MIT-licensed bridges that shaped the host side. The full lineage, every
cherry-picked commit, and who did what: **[docs/CREDITS.md](docs/CREDITS.md)**.

## Contributing

The protocol is the stable surface; the firmware is a reference
implementation. **[CONTRIBUTING.md](CONTRIBUTING.md)** explains why the
best contribution is usually a fork — and what this repo does accept.

## License

[MIT](LICENSE), copyright 2026 Anthropic, PBC — with community
contributions credited in [docs/CREDITS.md](docs/CREDITS.md). The
`characters/bufo/` GIFs are third-party artwork from the community
[bufo emoji set](https://bufo.zone) — redistributed for convenience, not
covered by the MIT license; see
[characters/bufo/README.md](characters/bufo/README.md).
