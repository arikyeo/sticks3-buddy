#!/usr/bin/env python3
"""Protocol v2 conformance harness, run against a live Stick over USB serial.

Covers the Track F P5 wire behavior end-to-end:
  1. pre-hello purity  - v1 lines and v2 cmds sent before hello produce no
                         v2 traffic (no rx acks, no unsolicited acks)
  2. hello handshake   - ack shape, proto negotiation, caps, maxLine/maxSessions
  3. rxack             - post-hello, host-capability gated, monotonic n
  4. sessions ingest   - heartbeat sessions[] lands in the device table
  5. prompt sid/qn     - v2 prompt targeting fields stored + prompt_cancel
  6. ask               - ask event stored (first question), ask_cancel clears

Checks 4-6 introspect the device with {"cmd":"debug_state"}, which only
exists in debug builds. Flash one first, e.g.:

    PLATFORMIO_BUILD_FLAGS=-DBUDDY_DEBUG pio run -e m5stickc-plus -t upload

NOTE: the StickS3 build does not parse JSON from its USB CDC port (the
ARDUINO_USB_MODE guard in data.h — BLE is its transport). Run this harness
against a StickC Plus / Plus2 over USB, or adapt it to a BLE NUS client
for the S3.

Usage:
    python tools/test_protocol.py [--port COM5] [--skip-debug]

--skip-debug runs only checks 1-3 (works on a release build).
"""
import argparse
import glob
import json
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial required: pip install pyserial")


def find_port(explicit):
    if explicit:
        return explicit
    for pattern in ("/dev/cu.usbserial-*", "/dev/cu.usbmodem*",
                    "/dev/ttyUSB*", "/dev/ttyACM*"):
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
        if ports:
            return ports[0]
    except Exception:
        pass
    sys.exit("no stick found - pass --port")


