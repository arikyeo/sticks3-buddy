"""Firmware update-check card via the keyless GitHub REST API.

Every UPDATE_POLL_SECS the poller fetches
``https://api.github.com/repos/<repo>/releases/latest`` (urllib, no ``gh``
dependency, no token) and compares the release tag against the connected
device's advertised firmware version (the ``hello`` ack's ``data.fw``,
exposed via ``Daemon.device_fw_version``). A strictly newer tag emits one
"update" ntfy card; the same tag is never carded twice.

Degrades quietly: an unknown device fw (nothing paired, or a v1-only
stick that never completed the v2 handshake) skips the cycle before ever
touching the network; a fetch failure, a malformed payload, or a
non-semver-ish tag are all treated as "can't tell" rather than an error —
same best-effort posture as the weather poller. No direct network I/O in
tests — the fetch is injected, and the default urllib path is exercised
with ``urlopen`` mocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Awaitable, Callable, Optional

from ..cards import KIND_UPDATE, Card

log = logging.getLogger(__name__)

UPDATE_POLL_SECS = 6 * 3600.0
DEFAULT_FIRMWARE_REPO = "arikyeo/sticks3-buddy"
_FETCH_TIMEOUT_SECS = 10.0
_LEADING_DIGITS = re.compile(r"\d+")


def _releases_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _default_fetch_sync(repo: str) -> Optional[dict]:
    import urllib.request

    req = urllib.request.Request(
        _releases_url(repo),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "buddy-bridge"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECS) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 — update-check is best-effort
        log.debug("update cards: fetch failed: %s", exc)
        return None


async def _default_fetch(repo: str) -> Optional[dict]:
    return await asyncio.to_thread(_default_fetch_sync, repo)


def parse_version(tag: object) -> Optional[tuple[int, ...]]:
    """Best-effort dotted-numeric version, tolerant of a leading 'v' and a
    build/prerelease suffix on any component ('v2.1.3-rc1' -> (2, 1, 3)).

    None when the tag doesn't start with a recognizable number — garbage
    in means "can't compare", never a crash and never a false "newer".
    """
    if not isinstance(tag, str):
        return None
    s = tag.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    out: list[int] = []
    for part in s.split("."):
        m = _LEADING_DIGITS.match(part)
        if m is None:
            break
        out.append(int(m.group()))
    return tuple(out) if out else None


def is_newer(remote_tag: str, current: str) -> bool:
    """True only when both ``remote_tag`` and ``current`` parse and the
    remote version is strictly greater, compared component-wise with
    missing trailing components padded to 0. Any parse failure (a garbage
    release tag, an unknown/garbage device fw string) returns False —
    "can't tell" never triggers a spurious update card."""
    remote = parse_version(remote_tag)
    ours = parse_version(current)
    if remote is None or ours is None:
        return False
    length = max(len(remote), len(ours))
    remote_padded = remote + (0,) * (length - len(remote))
    ours_padded = ours + (0,) * (length - len(ours))
    return remote_padded > ours_padded


def latest_tag(payload: object) -> str:
    """``tag_name`` from a GitHub 'releases/latest' payload; "" if absent,
    the wrong type, or the payload isn't the expected shape at all (e.g. a
    rate-limit error body)."""
    if not isinstance(payload, dict):
        return ""
    tag = payload.get("tag_name")
    return tag if isinstance(tag, str) else ""


def _strip_v(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") else tag


class UpdateCardsPoller:
    def __init__(
        self,
        emit: Callable[[Card], None],
        *,
        get_device_fw: Callable[[], str],
        repo: str = DEFAULT_FIRMWARE_REPO,
        fetch: Callable[[str], Awaitable[Optional[dict]]] = _default_fetch,
        poll_secs: float = UPDATE_POLL_SECS,
    ) -> None:
        self._emit = emit
        self._get_device_fw = get_device_fw
        self._repo = repo
        self._fetch = fetch
        self._poll_secs = poll_secs
        self._last_carded_tag: Optional[str] = None

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("update cards: poll failed")
            await asyncio.sleep(self._poll_secs)

    async def poll_once(self) -> None:
        device_fw = self._get_device_fw()
        if not device_fw:
            return  # nothing paired / fw unknown: nothing to compare against
        payload = await self._fetch(self._repo)
        tag = latest_tag(payload)
        if not tag or tag == self._last_carded_tag:
            return
        if not is_newer(tag, device_fw):
            return
        self._last_carded_tag = tag
        self._emit(
            Card(
                kind=KIND_UPDATE,
                title=f"fw {_strip_v(tag)} available",
                body="menu > update to install",
                ts=int(time.time()),
            )
        )
