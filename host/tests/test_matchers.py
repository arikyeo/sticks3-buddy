from buddy_bridge.matchers import (
    ALWAYS_ASK,
    AUTO_ALLOW,
    DEFAULT,
    SAFE_TOOLS,
    STRICT,
    Matchers,
    is_safe_tool,
    load_matchers,
)


def test_precedence_always_ask_over_auto_allow():
    m = Matchers(
        auto_allow=("Bash:git *",),
        always_ask=("Bash:git push*",),
        strict=("Bash:git commit*",),
    )
    assert m.classify("Bash", "git status") == AUTO_ALLOW
    assert m.classify("Bash", "git push origin main") == ALWAYS_ASK  # beats auto_allow
    assert m.classify("Bash", "git commit -m x") == AUTO_ALLOW  # auto_allow beats strict
    assert m.classify("Bash", "rm -rf /") == DEFAULT


def test_strict_matches_when_not_auto_allowed():
    m = Matchers(strict=("Bash:git push*",))
    assert m.classify("Bash", "git push --force") == STRICT
    assert m.classify("Bash", "git pull") == DEFAULT


def test_bare_tool_patterns_and_glob_tools():
    m = Matchers(auto_allow=("Read",), always_ask=("mcp__*",))
    assert m.classify("Read", "anything") == AUTO_ALLOW
    assert m.classify("mcp__github__push", "") == ALWAYS_ASK
    assert m.classify("Write", "x") == DEFAULT


def test_command_glob_matching():
    m = Matchers(always_ask=("Bash:*rm *",))
    # '*' matches zero chars, so '*rm *' catches both bare and prefixed rm
    assert m.classify("Bash", "rm -rf build") == ALWAYS_ASK
    assert m.classify("Bash", "sudo rm -rf /") == ALWAYS_ASK
    assert m.classify("Bash", "grep -rn pattern") == DEFAULT  # no ' ' after 'rm'-ish hit
    m2 = Matchers(always_ask=("Bash:rm *",))
    assert m2.classify("Bash", "rm -rf build") == ALWAYS_ASK
    assert m2.classify("Bash", "sudo rm -rf /") == DEFAULT  # anchored at the start


def test_safe_tools_hard_skip():
    assert SAFE_TOOLS == {
        "AskUserQuestion",
        "ExitPlanMode",
        "TodoWrite",
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
    }
    for tool in SAFE_TOOLS:
        assert is_safe_tool(tool)
    assert not is_safe_tool("Bash")


def test_load_matchers_tolerant(tmp_path):
    path = tmp_path / "matchers.toml"
    assert load_matchers(path).classify("Bash", "ls") == DEFAULT  # missing file

    path.write_text(
        """
auto_allow = ["Bash:git status*", 42]
always_ask = ["Write"]
strict = "not-a-list"
unknown_key = ["x"]
""",
        encoding="utf-8",
    )
    m = load_matchers(path)
    assert m.auto_allow == ("Bash:git status*",)  # non-strings dropped
    assert m.always_ask == ("Write",)
    assert m.strict == ()

    path.write_text("not [valid toml", encoding="utf-8")
    assert load_matchers(path).classify("Write", "") == DEFAULT
