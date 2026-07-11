"""buddy-bridge CLI.

Subcommands: daemon | status | probe | pair | mode | wifi | sessions |
hooks install/uninstall claude|codex | service install/uninstall | doctor | setup
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import bridge_home, load_config, save_config
from .ipc import client as ipc_client
from .ipc.endpoint import load_live_endpoint, read_endpoint

_BLE_MODES = ("exclusive", "ondemand", "off")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buddy-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"buddy-bridge {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="run the bridge daemon in the foreground")
    sub.add_parser("status", help="query the running daemon")
    sub.add_parser("sessions", help="list mirrored agent sessions")
    p_probe = sub.add_parser("probe", help="scan for buddy devices")
    p_probe.add_argument("--timeout", type=float, default=5.0)
    p_pair = sub.add_parser("pair", help="pin a device address into the config")
    p_pair.add_argument("--address", help="skip scanning and pin this address")
    p_pair.add_argument("--timeout", type=float, default=5.0)
    p_mode = sub.add_parser("mode", help="get or set BLE mode")
    p_mode.add_argument("value", nargs="?", choices=_BLE_MODES)
    p_wifi = sub.add_parser("wifi", help="provision/manage WiFi credentials on the connected buddy")
    p_wifi.add_argument(
        "ssid", nargs="?", help="WiFi network name (SSID); password is prompted. "
        "Omit with --list/--clear/--import"
    )
    p_wifi.add_argument(
        "--remove", action="store_true", help="remove the given SSID instead of provisioning it"
    )
    p_wifi.add_argument(
        "--list", action="store_true", help="list SSIDs stored on the device (never prompts)"
    )
    p_wifi.add_argument(
        "--clear", action="store_true", help="remove every stored SSID (prompts to confirm)"
    )
    p_wifi.add_argument(
        "--import", dest="import_file", metavar="FILE",
        help="bulk-import networks from a JSON/CSV/TSV/wpa_supplicant file"
    )
    p_hooks = sub.add_parser("hooks", help="install/uninstall agent hooks")
    p_hooks.add_argument("action", choices=("install", "uninstall"))
    p_hooks.add_argument("agent", choices=("claude", "codex"))
    p_svc = sub.add_parser("service", help="install/uninstall the background service")
    p_svc.add_argument("action", choices=("install", "uninstall"))
    sub.add_parser("doctor", help="diagnose the local setup")
    sub.add_parser("setup", help="create config + print next steps")

    args = parser.parse_args(argv)
    handler = {
        "daemon": cmd_daemon,
        "status": cmd_status,
        "sessions": cmd_sessions,
        "probe": cmd_probe,
        "pair": cmd_pair,
        "mode": cmd_mode,
        "wifi": cmd_wifi,
        "hooks": cmd_hooks,
        "service": cmd_service,
        "doctor": cmd_doctor,
        "setup": cmd_setup,
    }[args.cmd]
    return handler(args)


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run_daemon

    return run_daemon(load_config())


def _daemon_request(payload: dict, timeout: float = 3.0) -> dict | None:
    home = bridge_home()
    if load_live_endpoint(home) is None:
        return None
    return ipc_client.request(payload, timeout, home=home)


def cmd_status(args: argparse.Namespace) -> int:
    resp = _daemon_request({"event": "status"})
    if resp is None:
        stale = read_endpoint(bridge_home()) is not None
        print("daemon: not running" + (" (stale endpoint.json)" if stale else ""))
        return 1
    print(json.dumps(resp, indent=2, sort_keys=True))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    resp = _daemon_request({"event": "sessions"})
    if resp is None:
        print("daemon: not running")
        return 1
    sessions = resp.get("sessions", [])
    if not sessions:
        print("no active sessions")
        return 0
    for s in sessions:
        print(
            f"{s.get('agent', '?'):6} {s.get('state', '?'):5} "
            f"{s.get('title', '')!r:28} tok={s.get('tokens', 0)} sid={s.get('sid', '')}"
        )
    return 0


def _scan(prefix: str, timeout: float):
    import asyncio

    async def scan():
        from bleak import BleakScanner

        found = {}
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for device, adv in devices.values():
            name = (adv.local_name or device.name) or ""
            if name.startswith(prefix):
                found[device.address] = name
        return found

    return asyncio.run(scan())


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"scanning {args.timeout:.0f}s for name prefix {cfg.ble_name_prefix!r} ...")
    found = _scan(cfg.ble_name_prefix, args.timeout)
    if not found:
        print("no buddy devices found")
        return 1
    for addr, name in sorted(found.items()):
        print(f"  {addr}  {name}")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    cfg = load_config()
    address = args.address
    if not address:
        found = _scan(cfg.ble_name_prefix, args.timeout)
        if not found:
            print("no buddy devices found; nothing pinned")
            return 1
        if len(found) > 1:
            print("multiple devices found; re-run with --address <addr>:")
            for addr, name in sorted(found.items()):
                print(f"  {addr}  {name}")
            return 1
        address = next(iter(found))
    cfg.ble_address = address
    save_config(cfg)
    print(f"pinned buddy address {address} in {cfg.path}")
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.value is None:
        print(cfg.ble_mode)
        return 0
    cfg.ble_mode = args.value
    save_config(cfg)
    resp = _daemon_request({"event": "set_mode", "mode": args.value})
    if resp is not None and resp.get("ok"):
        print(f"ble mode set to {cfg.ble_mode} (applied to the running daemon)")
    else:
        print(f"ble mode set to {cfg.ble_mode} (no running daemon; applies on next start)")
    return 0


_WIFI_REQUEST_TIMEOUT_SECS = 12.0  # > daemon's own 10s device-ack wait + IPC round-trip headroom


def cmd_wifi(args: argparse.Namespace) -> int:
    """Provision or manage WiFi credentials on the connected buddy: a bare
    SSID upserts it (prompts for the password); --remove/--list/--clear/
    --import manage the device's stored network list. A password is only
    ever read via getpass (never a CLI argument, so it never lands in
    shell history or a process list), sent to the daemon over the local
    IPC socket, and never printed, logged, or written to
    audit.jsonl/cards.log."""
    exclusive = [bool(args.list), bool(args.clear), bool(args.import_file)]
    if sum(exclusive) > 1:
        print("wifi: --list, --clear and --import are mutually exclusive")
        return 1
    if (args.list or args.clear or args.import_file) and (args.ssid or args.remove):
        print("wifi: ssid/--remove are not used with --list/--clear/--import")
        return 1

    home = bridge_home()
    if args.list:
        return _cmd_wifi_list(home)
    if args.clear:
        return _cmd_wifi_clear(home)
    if args.import_file:
        return _cmd_wifi_import(args.import_file, home)
    if not args.ssid:
        print("wifi: ssid required (or use --list/--clear/--import)")
        return 1
    if args.remove:
        return _cmd_wifi_remove(args.ssid, home)
    return _cmd_wifi_upsert(args.ssid, home)


def _cmd_wifi_upsert(ssid: str, home: Path) -> int:
    import getpass

    # Check liveness before prompting: no point asking for a password that
    # has nowhere to go.
    if load_live_endpoint(home) is None:
        print("daemon: not running (start it with `buddy-bridge daemon`)")
        return 1
    password = getpass.getpass("WiFi password: ")
    resp = _daemon_request(
        {"event": "wifi", "ssid": ssid, "pass": password}, _WIFI_REQUEST_TIMEOUT_SECS
    )
    if resp is None:
        print("daemon: not running")
        return 1
    if not resp.get("ok"):
        print(f"wifi: {resp.get('error') or 'device rejected the credentials'}")
        return 1
    print(f"wifi: credentials sent to the buddy ({ssid!r})")
    return 0


def _cmd_wifi_remove(ssid: str, home: Path) -> int:
    if load_live_endpoint(home) is None:
        print("daemon: not running (start it with `buddy-bridge daemon`)")
        return 1
    resp = _daemon_request(
        {"event": "wifi", "ssid": ssid, "remove": True}, _WIFI_REQUEST_TIMEOUT_SECS
    )
    if resp is None:
        print("daemon: not running")
        return 1
    if not resp.get("ok"):
        print(f"wifi: {resp.get('error') or 'device rejected the request'}")
        return 1
    print(f"wifi: removed {ssid!r} (device now has {resp.get('n', '?')} network(s))")
    return 0


def _cmd_wifi_list(home: Path) -> int:
    if load_live_endpoint(home) is None:
        print("daemon: not running (start it with `buddy-bridge daemon`)")
        return 1
    resp = _daemon_request({"event": "wifi", "list": True}, _WIFI_REQUEST_TIMEOUT_SECS)
    if resp is None:
        print("daemon: not running")
        return 1
    if not resp.get("ok"):
        print(f"wifi: {resp.get('error') or 'device rejected the request'}")
        return 1
    ssids = resp.get("ssids") or []
    if not ssids:
        print("wifi: no networks stored on the device")
        return 0
    print(f"wifi: {len(ssids)} network(s) stored:")
    for s in ssids:
        print(f"  {s}")
    return 0


def _cmd_wifi_clear(home: Path) -> int:
    if load_live_endpoint(home) is None:
        print("daemon: not running (start it with `buddy-bridge daemon`)")
        return 1
    answer = input("remove ALL stored WiFi networks from the device? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("wifi: clear cancelled")
        return 0
    resp = _daemon_request({"event": "wifi", "clear": True}, _WIFI_REQUEST_TIMEOUT_SECS)
    if resp is None:
        print("daemon: not running")
        return 1
    if not resp.get("ok"):
        print(f"wifi: {resp.get('error') or 'device rejected the request'}")
        return 1
    print(f"wifi: cleared all networks (device now has {resp.get('n', 0)})")
    return 0


def _fmt_ssid_list(ssids: list[str], limit: int = 20) -> str:
    """Names only, comma-joined, capped so a pathological list can't flood
    the terminal with one giant line."""
    if len(ssids) <= limit:
        return ", ".join(repr(s) for s in ssids)
    shown = ", ".join(repr(s) for s in ssids[:limit])
    return f"{shown}, +{len(ssids) - limit} more"


def _cmd_wifi_import(import_file: str, home: Path) -> int:
    """Bulk-load networks from a JSON/CSV/TSV/wpa_supplicant file, sending
    one upsert per network sequentially (each ack awaited before the
    next). The device stores at most WIFI_MAX_ENTRIES networks and
    *rejects* further upserts once full (``{"ok":false,"error":"full"}``)
    rather than evicting anything, so a "full" ack stops the import
    immediately instead of hammering a device with no room left."""
    from .protocol import WIFI_MAX_ENTRIES
    from .wifi_import import WifiImportError, parse_wifi_import_file

    path = Path(import_file).expanduser()
    try:
        entries, warnings = parse_wifi_import_file(path)
    except WifiImportError as exc:
        print(f"wifi import: {exc}")
        return 1
    for w in warnings:
        print(f"wifi import: {w}")
    if not entries:
        print("wifi import: no networks found in file")
        return 1

    if len(entries) > WIFI_MAX_ENTRIES:
        overflow = [ssid for ssid, _ in entries[WIFI_MAX_ENTRIES:]]
        print(
            f"wifi import: file has {len(entries)} networks, device supports at most "
            f"{WIFI_MAX_ENTRIES}; entries beyond that will be rejected once the device "
            f"fills up ({_fmt_ssid_list(overflow)})"
        )

    if load_live_endpoint(home) is None:
        print("daemon: not running (start it with `buddy-bridge daemon`)")
        return 1

    imported = 0
    last_n: int | None = None
    remaining: list[str] = []
    device_full = False
    for i, (ssid, password) in enumerate(entries):
        resp = _daemon_request(
            {"event": "wifi", "ssid": ssid, "pass": password}, _WIFI_REQUEST_TIMEOUT_SECS
        )
        if resp is None:
            print(f"wifi import: daemon stopped responding before {ssid!r} was sent, aborting")
            remaining = [s for s, _ in entries[i:]]
            break
        if resp.get("ok"):
            imported += 1
            n = resp.get("n")
            if isinstance(n, int):
                last_n = n
            continue
        error = resp.get("error") or "device rejected the network"
        if error == "full":
            device_full = True
            remaining = [s for s, _ in entries[i:]]
            n = resp.get("n")
            if isinstance(n, int):
                last_n = n
            break
        print(f"wifi import: {ssid!r} failed: {error}")

    if device_full:
        at = last_n if last_n is not None else imported
        print(
            f"wifi import: device full at {at} networks, {len(remaining)} not imported: "
            f"{_fmt_ssid_list(remaining)}"
        )
    print(
        f"wifi import: imported {imported}/{len(entries)} networks"
        + (f" (device now has {last_n})" if last_n is not None else "")
    )
    print(
        f"wifi import: delete {path} now that its networks are on the device "
        "-- it stores passwords in plain text"
    )
    return 0 if imported == len(entries) else 1


def cmd_hooks(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.agent == "claude":
        from .install import claude_settings

        settings_path = Path.home() / ".claude" / "settings.json"
        if args.action == "install":
            changed = claude_settings.install(
                settings_path,
                gate=cfg.claude_gate,
                gate_tools=cfg.claude_gate_tools,
                answer_asks=cfg.telegram_answer_asks,
            )
            print(f"claude hooks {'installed into' if changed else 'already current in'} "
                  f"{settings_path}")
        else:
            removed = claude_settings.uninstall(settings_path)
            print(f"claude hooks {'removed from' if removed else 'not present in'} {settings_path}")
        return 0
    from .install import codex_hooks

    codex_dir = Path.home() / ".codex"
    if args.action == "install":
        changed = codex_hooks.install(codex_dir)
        print(f"codex hooks {'installed into' if changed else 'already current in'} "
              f"{codex_dir / 'hooks.json'}")
    else:
        removed = codex_hooks.uninstall(codex_dir)
        print(f"codex hooks {'removed from' if removed else 'not present in'} "
              f"{codex_dir / 'hooks.json'}")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from . import service

    if args.action == "install":
        code, messages = service.install()
    else:
        code, messages = service.uninstall()
    for line in messages:
        print(line)
    return code


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"python        : {sys.version.split()[0]} ({sys.executable})")
    home = bridge_home()
    print(f"home          : {home} ({'exists' if home.exists() else 'missing'})")
    cfg = load_config(create=False)
    print(f"config        : host id {cfg.host_id or '(unminted)'}, ble mode {cfg.ble_mode}, "
          f"claude gate {'on' if cfg.claude_gate else 'off'}, "
          f"codex gate {'on' if cfg.codex_gate else 'off'}")
    ep = read_endpoint(home)
    if ep is None:
        print("daemon        : not running (no endpoint.json)")
    elif load_live_endpoint(home) is None:
        print(f"daemon        : STALE endpoint (pid {ep['pid']}, port {ep['port']})")
    else:
        print(f"daemon        : running (pid {ep['pid']}, port {ep['port']})")
    try:
        import bleak  # noqa: F401

        print("bleak         : ok")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"bleak         : FAILED ({exc})")
    if sys.platform == "win32":
        try:
            from winrt.windows.devices import radios  # noqa: F401

            print("winrt radios  : ok (radio auto-recovery available)")
        except Exception:
            print("winrt radios  : missing (winrt ships as a core Windows dependency now — "
                  "reinstall buddy-bridge, or pip install \"buddy-bridge[win32]\")")
    for label, path in (
        ("claude hooks", Path.home() / ".claude" / "settings.json"),
        ("codex hooks", Path.home() / ".codex" / "hooks.json"),
    ):
        try:
            marked = "buddy_bridge.hooks." in path.read_text(encoding="utf-8")
        except OSError:
            marked = False
        print(f"{label:14}: {'installed' if marked else 'not installed'} ({path})")
    return 0 if ok else 1


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = load_config()  # mints host id + writes config on first run
    print(f"config ready at {cfg.path} (host id {cfg.host_id})")
    print("next steps:")
    print("  1. buddy-bridge daemon              # start the bridge (foreground)")
    print("  2. buddy-bridge probe               # confirm the buddy is advertising")
    print("  3. buddy-bridge hooks install claude")
    print("  4. buddy-bridge hooks install codex")
    print("  5. buddy-bridge doctor              # verify everything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
