"""Info cards: ntfy notification composer, dedupe, and a small local log.

The device side is the optional ``ntfy`` capability from PROTOCOL_V2.md:

    {"evt":"ntfy","kind":"gh","title":"PR #42 merged","body":"...","ts":...}

``kind`` is a short tag ("gh", "ci", "weather", ...) the device may map to
an icon; unknown kinds must be treated as generic, so this module doesn't
enforce a closed set. Titles are capped at 24 bytes and bodies at 80, both
glyph-sanitized like all other display text.

Cards are deduped by a (kind, title, body) content hash with a TTL, so
pollers may re-emit the same observation every cycle without spamming the
device. Sent cards are appended to ``<home>/cards.log`` (tiny, rotates to
``cards.log.1`` once) — deliberately separate from the decision audit
trail, which stays reserved for permission outcomes.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .protocol import LINE_BUDGET, dumps, sanitize, truncate_utf8_bytes

CARD_TITLE_MAX = 24
CARD_BODY_MAX = 80
DEDUPE_TTL_SECS = 6 * 3600.0
CARDS_LOG_FILENAME = "cards.log"
CARDS_LOG_MAX_BYTES = 64 * 1024
CARD_QUEUE_MAX = 16

KIND_GITHUB = "gh"
KIND_CI = "ci"
KIND_WEATHER = "weather"
KIND_UPDATE = "update"


@dataclass(frozen=True)
class Card:
    kind: str
    title: str
    body: str = ""
    ts: int = 0

    def key(self) -> str:
        raw = "\x1f".join((self.kind, self.title, self.body))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def clean_card(card: Card) -> Card:
    """Sanitize + cap a card's display fields (ts filled in when 0)."""
    return Card(
        kind=truncate_utf8_bytes(sanitize(card.kind), 12) or "info",
        title=truncate_utf8_bytes(sanitize(card.title), CARD_TITLE_MAX),
        body=truncate_utf8_bytes(sanitize(card.body), CARD_BODY_MAX),
        ts=int(card.ts) if card.ts else int(time.time()),
    )


def build_ntfy_line(card: Card, budget: int = LINE_BUDGET) -> str:
    """Compose the ntfy event line, budget-aware (body shrinks first, then
    title — the fixed fields alone always fit any sane budget)."""
    cleaned = clean_card(card)
    title, body = cleaned.title, cleaned.body

    def compose() -> str:
        return dumps(
            {"evt": "ntfy", "kind": cleaned.kind, "title": title, "body": body,
             "ts": cleaned.ts}
        )

    line = compose()
    for step in (40, 16, 0):
        if len(line.encode("utf-8")) <= budget:
            break
        body = truncate_utf8_bytes(body, step)
        line = compose()
    if len(line.encode("utf-8")) > budget:
        title = truncate_utf8_bytes(title, 8)
        line = compose()
    return line


class CardDeduper:
    """Suppress repeats of the same (kind, title, body) within a TTL."""

    def __init__(
        self,
        ttl: float = DEDUPE_TTL_SECS,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._now = now_fn
        self._seen: dict[str, float] = {}

    def accept(self, card: Card) -> bool:
        now = self._now()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._ttl}
        key = clean_card(card).key()  # dedupe on what the device would see
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


@dataclass
class CardLog:
    """Append-only local log of emitted cards with one-deep rotation."""

    path: Path
    max_bytes: int = CARDS_LOG_MAX_BYTES

    def append(self, card: Card) -> None:
        cleaned = clean_card(card)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cleaned.ts))
        line = f"{stamp} [{cleaned.kind}] {cleaned.title} | {cleaned.body}\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # the log must never break card delivery


@dataclass
class CardPipeline:
    """Bounded queue + dedupe + log; the daemon drains it to the device
    whenever a v2 link with the ntfy capability is up."""

    deduper: CardDeduper
    log: CardLog
    queue: list[Card] = field(default_factory=list)

    def submit(self, card: Card, *, force: bool = False) -> bool:
        """Adapter-facing entry: dedupe, log, and queue. False if suppressed.

        ``force`` skips the content deduper (still logged + queued) — for
        callers that run their own dedupe/rate-limit policy, like the LAN
        relay's remote cards where the 6h content TTL would wrongly swallow
        a legitimately recurring prompt.
        """
        if not force and not self.deduper.accept(card):
            return False
        cleaned = clean_card(card)
        self.log.append(cleaned)
        self.queue.append(cleaned)
        del self.queue[:-CARD_QUEUE_MAX]  # bounded: oldest cards fall off
        return True

    def pop(self) -> Card | None:
        return self.queue.pop(0) if self.queue else None
