# buddy-bridge

Windows-first Python host that bridges **Claude Code CLI** and **Codex CLI**
to an M5 StickS3 BLE desk buddy: live session states, activity feed, token
counts, and (opt-in) hardware permission approvals on the stick's buttons.

Requires Python >= 3.11. Runtime state lives in `~/.buddy-bridge/`
(override with `BUDDY_BRIDGE_HOME`), never in the repo.

## Install

```powershell
pipx install ./host              # or: pip install ./host
```

On Windows the winrt packages (BT radio auto-recovery) install automatically
as environment-marked core dependencies; `buddy-bridge[win32]` still works
as an alias of the same thing. `buddy-bridge` and `python -m buddy_bridge`
are equivalent entry points.

From a source checkout for development:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e "host[dev]"
```

## Setup

```powershell
buddy-bridge setup               # writes ~/.buddy-bridge/config.toml, mints a host id
buddy-bridge daemon              # run the bridge (foreground)
buddy-bridge service install     # optional: start it automatically at logon
buddy-bridge probe               # confirm the buddy is advertising (name prefix "Claude")
buddy-bridge pair                # optional: pin one device by address
buddy-bridge hooks install claude
buddy-bridge hooks install codex
buddy-bridge doctor              # verify python/BLE/daemon/hook state
buddy-bridge status | sessions   # query the running daemon
```

`hooks install` merges managed entries into `~/.claude/settings.json` /
`~/.codex/hooks.json` (plus `codex_hooks = true` under `[features]` in
`~/.codex/config.toml`). Entries are marked by the hook module path;
`hooks uninstall` removes exactly those and leaves everything foreign
untouched. A `.buddy-bridge.bak` backup is written before any change, and
an unparseable config file is refused, never clobbered.

## BLE modes

`buddy-bridge mode exclusive|ondemand|off` persists to the config **and**
applies live to a running daemon (via IPC); without one it takes effect on
the next start:

- **exclusive** (default) — hold the connection whenever the buddy is around;
  reconnect with 3 s -> 60 s backoff. If the buddy is soft-pinned to another
  host (hello ack `sel:false`), back off to one retry per minute until it
  selects us again.
- **ondemand** — no persistent link: a pending permission connects, delivers
  the prompt, waits for the decision (gate deadline minus 10 s), then clears
  and disconnects. A connect that can't complete in 5 s fails open to the
  native prompt.
- **off** — the BLE machinery never starts (bleak isn't even imported);
  mirrors and token accounting keep working.

On Windows, 5 consecutive scan misses trigger an automatic Bluetooth radio
power-cycle (max 3 attempts, 120 s cooldown) to unstick the WinRT scanner —
this briefly drops *all* BT devices, so it is deliberately rationed.

## Autostart (`service install`)

`buddy-bridge service install` registers the daemon to start at logon;
`service uninstall` removes it again. No admin rights needed:

- **Windows** — tries a `schtasks` ONLOGON task first. Task creation is
  denied on some locked-down accounts ("Access is denied"); the installer
  then falls back to a `buddy-bridge.vbs` launcher in the user Startup
  folder that runs the daemon with a hidden window. Uninstall removes
  both, whichever half exists.
- **macOS** — writes a launchd user agent
  (`~/Library/LaunchAgents/com.buddy-bridge.daemon.plist`, RunAtLoad +
  KeepAlive) and `launchctl load`s it.
- **Linux/other** — unsupported; run `buddy-bridge daemon` from a systemd
  user unit or similar.

The launcher prefers `pythonw.exe` next to your interpreter so nothing
flashes a console at logon.

## Sharing one stick between machines

BLE allows a single central, so only one bridge can hold the stick at a
time — but the stick keeps up to 7 bonds, and in its AUTO host mode the
first bonded host to connect wins. Two host-side features make several
machines share it hands-free:

### Yield when idle (`[ble] idle_yield_min`)

```toml
[ble]
idle_yield_min = 5   # minutes; 0 (default) = never yield
```

Exclusive mode only: after N minutes with no running/waiting session, no
pending prompt, and no queued card, the daemon pushes one last clean
snapshot, disconnects, and drops to one reconnect attempt per minute —
freeing the stick for whichever machine gets busy next. **Any** local
activity (a hook event, a permission prompt, a new card) instantly exits
yield with a normal fast reconnect burst; if another host has taken the
stick meanwhile, the attempts stay patient at 60 s while your sessions
keep mirroring locally. `buddy-bridge status` reports `ble_yielding`.

### LAN relay federation (`[relay]`)

Machines running buddy-bridge on the same trusted LAN can federate: the
bridge currently holding the stick (the *holder*) shows cards for the
other machines' pending prompts, and hands the stick over when a peer
needs it. Two-machine example — on **both** `alpha` and `bravo`:

```toml
[ble]
idle_yield_min = 5

