"""Bulk WiFi import: parse a JSON/CSV/TSV/wpa_supplicant file into an
ordered list of (ssid, password) pairs for ``cli._cmd_wifi_import``.

Formats (auto-detected, in this order):
  * wpa_supplicant.conf-style ``network={ ssid="..." psk="..." }`` blocks
    (best-effort: only quoted ASCII psk is supported).
  * JSON: a list of ``{"ssid","pass"}`` objects, or a ``{ssid: pass}`` map.
  * CSV/TSV: one ``ssid<delim>pass`` pair per line (delimiter auto-detected
    from tabs vs commas), via the stdlib ``csv`` module so quoted fields
    with embedded delimiters parse correctly. Blank lines and lines whose
    first non-blank character is ``#`` are skipped.

Order is preserved (import sends are sequential and order matters once the
device is near its capacity). Callers must only print/log the ssid half of
a returned pair, or the ``warnings`` list -- both are safe to print as-is;
neither ever contains a password.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

WifiEntry = tuple[str, str]

_WPA_NETWORK_BLOCK_RE = re.compile(r"network\s*=\s*\{([^{}]*)\}", re.DOTALL)
_WPA_HAS_NETWORK_RE = re.compile(r"network\s*=\s*\{")
_WPA_HEX_PSK_RE = re.compile(r'psk\s*=\s*[0-9a-fA-F]{64}\s*(?:#.*)?$', re.MULTILINE)


class WifiImportError(Exception):
    """The import file could not be read, or its contents don't resemble
    any supported format at all (as opposed to a single bad row/entry,
    which is a skip + warning, not a hard failure)."""


def parse_wifi_import_file(path: Path) -> tuple[list[WifiEntry], list[str]]:
    """Parse ``path`` into ``(entries, warnings)``.

    ``entries`` preserves file order. ``warnings`` are human-readable,
    ssid-only strings safe to print verbatim (skipped rows, unsupported
    wpa_supplicant blocks, etc.). Raises :class:`WifiImportError` if the
    file can't be read, is empty, or its top-level shape doesn't match any
    supported format (a JSON document that's neither a list nor an
    object).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WifiImportError(f"file not found: {path}") from None
    except IsADirectoryError:
        raise WifiImportError(f"{path} is a directory, not a file") from None
    except UnicodeDecodeError:
        raise WifiImportError(f"{path} is not valid UTF-8 text") from None
    except OSError as exc:
        raise WifiImportError(f"could not read {path}: {exc}") from None

    if not text.strip():
        raise WifiImportError(f"{path} is empty")

    if _WPA_HAS_NETWORK_RE.search(text):
        return _parse_wpa_supplicant(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _parse_delimited(text)
    return _parse_json(data)


def _parse_json(data: object) -> tuple[list[WifiEntry], list[str]]:
    entries: list[WifiEntry] = []
    warnings: list[str] = []
    if isinstance(data, dict):
        for ssid, password in data.items():
            if not isinstance(ssid, str) or not ssid:
                warnings.append("skipped a JSON entry with a non-string/empty ssid")
                continue
            if not isinstance(password, str):
                warnings.append(f"skipped {ssid!r}: password is not a string")
                continue
            entries.append((ssid, password))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                warnings.append(f"skipped entry {i}: not a JSON object")
                continue
            ssid = item.get("ssid")
            password = item.get("pass", item.get("password"))
            if not isinstance(ssid, str) or not ssid:
                warnings.append(f"skipped entry {i}: missing/invalid ssid")
                continue
            if not isinstance(password, str):
                warnings.append(f"skipped {ssid!r}: missing/invalid pass")
                continue
            entries.append((ssid, password))
    else:
        raise WifiImportError(
            "JSON file must be a list of {ssid,pass} objects, or an object mapping ssid -> password"
        )
    return entries, warnings


def _parse_delimited(text: str) -> tuple[list[WifiEntry], list[str]]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(raw_line)
    if not lines:
        return [], []

    delimiter = "\t" if any("\t" in line for line in lines) else ","
    entries: list[WifiEntry] = []
    warnings: list[str] = []
    for i, row in enumerate(csv.reader(lines, delimiter=delimiter)):
        if len(row) < 2:
            warnings.append(f"skipped malformed line {i + 1}: expected ssid{delimiter!r}pass")
            continue
        ssid, password = row[0].strip(), row[1].strip()
        if not ssid:
            warnings.append(f"skipped line {i + 1}: empty ssid")
            continue
        entries.append((ssid, password))
    return entries, warnings


def _parse_wpa_supplicant(text: str) -> tuple[list[WifiEntry], list[str]]:
    entries: list[WifiEntry] = []
    warnings: list[str] = []
    for block in _WPA_NETWORK_BLOCK_RE.finditer(text):
        body = block.group(1)
        ssid = _wpa_field(body, "ssid")
        if ssid is None:
            warnings.append("skipped a wpa_supplicant network{} block: no quoted ssid")
            continue
        psk = _wpa_field(body, "psk")
        if psk is None:
            if _WPA_HEX_PSK_RE.search(body):
                warnings.append(
                    f"skipped {ssid!r}: raw-hex psk not supported (need a quoted passphrase)"
                )
            else:
                warnings.append(f"skipped {ssid!r}: no quoted psk found (open/eap network?)")
            continue
        entries.append((ssid, psk))
    return entries, warnings


def _wpa_field(body: str, name: str) -> str | None:
    """Extract a quoted ``name="value"`` field, unescaping \\" and \\\\."""
    m = re.search(rf'{name}\s*=\s*"((?:[^"\\]|\\.)*)"', body)
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
