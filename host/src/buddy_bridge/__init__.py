"""buddy-bridge: bridge Claude Code CLI + Codex CLI to an M5 StickS3 BLE buddy.

Keep this module import-light: hook scripts run `python -m buddy_bridge.hooks.*`
on every hook event and must not pay for bleak/watchfiles imports.
"""

__version__ = "0.2.0"
