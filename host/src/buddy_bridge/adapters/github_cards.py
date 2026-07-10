"""GitHub info cards via the ``gh`` CLI (no tokens handled here).

Every GH_POLL_SECS the poller runs ``gh api notifications`` and turns
unread notification threads into "gh" cards; for each configured repo it
also checks the latest completed workflow run (``gh run list``) and emits
a "ci" card when the conclusion changes.

Degrades gracefully: a missing ``gh`` binary disables the poller for the
daemon's lifetime (one log line); a failing/unauthenticated ``gh`` just
skips the cycle (gh prints its own auth hint on stderr, which we log at
debug). No direct network I/O — everything goes through the ``gh``
subprocess, and tests inject a fake runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional, Sequence

from ..cards import KIND_CI, KIND_GITHUB, Card

log = logging.getLogger(__name__)

GH_POLL_SECS = 300.0
NOTIFICATION_LIMIT = 8  # newest unread threads per sweep; keep it small

# (rc, stdout) or None when the binary is missing entirely
GhRunner = Callable[[Sequence[str]], Awaitable[Optional[tuple[int, str]]]]


async def _default_run_gh(args: Sequence[str]) -> Optional[tuple[int, str]]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:  # unlaunchable for any other reason: treat as missing
        log.debug("gh: launch failed: %s", exc)
        return None
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0 and stderr:
        log.debug("gh %s: rc=%s %s", args[0], proc.returncode, stderr.decode()[:200])
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


def _iso_to_epoch(value: object) -> int:
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return 0


def notification_cards(payload: object, limit: int = NOTIFICATION_LIMIT) -> list[Card]:
    """Unread notification threads -> "gh" cards (pure, for tests)."""
    if not isinstance(payload, list):
        return []
    cards: list[Card] = []
    for thread in payload:
        if not isinstance(thread, dict) or thread.get("unread") is False:
            continue
        subject = thread.get("subject") if isinstance(thread.get("subject"), dict) else {}
        repo = thread.get("repository") if isinstance(thread.get("repository"), dict) else {}
        title = str(repo.get("name") or repo.get("full_name") or "github")
        body = str(subject.get("title") or subject.get("type") or "notification")
        cards.append(
            Card(
                kind=KIND_GITHUB,
                title=title,
                body=body,
                ts=_iso_to_epoch(thread.get("updated_at")),
            )
        )
        if len(cards) >= limit:
            break
    return cards


def latest_run(payload: object) -> Optional[dict]:
    """First completed run from a ``gh run list --json`` payload."""
    if not isinstance(payload, list):
        return None
    for run in payload:
        if isinstance(run, dict) and run.get("status") == "completed":
            return run
    return None


class GithubCardsPoller:
    def __init__(
        self,
        emit: Callable[[Card], None],
        *,
        repos: Sequence[str] = (),
        run_gh: GhRunner = _default_run_gh,
        poll_secs: float = GH_POLL_SECS,
    ) -> None:
        self._emit = emit
        self._repos = tuple(repos)
        self._run_gh = run_gh
        self._poll_secs = poll_secs
        self._disabled = False  # set permanently when gh is missing
        self._last_ci: dict[str, str] = {}  # repo -> last seen conclusion key

    @property
    def disabled(self) -> bool:
        return self._disabled

    async def run(self) -> None:
        while not self._disabled:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 — a bad cycle never kills the loop
                log.exception("gh cards: poll failed")
            await asyncio.sleep(self._poll_secs)

    async def _gh_json(self, args: Sequence[str]) -> object:
        """Run gh, parse stdout as JSON. None on any failure; flips the
        poller off for good when the binary itself is missing."""
        result = await self._run_gh(args)
        if result is None:
            if not self._disabled:
                log.info("gh cards: gh CLI not found; github cards disabled")
            self._disabled = True
            return None
        rc, stdout = result
        if rc != 0:
            return None  # unauth/network/rate-limit: silently skip this cycle
        try:
            return json.loads(stdout)
        except ValueError:
            return None

    async def poll_once(self) -> None:
        payload = await self._gh_json(["api", "notifications"])
        for card in notification_cards(payload):
            self._emit(card)
        if self._disabled:
            return
        for repo in self._repos:
            await self._poll_ci(repo)

    async def _poll_ci(self, repo: str) -> None:
        payload = await self._gh_json(
            [
                "run", "list", "--repo", repo, "--limit", "5",
                "--json", "status,conclusion,displayTitle,workflowName,updatedAt",
            ]
        )
        run = latest_run(payload)
        if run is None:
            return
        conclusion = str(run.get("conclusion") or "unknown")
        key = f"{conclusion}:{run.get('displayTitle') or ''}"
        if self._last_ci.get(repo) == key:
            return
        self._last_ci[repo] = key
        short_repo = repo.rsplit("/", 1)[-1]
        self._emit(
            Card(
                kind=KIND_CI,
                title=short_repo,
                body=f"{conclusion}: {run.get('displayTitle') or run.get('workflowName') or ''}",
                ts=_iso_to_epoch(run.get("updatedAt")),
            )
        )
