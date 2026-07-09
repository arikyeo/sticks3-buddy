# buddy-bridge

Windows-first Python host that bridges **Claude Code CLI** and **Codex CLI**
to an M5 StickS3 BLE desk buddy: live session states, activity feed, token
counts, and (opt-in) hardware permission approvals on the stick's buttons.

Requires Python >= 3.11. Runtime state lives in `~/.buddy-bridge/`
(override with `BUDDY_BRIDGE_HOME`), never in the repo.

## Install

```powershell
pipx install ./host              # or: pip install ./host
pipx inject buddy-bridge "buddy-bridge[win32]"   # optional: BT radio auto-recovery
```

From a source checkout for development:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e "host[dev]"
```

## Setup

```powershell
buddy-bridge setup               # writes ~/.buddy-bridge/config.toml, mints a host id
buddy-bridge daemon              # run the bridge (foreground; service install = later phase)
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

`buddy-bridge mode exclusive|ondemand|off` (restart the daemon to apply):

- **exclusive** (default) — hold the connection whenever the buddy is around;
  reconnect with 3 s -> 60 s backoff.
- **ondemand** — connect only while there is something to deliver (e.g. a
  pending permission), drop after 2 min idle.
- **off** — never touch the radio; mirrors and token accounting keep working.

On Windows, 5 consecutive scan misses trigger an automatic Bluetooth radio
power-cycle (max 3 attempts, 120 s cooldown) to unstick the WinRT scanner —
this briefly drops *all* BT devices, so it is deliberately rationed.

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
