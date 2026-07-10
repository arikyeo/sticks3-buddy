"""Weather info cards from the Open-Meteo current-weather API (keyless).

Polls every WEATHER_POLL_SECS when [cards] weather_lat/lon are configured
and emits one "weather" card on the first reading and then only on a
significant change (temperature moved >= 2°C or the weather code flipped).
The blocking urllib fetch runs in a worker thread; tests inject a fake
fetcher, so no live network is ever touched by the suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from ..cards import KIND_WEATHER, Card

log = logging.getLogger(__name__)

WEATHER_POLL_SECS = 1800.0
SIGNIFICANT_TEMP_DELTA = 2.0
_FETCH_TIMEOUT_SECS = 10.0

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat:.4f}&longitude={lon:.4f}"
    "&current=temperature_2m,weather_code"
)

# WMO weather interpretation codes -> short display text
_WMO = (
    ((0,), "clear"),
    ((1, 2), "partly cloudy"),
    ((3,), "overcast"),
    ((45, 48), "fog"),
    (tuple(range(51, 58)), "drizzle"),
    (tuple(range(61, 68)), "rain"),
    (tuple(range(71, 78)), "snow"),
    ((80, 81, 82), "showers"),
    ((85, 86), "snow showers"),
    ((95, 96, 99), "thunderstorm"),
)


def describe_wmo(code: int) -> str:
    for codes, text in _WMO:
        if code in codes:
            return text
    return f"code {code}"


def _default_fetch_sync(lat: float, lon: float) -> Optional[dict]:
    import urllib.request

    url = OPEN_METEO_URL.format(lat=lat, lon=lon)
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECS) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 — weather is best-effort
        log.debug("weather: fetch failed: %s", exc)
        return None


async def _default_fetch(lat: float, lon: float) -> Optional[dict]:
    return await asyncio.to_thread(_default_fetch_sync, lat, lon)


def parse_current(payload: object) -> Optional[tuple[float, int]]:
    """(temperature_2m, weather_code) from an Open-Meteo reply."""
    if not isinstance(payload, dict):
        return None
    current = payload.get("current")
    if not isinstance(current, dict):
        return None
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        return None
    if isinstance(code, bool) or not isinstance(code, int):
        code = -1
    return float(temp), code


class WeatherCardsPoller:
    def __init__(
        self,
        emit: Callable[[Card], None],
        *,
        lat: float,
        lon: float,
        fetch: Callable[[float, float], Awaitable[Optional[dict]]] = _default_fetch,
        poll_secs: float = WEATHER_POLL_SECS,
    ) -> None:
        self._emit = emit
        self._lat = lat
        self._lon = lon
        self._fetch = fetch
        self._poll_secs = poll_secs
        self._last: Optional[tuple[float, int]] = None

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("weather cards: poll failed")
            await asyncio.sleep(self._poll_secs)

    def _significant(self, reading: tuple[float, int]) -> bool:
        if self._last is None:
            return True  # first reading: announce once
        temp, code = reading
        last_temp, last_code = self._last
        return abs(temp - last_temp) >= SIGNIFICANT_TEMP_DELTA or code != last_code

    async def poll_once(self) -> None:
        payload = await self._fetch(self._lat, self._lon)
        reading = parse_current(payload)
        if reading is None:
            return
        if not self._significant(reading):
            return
        self._last = reading
        temp, code = reading
        self._emit(
            Card(
                kind=KIND_WEATHER,
                title="Weather",
                body=f"{temp:.0f}C {describe_wmo(code)}",
                ts=int(time.time()),
            )
        )
