import json

from buddy_bridge.adapters.claude_jsonl import JsonlTailer, parse_transcript_line


def _assistant_line(text, tokens, sid="sess-1", ts="2026-07-10T03:04:05.000Z"):
    return (
        json.dumps(
            {
                "type": "assistant",
                "sessionId": sid,
                "cwd": "C:\\proj\\demo",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": tokens},
                },
            }
        )
        + "\n"
    )


def _user_line():
    return json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"


def _mk_tailer(tmp_path, events):
    return JsonlTailer(
        root=tmp_path / "projects",
        offsets_path=tmp_path / "home" / "offsets.json",
        on_event=events.append,
    )


def _setup_root(tmp_path):
    proj = tmp_path / "projects" / "C--proj-demo"
    proj.mkdir(parents=True)
    f = proj / "sess-1.jsonl"
    f.write_text(
        _assistant_line("old reply 1", 11) + _user_line() + _assistant_line("old reply 2", 22),
        encoding="utf-8",
    )
    return f


def test_parse_transcript_line():
    event = parse_transcript_line(_assistant_line("hello world", 42).encode())
    assert event.session_id == "sess-1"
    assert event.text == "hello world"
    assert event.output_tokens == 42
    assert event.cwd == "C:\\proj\\demo"
    assert len(event.hhmm) == 5 and event.hhmm[2] == ":"
    assert parse_transcript_line(_user_line().encode()) is None
    assert parse_transcript_line(b"not json") is None
    assert parse_transcript_line(b"") is None
    assert parse_transcript_line(b'{"type":"assistant","message":{}}') is None


def test_initial_sweep_suppresses_existing_content(tmp_path):
    f = _setup_root(tmp_path)
    events = []
    tailer = _mk_tailer(tmp_path, events)
    tailer.load_offsets()
    tailer.initial_sweep()
    assert tailer.poll_file(f) == []  # pre-existing history is baseline, no replay

    # new content after the sweep IS picked up
    with f.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line("fresh reply", 33))
    got = tailer.poll_file(f)
    assert [e.text for e in got] == ["fresh reply"]
    assert got[0].output_tokens == 33


def test_offsets_persist_across_restart_no_replay(tmp_path):
    f = _setup_root(tmp_path)
    events1 = []
    t1 = _mk_tailer(tmp_path, events1)
    t1.load_offsets()
    t1.initial_sweep()
    with f.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line("counted once", 5))
    assert len(t1.poll_file(f)) == 1
    t1.save_offsets()

    # daemon restart: same offsets file, fresh tailer
    t2 = _mk_tailer(tmp_path, [])
    t2.load_offsets()
    t2.initial_sweep()
    assert t2.poll_file(f) == []  # nothing replayed
    with f.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line("new after restart", 7))
    got = t2.poll_file(f)
    assert [e.text for e in got] == ["new after restart"]


def test_new_file_after_sweep_read_from_start(tmp_path):
    _setup_root(tmp_path)
    tailer = _mk_tailer(tmp_path, [])
    tailer.load_offsets()
    tailer.initial_sweep()
    new = tmp_path / "projects" / "C--proj-demo" / "sess-2.jsonl"
    new.write_text(
        _assistant_line("first in new session", 9, sid="sess-2"), encoding="utf-8"
    )
    got = tailer.poll_file(new)
    assert [e.text for e in got] == ["first in new session"]
    assert got[0].session_id == "sess-2"


def test_partial_line_not_consumed(tmp_path):
    f = _setup_root(tmp_path)
    tailer = _mk_tailer(tmp_path, [])
    tailer.load_offsets()
    tailer.initial_sweep()
    full = _assistant_line("split write", 3)
    head, tail = full[:40], full[40:]
    # newline="" -> no \n -> \r\n translation, keeping the byte math exact
    with f.open("a", encoding="utf-8", newline="") as fh:
        fh.write(head)  # no newline yet
    assert tailer.poll_file(f) == []
    offset_before = tailer.offsets[str(f)]
    with f.open("a", encoding="utf-8", newline="") as fh:
        fh.write(tail)
    got = tailer.poll_file(f)
    assert [e.text for e in got] == ["split write"]
    assert tailer.offsets[str(f)] == offset_before + len(full.encode())


def test_truncated_file_reread_from_zero(tmp_path):
    f = _setup_root(tmp_path)
    tailer = _mk_tailer(tmp_path, [])
    tailer.load_offsets()
    tailer.initial_sweep()
    f.write_text(_assistant_line("rewritten", 4), encoding="utf-8")  # shrinks the file
    got = tailer.poll_file(f)
    assert [e.text for e in got] == ["rewritten"]


def test_garbage_lines_skipped(tmp_path):
    f = _setup_root(tmp_path)
    tailer = _mk_tailer(tmp_path, [])
    tailer.load_offsets()
    tailer.initial_sweep()
    with f.open("a", encoding="utf-8") as fh:
        fh.write("!!! not json !!!\n")
        fh.write('{"type":"weird_new_record"}\n')
        fh.write(_assistant_line("still parsed", 8))
    got = tailer.poll_file(f)
    assert [e.text for e in got] == ["still parsed"]