class Buddy:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.1)
        self.raw = []

    def send(self, obj):
        line = json.dumps(obj) if isinstance(obj, dict) else obj
        self.s.write((line + "\n").encode())

    def drain(self, seconds=1.0):
        """Collect parsed JSON lines for `seconds`. Non-JSON noise is kept in
        self.raw (boot logs etc.) but not returned."""
        out = []
        end = time.time() + seconds
        buf = b""
        while time.time() < end:
            chunk = self.s.read(256)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode(errors="replace").strip()
                if not text:
                    continue
                self.raw.append(text)
                if text.startswith("{"):
                    try:
                        out.append(json.loads(text))
                    except ValueError:
                        pass
        return out

    def expect_ack(self, name, seconds=2.0):
        for msg in self.drain(seconds):
            if msg.get("ack") == name:
                return msg
        return None

    def debug_state(self):
        self.send({"cmd": "debug_state"})
        state = hosts = None
        for msg in self.drain(2.0):
            if msg.get("dbg") == "state":
                state = msg
            elif msg.get("dbg") == "hosts":
                hosts = msg
        return state, hosts


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--skip-debug", action="store_true",
                    help="skip checks that need a -DBUDDY_DEBUG build")
    args = ap.parse_args()

    b = Buddy(find_port(args.port))
    print(f"port: {b.s.port}\n")
    b.drain(0.5)   # flush boot noise

    # ---- 1. pre-hello purity -------------------------------------------------
    print("[1] pre-hello purity (v1 byte-identity)")
    b.send({"total": 1, "running": 1, "waiting": 0})
    b.send({"time": [int(time.time()), 0]})
    quiet = b.drain(1.5)
    check("v1 heartbeat + time produce no output", len(quiet) == 0, str(quiet))
    b.send({"cmd": "prompt_cancel", "id": "req_nope"})
    quiet = b.drain(1.5)
    check("pre-hello prompt_cancel silently swallowed", len(quiet) == 0, str(quiet))
    rx = [m for m in quiet if m.get("ack") == "rx"]
    check("no rx acks pre-hello", len(rx) == 0, str(rx))
    # sanity: v1 acks still work pre-hello (this IS v1 behavior)
    b.send({"cmd": "status"})
    st = b.expect_ack("status")
    check("v1 status ack still answered", st is not None and st.get("ok") is True)

    # ---- 2. hello handshake ----------------------------------------------------
    print("[2] hello handshake")
    b.send({
        "cmd": "hello", "proto": 2,
        "host": {"id": "harness1", "name": "pytest rig", "app": "harness", "ver": "0"},
        "caps": ["sessions", "ask", "rxack", "cancel"],
    })
    ack = b.expect_ack("hello")
    check("hello ack arrives", ack is not None)
    if ack:
        d = ack.get("data", {})
        check("ok:true proto:2", ack.get("ok") is True and ack.get("proto") == 2, str(ack))
        check("data.sel present (bool)", isinstance(d.get("sel"), bool), str(d))
        check("data.maxLine sane", isinstance(d.get("maxLine"), int) and 256 <= d["maxLine"] <= 16384, str(d))
        check("data.maxSessions sane", isinstance(d.get("maxSessions"), int) and 1 <= d["maxSessions"] <= 32, str(d))
        caps = d.get("caps", [])
        check("device caps advertised", all(c in caps for c in
              ("sessions", "ask", "rxack", "cancel", "ntfy")), str(caps))
        check("fw + board reported", bool(d.get("fw")) and bool(d.get("board")), str(d))

    # ---- 3. rxack ---------------------------------------------------------------
    print("[3] rxack")
    b.send({"total": 1, "running": 1, "waiting": 0})
    acks = [m for m in b.drain(1.5) if m.get("ack") == "rx"]
    check("rx ack after post-hello line", len(acks) >= 1, str(acks))
    n1 = acks[-1].get("n", -1) if acks else -1
    b.send({"total": 1, "running": 0, "waiting": 0})
    acks = [m for m in b.drain(1.5) if m.get("ack") == "rx"]
    n2 = acks[-1].get("n", -1) if acks else -1
    check("rx counter monotonic", isinstance(n1, int) and isinstance(n2, int) and n2 > n1 >= 0,
          f"n1={n1} n2={n2}")

    if args.skip_debug:
        done(b)
        return

    # ---- 4. sessions ingest ---------------------------------------------------
    print("[4] sessions ingest (needs -DBUDDY_DEBUG build)")
    b.send({
        "total": 2, "running": 1, "waiting": 1,
        "sessions": [
            {"sid": "cli:1", "agent": "claude", "title": "fix tests",
             "state": "wait", "tok": 18420, "last": "running pytest"},
            {"sid": "cli:2", "agent": "codex", "title": "refactor",
             "state": "run", "tok": 90000, "last": ""},
        ],
    })
    b.drain(0.8)
    state, hosts = b.debug_state()
    check("debug_state answers (debug build?)", state is not None,
          "flash with -DBUDDY_DEBUG or pass --skip-debug")
    if state:
        sids = [s.get("sid") for s in state.get("sessions", [])]
        check("session table has both sids in order", sids == ["cli:1", "cli:2"], str(sids))
        sts = [s.get("st") for s in state.get("sessions", [])]
        check("states mapped (wait=2, run=1)", sts == [2, 1], str(sts))
        check("hello state tracked", state.get("hello") is True and state.get("proto") == 2, str(state))
    if hosts is not None:
        check("host registry dump present", isinstance(hosts.get("hosts"), list), str(hosts))

    # ---- 5. prompt sid/qn + prompt_cancel ---------------------------------------
    print("[5] prompt routing")
    b.send({
        "total": 1, "running": 0, "waiting": 1,
        "prompt": {"id": "req_hx1", "sid": "cli:1", "qn": 2,
                   "tool": "Bash", "hint": "make deploy"},
    })
    b.drain(0.8)
    state, _ = b.debug_state()
    if state:
        pr = state.get("prompt", {})
        check("prompt sid routed", pr.get("id") == "req_hx1" and pr.get("sid") == "cli:1", str(pr))
        check("prompt qn stored", pr.get("qn") == 2, str(pr))
    b.send({"cmd": "prompt_cancel", "id": "req_hx1"})
    ack = b.expect_ack("prompt_cancel")
    check("prompt_cancel ok:true", ack is not None and ack.get("ok") is True, str(ack))
    state, _ = b.debug_state()
    if state:
        check("prompt cleared after cancel", state.get("prompt", {}).get("id") == "", str(state))
    b.send({"cmd": "prompt_cancel", "id": "req_hx1"})
    ack = b.expect_ack("prompt_cancel")
    check("stale cancel ok:false", ack is not None and ack.get("ok") is False, str(ack))

    # ---- 6. ask -------------------------------------------------------------------
    print("[6] ask")
    b.send({
        "evt": "ask", "id": "ask_hx1", "sid": "cli:1", "multiSelect": False,
        "questions": [{"header": "Which approach?", "text": "Pick one:",
                       "options": [{"label": "Option A", "desc": "a"},
                                   {"label": "Option B", "desc": "b"}]}],
    })
    b.drain(0.8)
    state, _ = b.debug_state()
    if state:
        a = state.get("ask", {})
        check("ask stored", a.get("id") == "ask_hx1" and a.get("nOpts") == 2, str(a))
    b.send({"evt": "ask_cancel", "id": "ask_hx1"})
    b.drain(0.8)
    state, _ = b.debug_state()
    if state:
        check("ask_cancel cleared it", state.get("ask", {}).get("id") == "", str(state))

    done(b)


def done(b):
    print(f"\n{PASS} passed, {FAIL} failed")
    b.s.close()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