[relay]
enabled = true
port = 48901
token = "pick-one-long-random-string"   # REQUIRED, same on every machine
```

Restart the daemons (`buddy-bridge status` shows the relay view). What
you get:

- Peers find each other via signed UDP broadcasts on `port` (every 30 s,
  immediately on holder changes; a silent peer expires after 120 s).
- While `alpha` holds the stick and a prompt waits on `bravo`, the stick
  shows a dim `remote` card — `[bravo] approve: Bash` plus the hint —
  rate-limited to 1 card per peer per 10 s and deduped. When `bravo`'s
  prompt resolves, an undelivered card is retracted.
- If `alpha` is idle when `bravo`'s prompt lands, `alpha` yields the
  stick immediately (no need to wait out `idle_yield_min`) and `bravo`'s
  reconnect grabs it — switchover typically 5–10 s.
- **No remote decisions in v1**: the card is informational; you answer on
  the machine that asked (its native CLI prompt, or its own stick session
  once it holds the link). A future revision may relay decisions.

Security model (trusted LAN, shared secret):

- The `token` is a shared secret; **every** discovery datagram and TCP
  frame is HMAC-SHA256 signed with it. Unsigned, tampered, wrong-token or
  stale (>120 s skew) input is dropped silently — keep machine clocks
  roughly in sync (NTP is fine).
- No credentials, transcripts, or decisions ever ride the relay — only
  host names, tool names, prompt hints (<= 80 chars), and session ids.
- The relay binds `0.0.0.0` **only** while `enabled = true` *and* the
  token is non-empty; otherwise zero sockets are opened (test-asserted).
  Treat the LAN as the trust boundary: don't enable it on networks you
  don't control, and rotate the token like any shared secret.

## WiFi provisioning

```powershell
buddy-bridge wifi <ssid>         # prompts for the password
```

Sends `{"cmd":"wifi","ssid":...,"pass":...}` to the connected buddy over
the running daemon's IPC and waits up to 10 s for
`{"ack":"wifi","ok":true|false}`. Requires protocol v2 — a stopped daemon,
a disconnected link, or a v1-only stick all print a clear error and exit
1, and the password is never requested in that case. The password is read
with `getpass` (no terminal echo): it never appears on the command line
or in shell history, and is never written to a log line, `audit.jsonl`,
or `cards.log`.

## Protocol v2

Every connection opens with a `hello`; a stick that acks `proto >= 2` gets
the richer v2 stream per `PROTOCOL_V2.md` (negotiated line budget, a
per-session `sessions[]` heartbeat with pinned `cli:N`/`cdx:N` ids, prompt
`sid`/`qn` routing, `prompt_cancel`, and the rxack dead-link watch). No ack
within 2 s means a v1-only stick: 900-byte lines and aggregate counts,
exactly as before.

## Info cards (opt-in)

With `[cards] enabled = true` in the config, the daemon can push `ntfy`
cards to sticks that advertise the `ntfy` capability:

```toml
[cards]
enabled = true
github = true                      # unread gh notifications (needs gh CLI)
update_check = true                # firmware update-check card (default on)
firmware_repo = "arikyeo/sticks3-buddy"  # override to point at a fork
repos = ["arikyeo/sticks3-buddy"]  # optional: CI conclusion cards per repo
weather_lat = 1.3521               # optional: Open-Meteo current weather
weather_lon = 103.8198
```

GitHub cards go through the `gh` CLI (missing/unauthenticated `gh` degrades
gracefully); weather polls the keyless Open-Meteo API every 30 min. Cards
are deduped by content for 6 h and appended to `~/.buddy-bridge/cards.log`
(tiny, one rotation). Nothing does network I/O while `enabled = false`.

`update_check` (on by default once `enabled = true`) polls
`https://api.github.com/repos/<firmware_repo>/releases/latest` every 6 h
(keyless GitHub REST API via `urllib`, no `gh` dependency) and compares
the release tag against the connected device's advertised firmware
version (the `hello` ack's `data.fw`). A strictly newer release pushes
one `"update"` ntfy card (`fw <tag> available` / `menu > update to
install`) that the firmware renders; the same tag is never carded twice,
and nothing fires until a v2 device has completed the hello handshake
(unknown firmware version = nothing to compare against).

## Permission gate (fail-open by design)

`PreToolUse` (Claude, install-gated by `[claude] gate = true`) and
`PermissionRequest` (Codex) can route tool approvals to the stick. Rules in
`~/.buddy-bridge/matchers.toml`:

```toml
auto_allow = ["Bash:git status*"]   # instant allow, still audited
always_ask = ["Bash:*rm *"]         # always routed to the device
strict     = ["Bash:git push*"]     # device-tier that auto_allow can never override
```

Precedence: `always_ask` > `auto_allow` > `strict` > default. Unmatched
tools fall through to the agent's **native** prompt.

**Fail-open guarantees** — the buddy can only ever *add* a faster approval
path, never take one away or block your CLI:

- Hook scripts import only the stdlib + a socket client with a 0.5 s connect
  timeout; every failure is a silent no-op and the hook always exits 0.
- The gate prints a decision **only** when the device answered allow/deny.
  Daemon down, BLE disconnected, mode off, unmatched tool, device timeout
  (300 s Claude / 105 s Codex), or a dead session — all print nothing, so the
  native permission prompt appears exactly as if buddy-bridge weren't there.
- Safe interaction tools (`AskUserQuestion`, `ExitPlanMode`, `TodoWrite`,
  `TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`) are never gated.
  `AskUserQuestion` gets special treatment: a non-blocking hook entry
  (installed regardless of the gate setting) forwards the question payload
  so a v2 stick with the `ask` capability *displays* the question —
  header, text, and options — while you answer in the CLI as usual. The
  display clears when you answer (or the turn/session ends); v1 sticks
  and sticks without the capability see nothing new.
- `BUDDY_BRIDGE_NOGATE=1` disables gating per invocation without uninstalling.
- A device deny tells the agent: *"User denied on Hardware Buddy device. Do
  not retry with alternative phrasings."*

## Audit log

Every gate outcome appends one JSON line to `~/.buddy-bridge/audit.jsonl`:
`ts, host, agent, sid, tool, hint` (exactly as shown on the device),
`decision`, and `source`:

| source | meaning |
|---|---|
| `device` | decided on the stick's buttons |
| `auto_allow` | matched an `auto_allow` rule |
| `timeout_fallback` | device never answered; fell back to native prompt |
| `daemon_down` | BLE off/disconnected; fell back to native prompt |

## Tokens

Output tokens are summed from `~/.claude/projects/**/*.jsonl` (tailed live,
byte offsets persisted — history is never replayed or double-counted) and
`~/.codex/sessions/**/*.jsonl` (60 s scans of the cumulative counters).
`~/.buddy-bridge/ledger.json` keeps lifetime and per-day totals; "today"
rolls over at local midnight.

## Tests

```powershell
python -m pytest host/tests -q
python -m ruff check host
```

No hardware needed; hook installer tests run against temp files only.

## Credits

BLE reconnect/chunking/radio-recovery patterns vendored from
[SnowWarri0r/cc-buddy-bridge](https://github.com/SnowWarri0r/cc-buddy-bridge)
(MIT); Codex hook stdout discipline ported from
[Yamiqu/codex-buddy-bridge](https://github.com/Yamiqu/codex-buddy-bridge) (MIT).
