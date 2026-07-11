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

### Relay federation (`[relay]`): every machine on one stick

Machines running buddy-bridge on the same trusted network federate into
one view: the bridge currently holding the stick (the *holder*) shows a
**merged** session list for every machine, surfaces the oldest pending
prompt across all of them, and routes your button press back to the
machine that asked. You approve or deny *any* machine's prompt from the
stick, wherever it is plugged in.

Three-machine walkthrough — `alpha` (Windows desktop, usually holds the
stick), `bravo` (Windows laptop), `carol` (a Mac). On **every** machine,
the same `[relay]` block:

```toml
[ble]
idle_yield_min = 5

[relay]
enabled = true
port = 48901
token = "pick-one-long-random-string"   # REQUIRED, same on every machine
name_short = "alpha"                    # optional, <= 6 chars; hostname otherwise
```

On the Mac, install and autostart the same way as anywhere else:

```bash
uv tool install ./host        # or: pipx install ./host
buddy-bridge setup            # then paste the same [relay] block
buddy-bridge hooks install claude && buddy-bridge hooks install codex
buddy-bridge service install  # launchd user agent, starts at login
```

Restart the daemons (`buddy-bridge status` shows the relay + federation
view). What you get:

- Peers find each other via signed UDP broadcasts on `port` (every 30 s,
  immediately on holder changes; a silent peer expires after 120 s).
- Non-holders stream their full state to the holder (`state_sync`: on
  change, 5 s keepalive). The stick's session list shows everyone —
  `[bravo] api-server`, `[carol] webapp` — waiting-first across all
  machines; remote idle sessions are the first dropped when the list
  overflows. A peer that goes silent for 30 s vanishes from the view.
- The heartbeat prompt is the **oldest waiting prompt across all
  machines**, with `qn` counting the merged queue behind it. Press the
  button: a local prompt resolves as always, a remote one is relayed to
  its origin (retried 3x), whose gate returns allow/deny exactly as if
  its own stick had answered. The origin's clock still rules — if the
  relay or the holder dies mid-decision, the origin times out and fails
  open to the native CLI prompt, same as ever.
- A machine with **no stick at all** (`mode off`, or a failed ondemand
  connect) now waits for a federated decision when a v2 holder is on the
  air, instead of instantly falling open.
- Claude `AskUserQuestion` panels from any machine render read-only on
  the holder's stick, `[NAME]`-prefixed; you still answer them in that
  machine's CLI.
- Machines still running the previous buddy-bridge speak v1: their
  prompts appear as the old dim `remote` cards, and this version's card
  path is used when *the holder* is v1. Mixed networks work; upgrade the
  holder first to get the merged view.
- Yield still works: an idle holder hands the stick to a busy peer, and
  a machine whose prompt you keep answering remotely never needs it.

#### Overlay networks (Tailscale / WireGuard / routed subnets)

Discovery uses UDP broadcast, which doesn't cross routed or overlay
networks. Add static peers and the same signed discovery datagram is
also **unicast** to each of them (30 s cadence, instantly on holder
flips) — broadcast stays on, so LAN mates and remote peers mix freely:

```toml
[relay]
enabled = true
token = "pick-one-long-random-string"
peers = [
  "desktop.tail1234.ts.net",       # MagicDNS names re-resolve at send time
  "mbp.tail1234.ts.net",
  "100.64.0.7:48901",              # or plain IPv4, optionally with a port
]
```

List every *other* machine on each host (a self-entry is harmless — own
datagrams are ignored). Names that don't resolve are skipped quietly and
retried on the next announce; a successful TCP exchange also refreshes
peer liveness, so a link where UDP only passes one way stays federated.
Everything else (state_sync, decisions) is already unicast TCP and needs
no extra configuration. Tailscale passes both fine.

#### Threat model (read before enabling)

The relay v2 crosses a real line: **decisions ride the network**, so the
`[relay]` token now guards approval authority, not just display text.

- **Every** datagram and TCP frame is HMAC-SHA256 signed over its
  canonical JSON body with the shared token. Unsigned, tampered,
  wrong-token or stale (>120 s skew) input is dropped without a reply —
  keep machine clocks roughly in sync (NTP is fine).
