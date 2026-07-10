"""Autostart install/uninstall for the bridge daemon (no admin required).

Windows — two tiers, best first:
  1. ``schtasks /Create /SC ONLOGON`` (survives Startup-folder cleanups).
     Creating a logon task is denied on some machines (locked-down accounts
     return "Access is denied" without elevation), so on ANY failure:
  2. a ``buddy-bridge.vbs`` launcher in the user's Startup folder that
     WScript-runs the daemon with a hidden window (style 0) — works for
     plain user accounts everywhere.
  Uninstall removes BOTH (whichever half exists).

macOS — a launchd user agent at
``~/Library/LaunchAgents/com.buddy-bridge.daemon.plist`` (RunAtLoad +
KeepAlive), loaded/unloaded via ``launchctl``.

Elsewhere — unsupported; run ``buddy-bridge daemon`` from your init system
(e.g. a systemd user unit) instead.

Launchers prefer ``pythonw.exe`` next to the current interpreter so no
console window flashes at logon; the VBS tier hides the window either way.
Everything is parameterized (platform / env / home / subprocess runner) so
tests never touch the real machine.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

TASK_NAME = "buddy-bridge"
VBS_NAME = "buddy-bridge.vbs"
LAUNCHD_LABEL = "com.buddy-bridge.daemon"
_SCHTASKS_TIMEOUT = 15.0

Runner = Callable[..., "subprocess.CompletedProcess"]


def _run(cmd: list[str], runner: Runner) -> subprocess.CompletedProcess:
    return runner(cmd, capture_output=True, text=True, timeout=_SCHTASKS_TIMEOUT)


def daemon_python(python_exe: Optional[str] = None, *, windows: bool = True) -> str:
    """Interpreter for the launcher: pythonw.exe beside the given/current
    python on Windows when it exists (silent), else the interpreter itself.
    The input string is passed through untouched unless pythonw is found —
    no Path round-trip that would rewrite separators for a foreign OS."""
    py = python_exe or sys.executable
    if windows:
        pythonw = Path(py).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(py)


def startup_vbs_path(env: Optional[dict] = None) -> Optional[Path]:
    """``%APPDATA%\\...\\Startup\\buddy-bridge.vbs`` (None without APPDATA)."""
    appdata = (env if env is not None else os.environ).get("APPDATA", "").strip()
    if not appdata:
        return None
    return (
        Path(appdata)
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / VBS_NAME
    )


def vbs_content(python_exe: str) -> str:
    quoted = f'""{python_exe}"" -m buddy_bridge daemon'
    return (
        "' buddy-bridge autostart launcher (managed - `buddy-bridge service uninstall`"
        " removes this file)\n"
        f'CreateObject("WScript.Shell").Run "{quoted}", 0, False\n'
    )


def launchd_plist_path(home: Optional[Path] = None) -> Path:
    base = home if home is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(python_exe: str) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [python_exe, "-m", "buddy_bridge", "daemon"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
        }
    )


# ---- install ----


def _install_windows(
    python_exe: Optional[str], runner: Runner, env: Optional[dict]
) -> tuple[int, list[str]]:
    exe = daemon_python(python_exe, windows=True)
    command = f'"{exe}" -m buddy_bridge daemon'
    try:
        proc = _run(
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME,
             "/TR", command],
            runner,
        )
        schtasks_err = (proc.stderr or proc.stdout or "").strip()
        schtasks_ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        schtasks_ok, schtasks_err = False, str(exc)
    if schtasks_ok:
        return 0, [
            f"installed scheduled task {TASK_NAME!r} (runs at logon)",
            "start it now with: buddy-bridge daemon",
        ]
    vbs = startup_vbs_path(env)
    if vbs is None:
        return 1, [
            f"schtasks failed ({schtasks_err or 'unknown error'}) and APPDATA is unset; "
            "nothing installed",
        ]
    vbs.parent.mkdir(parents=True, exist_ok=True)
    vbs.write_text(vbs_content(exe), encoding="utf-8")
    return 0, [
        f"schtasks unavailable ({schtasks_err or 'unknown error'})",
        f"installed Startup-folder launcher instead: {vbs}",
        "start it now with: buddy-bridge daemon",
    ]


def _uninstall_windows(runner: Runner, env: Optional[dict]) -> tuple[int, list[str]]:
    messages: list[str] = []
    try:
        proc = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], runner)
        if proc.returncode == 0:
            messages.append(f"removed scheduled task {TASK_NAME!r}")
    except (OSError, subprocess.SubprocessError):
        pass
    vbs = startup_vbs_path(env)
    if vbs is not None and vbs.exists():
        try:
            vbs.unlink()
            messages.append(f"removed Startup-folder launcher {vbs}")
        except OSError as exc:
            return 1, messages + [f"could not remove {vbs}: {exc}"]
    if not messages:
        messages.append("nothing installed; nothing removed")
    return 0, messages


def _install_macos(
    python_exe: Optional[str], runner: Runner, home: Optional[Path]
) -> tuple[int, list[str]]:
    exe = daemon_python(python_exe, windows=False)
    plist = launchd_plist_path(home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(launchd_plist(exe))
    messages = [f"wrote launch agent {plist}"]
    try:
        proc = _run(["launchctl", "load", "-w", str(plist)], runner)
        if proc.returncode == 0:
            messages.append("loaded via launchctl (daemon starts now and at login)")
        else:
            messages.append(
                f"launchctl load failed ({(proc.stderr or '').strip() or proc.returncode}); "
                "it will load at next login"
            )
    except (OSError, subprocess.SubprocessError) as exc:
        messages.append(f"launchctl unavailable ({exc}); the agent loads at next login")
    return 0, messages


def _uninstall_macos(runner: Runner, home: Optional[Path]) -> tuple[int, list[str]]:
    plist = launchd_plist_path(home)
    messages: list[str] = []
    if plist.exists():
        try:
            _run(["launchctl", "unload", "-w", str(plist)], runner)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            plist.unlink()
            messages.append(f"removed launch agent {plist}")
        except OSError as exc:
            return 1, [f"could not remove {plist}: {exc}"]
    if not messages:
        messages.append("nothing installed; nothing removed")
    return 0, messages


def install(
    python_exe: Optional[str] = None,
    *,
    platform: Optional[str] = None,
    runner: Runner = subprocess.run,
    env: Optional[dict] = None,
    home: Optional[Path] = None,
) -> tuple[int, list[str]]:
    plat = platform or sys.platform
    if plat == "win32":
        return _install_windows(python_exe, runner, env)
    if plat == "darwin":
        return _install_macos(python_exe, runner, home)
    return 2, [
        f"autostart install is not supported on {plat}; run `buddy-bridge daemon` "
        "from your init system (e.g. a systemd user unit)",
    ]


def uninstall(
    *,
    platform: Optional[str] = None,
    runner: Runner = subprocess.run,
    env: Optional[dict] = None,
    home: Optional[Path] = None,
) -> tuple[int, list[str]]:
    plat = platform or sys.platform
    if plat == "win32":
        return _uninstall_windows(runner, env)
    if plat == "darwin":
        return _uninstall_macos(runner, home)
    return 2, [f"autostart uninstall is not supported on {plat}"]
