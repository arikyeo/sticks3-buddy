"""User-tunable gate matchers at ``<home>/matchers.toml``.

    auto_allow = ["Bash:git status*", "Bash:git log*"]
    always_ask = ["Bash:*rm *", "Write"]
    strict     = ["Bash:git push*"]

Pattern syntax is glob-ish (fnmatch): either a bare tool name pattern
("Write", "Task*") or "Tool:command-glob" matched against the invocation's
command/detail string.

Semantics:
  auto_allow  -> instant allow, no device round-trip (audited)
  always_ask  -> always routed to the device
  strict      -> routed to the device; reserved stronger tier that an
                 auto_allow entry can never override
  (unmatched) -> "default": the daemon answers "ask" immediately and the
                 agent's native prompt handles it

Precedence: always_ask > auto_allow > strict > default.

SAFE_TOOLS are interaction/bookkeeping tools that must never be gated at
all — the hook path skips them before any matching.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

MATCHERS_FILENAME = "matchers.toml"

ALWAYS_ASK = "always_ask"
AUTO_ALLOW = "auto_allow"
STRICT = "strict"
DEFAULT = "default"

SAFE_TOOLS = frozenset(
    {
        "AskUserQuestion",
        "ExitPlanMode",
        "TodoWrite",
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
    }
)


def is_safe_tool(tool_name: str) -> bool:
    return tool_name in SAFE_TOOLS


def _match_one(pattern: str, tool_name: str, command: str) -> bool:
    pattern = pattern.strip()
    if not pattern:
        return False
    if ":" in pattern:
        tool_pat, cmd_pat = pattern.split(":", 1)
        return fnmatchcase(tool_name, tool_pat.strip()) and fnmatchcase(
            command, cmd_pat.strip()
        )
    return fnmatchcase(tool_name, pattern)


@dataclass
class Matchers:
    auto_allow: tuple[str, ...] = field(default_factory=tuple)
    always_ask: tuple[str, ...] = field(default_factory=tuple)
    strict: tuple[str, ...] = field(default_factory=tuple)

    def classify(self, tool_name: str, command: str = "") -> str:
        tool_name = str(tool_name or "")
        command = str(command or "")
        if any(_match_one(p, tool_name, command) for p in self.always_ask):
            return ALWAYS_ASK
        if any(_match_one(p, tool_name, command) for p in self.auto_allow):
            return AUTO_ALLOW
        if any(_match_one(p, tool_name, command) for p in self.strict):
            return STRICT
        return DEFAULT


def _str_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item.strip())


def load_matchers(path: Path) -> Matchers:
    """Tolerant load; a missing or garbled file yields empty matchers."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return Matchers()
    if not isinstance(data, dict):
        return Matchers()
    return Matchers(
        auto_allow=_str_list(data.get("auto_allow")),
        always_ask=_str_list(data.get("always_ask")),
        strict=_str_list(data.get("strict")),
    )
