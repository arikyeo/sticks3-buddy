"""Autostart service install: schtasks tier, Startup-folder fallback,
launchd path, and the python -m buddy_bridge entry point. All mocked —
no real schtasks/launchctl runs, no real Startup folder or LaunchAgents."""

import plistlib
import subprocess
import sys

from buddy_bridge import service


class RecordingRunner:
    """Stands in for subprocess.run; scripted per-command results."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.results:
            rc, err = self.results.pop(0)
        else:
            rc, err = 0, ""
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)


def _win_env(tmp_path):
    return {"APPDATA": str(tmp_path / "AppData" / "Roaming")}


def _vbs(tmp_path):
    return (
        tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs" / "Startup" / "buddy-bridge.vbs"
    )


# ---- windows ----


def test_windows_schtasks_success_no_vbs(tmp_path):
    runner = RecordingRunner([(0, "")])
    code, messages = service.install(
        "C:/py/python.exe", platform="win32", runner=runner, env=_win_env(tmp_path)
    )
    assert code == 0
    (cmd,) = runner.calls
    assert cmd[:2] == ["schtasks", "/Create"]
    assert "/SC" in cmd and "ONLOGON" in cmd
    assert service.TASK_NAME in cmd
    assert not _vbs(tmp_path).exists()
    assert any("scheduled task" in m for m in messages)


def test_windows_access_denied_falls_back_to_startup_vbs(tmp_path):
    runner = RecordingRunner([(1, "ERROR: Access is denied.")])
    code, messages = service.install(
        "C:/py/python.exe", platform="win32", runner=runner, env=_win_env(tmp_path)
    )
    assert code == 0
    vbs = _vbs(tmp_path)
    assert vbs.exists()
    content = vbs.read_text(encoding="utf-8")
    # WScript.Shell.Run, hidden window (0), doubled-quote path escaping
    assert 'CreateObject("WScript.Shell").Run' in content
    assert '""C:/py/python.exe"" -m buddy_bridge daemon' in content
    assert '", 0, False' in content
    assert any("Access is denied" in m for m in messages)
    assert any(str(vbs) in m for m in messages)


def test_windows_schtasks_missing_binary_falls_back(tmp_path):
    def runner(cmd, **kwargs):
        raise FileNotFoundError("schtasks not found")

    code, _ = service.install(
        "C:/py/python.exe", platform="win32", runner=runner, env=_win_env(tmp_path)
    )
    assert code == 0
    assert _vbs(tmp_path).exists()


def test_windows_no_appdata_reports_failure(tmp_path):
    runner = RecordingRunner([(1, "Access is denied.")])
    code, messages = service.install(
        "C:/py/python.exe", platform="win32", runner=runner, env={}
    )
    assert code == 1
    assert any("APPDATA" in m for m in messages)


def test_windows_uninstall_removes_both(tmp_path):
    env = _win_env(tmp_path)
    vbs = _vbs(tmp_path)
    vbs.parent.mkdir(parents=True)
    vbs.write_text("stub", encoding="utf-8")
    runner = RecordingRunner([(0, "")])
    code, messages = service.uninstall(platform="win32", runner=runner, env=env)
    assert code == 0
    (cmd,) = runner.calls
    assert cmd[:2] == ["schtasks", "/Delete"] and service.TASK_NAME in cmd
    assert not vbs.exists()
    assert len(messages) == 2  # task + vbs both reported


def test_windows_uninstall_nothing_installed(tmp_path):
    runner = RecordingRunner([(1, "task does not exist")])
    code, messages = service.uninstall(
        platform="win32", runner=runner, env=_win_env(tmp_path)
    )
    assert code == 0
    assert messages == ["nothing installed; nothing removed"]


def test_daemon_python_prefers_pythonw(tmp_path):
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    assert service.daemon_python(str(py), windows=True) == str(py)  # no pythonw yet
    pyw = tmp_path / "pythonw.exe"
    pyw.write_bytes(b"")
    assert service.daemon_python(str(py), windows=True) == str(pyw)
    assert service.daemon_python(str(py), windows=False) == str(py)


# ---- macOS ----


def test_macos_install_writes_plist_and_loads(tmp_path):
    runner = RecordingRunner([(0, "")])
    code, messages = service.install(
        "/usr/bin/python3", platform="darwin", runner=runner, home=tmp_path
    )
    assert code == 0
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.buddy-bridge.daemon.plist"
    assert plist_path.exists()
    data = plistlib.loads(plist_path.read_bytes())
    assert data["Label"] == "com.buddy-bridge.daemon"
    assert data["ProgramArguments"] == ["/usr/bin/python3", "-m", "buddy_bridge", "daemon"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    (cmd,) = runner.calls
    assert cmd[:2] == ["launchctl", "load"]
    assert any("loaded via launchctl" in m for m in messages)


def test_macos_uninstall_unloads_and_removes(tmp_path):
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.buddy-bridge.daemon.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(b"stub")
    runner = RecordingRunner([(0, "")])
    code, messages = service.uninstall(platform="darwin", runner=runner, home=tmp_path)
    assert code == 0
    assert not plist_path.exists()
    (cmd,) = runner.calls
    assert cmd[:2] == ["launchctl", "unload"]
    assert any("removed launch agent" in m for m in messages)


def test_macos_launchctl_failure_still_installs(tmp_path):
    runner = RecordingRunner([(1, "Load failed: 5")])
    code, messages = service.install(
        "/usr/bin/python3", platform="darwin", runner=runner, home=tmp_path
    )
    assert code == 0  # plist is in place; loads at next login
    assert any("next login" in m for m in messages)


# ---- other platforms ----


def test_unsupported_platform_exits_2():
    runner = RecordingRunner()
    code, messages = service.install(platform="linux", runner=runner)
    assert code == 2 and runner.calls == []
    code, _ = service.uninstall(platform="linux", runner=runner)
    assert code == 2 and runner.calls == []


# ---- python -m buddy_bridge ----


def test_python_dash_m_entrypoint_runs():
    proc = subprocess.run(
        [sys.executable, "-m", "buddy_bridge", "--version"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "buddy-bridge" in proc.stdout


def test_main_module_importable_without_side_effects():
    import buddy_bridge.__main__ as entry

    assert callable(entry.main)
