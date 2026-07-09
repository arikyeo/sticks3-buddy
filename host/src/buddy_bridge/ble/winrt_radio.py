# Portions vendored from SnowWarri0r/cc-buddy-bridge@5c5b6142c54488491d4a039be23ccc69dcf00146
# (MIT) — radio power-cycle recovery for the WinRT advertisement watcher.
"""Windows Bluetooth radio power-cycle recovery.

The WinRT BluetoothLEAdvertisementWatcher can silently stop delivering
callbacks while still reporting status=Started. After a sustained run of
consecutive scan misses we toggle the radio off->on to recover without user
intervention. Power-cycling briefly drops ALL of the user's Bluetooth
devices, so it is rationed: fires only after AFTER_MISSES consecutive misses,
at most MAX_ATTEMPTS per disconnected spell, with COOLDOWN_SECS between
attempts. A successful connect resets the budget.

winrt is imported lazily; on non-Windows platforms (or when the win32 extra
is not installed) everything is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

log = logging.getLogger(__name__)

AFTER_MISSES = 5
MAX_ATTEMPTS = 3
COOLDOWN_SECS = 120.0


class RadioCycler:
    """Tracks scan-miss state and rations radio power-cycles (win32 only)."""

    def __init__(
        self,
        after_misses: int = AFTER_MISSES,
        max_attempts: int = MAX_ATTEMPTS,
        cooldown_secs: float = COOLDOWN_SECS,
    ) -> None:
        self.after_misses = after_misses
        self.max_attempts = max_attempts
        self.cooldown_secs = cooldown_secs
        self._attempts = 0
        self._misses_at_last_cycle = 0
        self._last_cycle_ts = 0.0

    def reset(self) -> None:
        """Call on a successful find/connect: next spell gets a fresh budget."""
        self._attempts = 0
        self._misses_at_last_cycle = 0
        self._last_cycle_ts = 0.0

    def eligible(self, consecutive_misses: int) -> bool:
        if sys.platform != "win32":
            return False
        if consecutive_misses - self._misses_at_last_cycle < self.after_misses:
            return False
        if self._attempts >= self.max_attempts:
            return False
        now = time.monotonic()
        if self._last_cycle_ts and now - self._last_cycle_ts < self.cooldown_secs:
            return False
        return True

    async def maybe_cycle(self, consecutive_misses: int) -> bool:
        """Power-cycle the radio when eligible. True only if a cycle happened."""
        if not self.eligible(consecutive_misses):
            return False
        fired = await power_cycle_radio()
        if fired:
            self._attempts += 1
            self._misses_at_last_cycle = consecutive_misses
            self._last_cycle_ts = time.monotonic()
            if self._attempts >= self.max_attempts:
                log.warning(
                    "radio reset: %d attempts without recovery — giving up auto-reset; "
                    "a manual Bluetooth toggle may be needed",
                    self._attempts,
                )
        return fired


async def power_cycle_radio() -> bool:
    """Toggle the Bluetooth radio off->on. True only when actually cycled.

    Returns False on non-Windows, missing winrt, no adapter, radio already
    off (respect the user's choice), access denied, or any error.
    """
    if sys.platform != "win32":
        return False
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.radios import RadioAccessStatus, RadioState
    except ImportError:
        log.info("radio reset: winrt not available, skipping")
        return False
    try:
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            log.warning("radio reset: no Bluetooth adapter found, skipping")
            return False
        radio = await adapter.get_radio_async()
        if radio.state != RadioState.ON:
            log.info("radio reset: radio is %s — leaving it to the user", radio.state)
            return False
        log.warning(
            "radio reset: power-cycling the Bluetooth radio to recover the scanner. "
            "This briefly disconnects ALL Bluetooth devices, not just the buddy."
        )
        status = await radio.set_state_async(RadioState.OFF)
        if status != RadioAccessStatus.ALLOWED:
            log.warning("radio reset: could not turn radio off (status=%s)", status)
            return False
        await asyncio.sleep(2.0)
        status = await radio.set_state_async(RadioState.ON)
        if status != RadioAccessStatus.ALLOWED:
            log.warning("radio reset: could not turn radio back on (status=%s)", status)
            return False
        await asyncio.sleep(2.0)
        log.info("radio reset: Bluetooth radio cycled successfully")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("radio reset failed: %s", exc)
        return False