- **Whoever holds the token can forge an "allow"** for any gated command
  on any federated machine. Make it long and random
  (`python -c "import secrets;print(secrets.token_urlsafe(32))"`),
  treat it like an SSH private key, rotate it if a machine is retired or
  the network changes. The relay refuses to open any socket while the
  token is empty (test-asserted).
- Static peers extend the trust boundary to **wherever the token goes**:
  the supported way to cross the open internet is a WireGuard/Tailscale
  overlay (use Tailscale ACLs to pin which nodes may reach the port).
  Never port-forward the relay port to the raw internet — HMAC gates
  actions but won't stop replay probing, fingerprinting, or a leaked
  token becoming remote approval authority.
- What rides the relay is bounded: host names, session titles, tool
  names, pre-truncated prompt hints (<= 80 chars), ask texts, session
  ids, allow/deny verdicts. **Never** credentials, transcripts, file
  contents, or WiFi passwords. Hints are sanitized and capped at the
  origin, and the holder re-sanitizes everything before it touches the
  device (a peer's HMAC proves token possession, not well-formedness).
- Inbound remote decisions resolve only a *matching pending id* — a
  forged or replayed id that isn't in flight is a logged no-op, and the
  freshness window bounds replay of a real one. Remote cards are further
  rate-limited (1 per peer per 10 s).

Changes vs 0.1.x: `state_sync`/`decision` relay events, merged stick
view, remote approve/deny, ask relay, `[relay] peers` static unicast
discovery, `[relay] name_short`; every v1 relay message is still spoken
and v1 peers keep working (cards).

## Device link over WiFi/TCP (`[device_link]`)

The stick can reach the bridge over the LAN instead of BLE: the bridge
listens on `[device_link] port` (default 48902) and the stick — TCP
client — dials in whenever it is powered, on WiFi, and the host is
reachable. One command sets it up (stick connected over BLE first):

```powershell
buddy-bridge link-provision              # autodetects this machine's LAN IP
buddy-bridge link-provision --host 100.64.0.7 --port 48902   # overlay/static
buddy-bridge link-provision --clear      # stick forgets the link, BLE only
```

`link-provision` mints a `[device_link]` token if the config has none
(`secrets.token_urlsafe(32)`, persisted to `config.toml`, **never
printed** — not even once), enables the listener, and sends
`{"cmd":"link",host,port,token}` to the stick over the **encrypted BLE
link**. From then on:

- Stick connects over TCP and authenticates its first line
  (`hello_link`, HMAC-SHA256 over canonical JSON with a ±120 s freshness
  window — the exact relay signing scheme). Bad signature or stale
  timestamp: closed without a reply, and that IP's accepts are
  rate-limited (3/min). One device connection at a time; a newer valid
  connect replaces the older.
- While the WiFi link is up it is **the** active link: same hello
  negotiation, same heartbeats/prompts/asks/cards, and the BLE reconnect
  loop pauses and releases its connection — the stick's BLE radio is
  free for other centrals (e.g. the stock desktop app). The federation
  holder flag follows whichever transport is live, so a wifi-held stick
  federates normally. `buddy-bridge status` shows
  `link_transport: "ble" | "wifi" | "none"`.
- WiFi link drops: BLE reconnect resumes immediately; the stick falls
  back on its own. Gates over an active WiFi link work even with
  `mode off`.

### Threat model (read before enabling)

- The `[device_link]` token is **link authority**: whoever holds it can
  *be* the stick — receive your prompts and answer them. Same trust
  model as the relay token (trusted LAN or WireGuard/Tailscale overlay
  only; never port-forward the port to the raw internet; rotate on
  network changes). Rotate = `link-provision --clear`, blank the token
  in `config.toml`, re-provision.
- It is deliberately a **separate secret from `[relay] token`** —
  sharing one was considered and rejected: the relay token grants
  approval authority across machines, the device-link token impersonates
  the device; separate secrets keep the blast radius of a leak separate.
- The TCP pipe is plaintext after its HMAC handshake. What rides it
  post-auth is exactly what the encrypted BLE link carries — session
  titles, prompt hints, decisions — and nothing more. **WiFi passwords
  never traverse the device link** (`buddy-bridge wifi` refuses unless
  the encrypted BLE link is up), and the device-link token itself is
  only ever provisioned over BLE, never over the TCP pipe it protects.
  `--clear` carries no secret, so it may ride either transport.
- Zero sockets unless `[device_link] enabled = true` AND the token is
  non-empty (test-asserted, same rule as the relay).

## WiFi provisioning

```powershell
buddy-bridge wifi <ssid>              # upsert: prompts for the password
buddy-bridge wifi <ssid> --remove     # drop one stored network
buddy-bridge wifi --list              # show SSIDs stored on the device
buddy-bridge wifi --clear             # drop every stored network (confirms)
buddy-bridge wifi --import creds.json # bulk-load several networks
```

The device keeps a small list of networks, upserted by SSID (re-sending an
SSID updates its password in place rather than adding a duplicate). Every
op is one of `{"cmd":"wifi",...}` sent to the connected buddy over the
running daemon's IPC, acknowledged with `{"ack":"wifi","ok":...}` (10 s
timeout). Every one of these requires protocol v2 — a stopped daemon, a
disconnected link, or a v1-only stick print a clear error and exit 1
before anything is requested (no password prompt in that case, and
`--clear` won't even ask you to confirm). A password is only ever read
with `getpass` (no terminal echo): it never appears on the command line,
in shell history, or in a log line, `audit.jsonl`, or `cards.log` —
`--remove`/`--list`/`--clear` never prompt for one at all.

The device holds up to 256 networks; beyond that it rejects further
upserts (`ok:false, error:"full"`) instead of evicting an older one, so
provisioning a network never silently drops a different one.

### Bulk import (`--import`)

```powershell
buddy-bridge wifi --import creds.json
buddy-bridge wifi --import creds.csv
buddy-bridge wifi --import wpa_supplicant.conf
```

Sends one upsert per network, **sequentially** — each ack is awaited
before the next network is sent — in the file's order. Format is
auto-detected:

- **JSON list**: `[{"ssid": "Home", "pass": "hunter2"}, {"ssid": "Office", "pass": "..."}]`
  (`"password"` is also accepted as an alias for `"pass"`).
- **JSON map**: `{"Home": "hunter2", "Office": "..."}`
- **CSV/TSV**: one `ssid,pass` (or tab-separated `ssid<TAB>pass`) pair per
  line; blank lines and lines starting with `#` are skipped. Fields with
  embedded commas/tabs need quoting (`"My, Home Net",p@ss,word`) — parsed
  with Python's `csv` module, so standard quoting rules apply. There is no
  header row; put `#` in front of one to skip it.
- **wpa_supplicant.conf** (best-effort): `network={ ssid="..." psk="..." }`
  blocks are scanned for quoted `ssid`/`psk` pairs. A block with a raw-hex
  `psk` (64 hex chars, unquoted) or no `psk` at all (open/EAP networks) is
  skipped with a warning — only a quoted ASCII passphrase can be resent to
  the device.

Any row/entry that doesn't parse (missing field, wrong JSON shape, an
unsupported wpa_supplicant block, ...) is skipped with a warning naming
its SSID — one bad line doesn't abort the rest of the file. A file that
can't be read or doesn't resemble any supported format at all (missing,
empty, non-UTF8, top-level JSON that's neither a list nor an object)
prints one clear error and exits 1 instead of a traceback.

If the file has more than 256 networks, `buddy-bridge` warns up front
(rare — the device supports 256); if the device is already close to full,
import stops the moment it gets back `error:"full"` rather than
continuing to hammer a full device, and reports how many networks were
never sent (by name). The final line reports how many networks were
imported and the device's resulting count, and reminds you to delete the
import file — **passwords in it are plain text**, and once they're on the
device (over the encrypted BLE link) the host never keeps a copy.

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

## Telegram bridge (opt-in): a virtual buddy in your pocket

With `[telegram]` configured, the daemon becomes reachable from anywhere:
push notifications for the same events the stick sees, live session
queries, and — behind separate opt-ins — answering permission prompts and
Claude's `AskUserQuestion` right from the chat. Transport is the Telegram
Bot HTTP API over the stdlib (`urllib` in a worker thread): no new
dependency, and one `getUpdates` long-poll task that backs off on errors
(1 s → 60 s) and can never crash or block the daemon. Every send is
best-effort: retry once, then drop quietly.

```powershell
buddy-bridge telegram setup    # BotFather walkthrough; token via hidden input
buddy-bridge telegram test     # live check: getMe + one message per chat
buddy-bridge telegram status   # config + daemon view (token never printed)
```

```toml
[telegram]
enabled = true
bot_token = "..."          # from @BotFather — see threat model below
allowed_chats = [123456789] # REQUIRED: numeric chat ids; everyone else ignored
events = ["prompt"]         # any of: prompt attention done card update
allow_decisions = false     # inline [Allow once][Deny] buttons (opt-in)
answer_asks = false         # answer AskUserQuestion from Telegram (opt-in)
```

Commands (allowlisted chats only): `/sessions` and `/status` show the
merged session list across this machine **and every federated peer** (the
holder view — the same data the stick dashboard renders): state glyph
(⏳ waiting / ▶ running / · idle), agent letter (C/X), host, title, token
count, last activity line. `/pending` lists only what's waiting, each item
with its inline buttons. `/mute [min]` / `/unmute` pause pushes per chat
(commands still answered). `/help` lists everything.

**Answering permission prompts** (`allow_decisions = true`): the buttons
route through the exact same `DecisionRouter` path as a stick button press
— wire id resolves the pending future, the gate's fail-open timeout still
rules, and a federated peer's prompt is relayed home precisely like a
stick answer from the holder. Nothing new becomes allowable; Telegram is
just another button.

**Answering `AskUserQuestion`** (`answer_asks = true`, then re-run
`buddy-bridge hooks install claude`): the question arrives with one button
per option (multi-select: toggle + Submit). The mechanism uses the
documented hook contract — the `AskUserQuestion` `PreToolUse` hook becomes
*blocking* and, when you pick an option, returns `permissionDecision:
"allow"` with `updatedInput` = the original `questions` plus an `answers`
map keyed by the verbatim question text, which Claude Code accepts as the
answer and skips the terminal dialog. **The honest limits:** while the
hook waits (up to the `claude_decision` timeout, 300 s default) the
terminal dialog is *deferred* — answer on Telegram or wait out the
timeout, after which the native dialog appears exactly as before (fail
open; the CLI can never brick). A hook cannot answer a question that has
already reached the terminal dialog, free-text "Other" replies aren't
supported over Telegram (labels only), and federated peers' asks stay
display-only (the federation protocol has no ask-answer channel).

### Threat model (read before enabling)

- **The bot token is full control authority.** Anyone holding it reads
  every notification (prompt hints include commands and paths) and — with
  the opt-ins on — can approve permission prompts and answer questions.
  Treat it like an SSH private key: use a **dedicated bot** for this, and
  assume token compromise = approval forgery. Rotate it at @BotFather if
  in doubt. The daemon never logs or prints it (`telegram status` shows
  only `set (hidden)`; transport failures log method + status code only).
- **The allowlist is required, not optional.** `enabled = true` without a
  token *and* at least one `allowed_chats` entry stays OFF (warned at
  startup). Updates from non-allowlisted chats are ignored and the chat id
  logged once; no reply of any kind is sent.
- Chat *content* and message text are never written to the daemon log.
- `allow_decisions` and `answer_asks` default **off**: out of the box the
  bridge is read-only push + query.
- Every remote decision and every answered ask appends to
  `~/.buddy-bridge/audit.jsonl` with `source: "telegram"` and `by: <chat
  id>`, alongside the existing device/auto-allow records.
- Commands sent while the daemon is down are dropped on restart (the
  backlog is discarded), so a queued-up "Allow" can never fire late.

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
`decision`, `source`, and — for telegram — `by` (the chat id):

| source | meaning |
|---|---|
| `device` | decided on the stick's buttons |
| `telegram` | decided/answered from an allowlisted Telegram chat (`by` = chat id) |
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
