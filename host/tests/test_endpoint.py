import os
import socket
import subprocess
import sys

from buddy_bridge.ipc.endpoint import (
    is_stale,
    load_live_endpoint,
    pid_alive,
    read_endpoint,
    write_endpoint,
)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_write_read_roundtrip(home):
    write_endpoint(home, 12345, "tok", 999)
    ep = read_endpoint(home)
    assert ep == {"port": 12345, "token": "tok", "pid": 999}


def test_read_garbage(home):
    assert read_endpoint(home) is None  # missing
    (home / "endpoint.json").write_text("{oops", encoding="utf-8")
    assert read_endpoint(home) is None
    (home / "endpoint.json").write_text('{"port": "x", "token": "t", "pid": 1}', encoding="utf-8")
    assert read_endpoint(home) is None


def test_pid_alive_self_and_dead():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(_dead_pid()) is False
    assert pid_alive(-5) is False


def test_stale_dead_pid(home):
    write_endpoint(home, 1, "tok", _dead_pid())
    assert is_stale(read_endpoint(home)) is True
    assert load_live_endpoint(home) is None


def test_stale_live_pid_no_listener(home):
    # grab a port that is then closed again -> nothing listening
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    write_endpoint(home, port, "tok", os.getpid())
    assert is_stale(read_endpoint(home)) is True
    assert load_live_endpoint(home) is None


def test_live_endpoint(home):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        # backlog > 1: each probe consumes a slot (never accepted) and on
        # Windows a used slot is not freed by the peer closing
        listener.listen(8)
        port = listener.getsockname()[1]
        write_endpoint(home, port, "tok", os.getpid())
        ep = read_endpoint(home)
        assert is_stale(ep) is False
        assert load_live_endpoint(home) == ep
