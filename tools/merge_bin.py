#!/usr/bin/env python3
"""
Build a single flashable image the same way .github/workflows/release.yml
does, for on-box testing before pushing a tag.

Usage:
  python3 tools/merge_bin.py --env m5stick-s3 --out buddy-sticks3-merged.bin

Requires the env to already be built (pio run -e <env>) and esptool
installed (pip install esptool).
"""
import argparse
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# partitions/sticks3_8mb_ota.csv (the current board_build.partitions) has
# ota_0/ota_1 slots, so the merged image must include boot_app0.bin (the
# OTA-slot selector stub) at the otadata-adjacent 0xE000 offset. It ships
# with the Arduino framework package, not the per-env build dir.
PARTS = [
    (0x0, "bootloader.bin"),
    (0x8000, "partitions.bin"),
    (0x10000, "firmware.bin"),
]
BOOT_APP0_OFFSET = 0xE000


def find_boot_app0() -> Path:
    core = Path.home() / ".platformio"
    hits = sorted(core.glob("packages/framework-arduinoespressif32*/tools/partitions/boot_app0.bin"))
    if not hits:
        sys.exit(f"boot_app0.bin not found under {core}/packages — run a build first")
    return hits[0]


def merge(env: str, out: Path) -> None:
    build_dir = PROJECT / ".pio" / "build" / env
    if not build_dir.exists():
        sys.exit(f"{build_dir} not found — run `pio run -e {env}` first")

    missing = [name for _, name in PARTS if not (build_dir / name).exists()]
    if missing:
        sys.exit(f"missing build output(s) in {build_dir}: {', '.join(missing)}")

    boot_app0 = find_boot_app0()

    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "merge_bin",
        "-o", str(out),
        "--flash_mode", "keep",
        "--flash_size", "8MB",
    ]
    images = sorted(
        [(offset, build_dir / name) for offset, name in PARTS]
        + [(BOOT_APP0_OFFSET, boot_app0)]
    )
    for offset, path in images:
        cmd += [hex(offset), str(path)]

    print(f"merging {env} -> {out}")
    out = out.resolve()
    cmd[cmd.index("-o") + 1] = str(out)
    subprocess.run(cmd, cwd=PROJECT, check=True)
    print(f"\nwrote {out} ({out.stat().st_size:,} bytes)")
    print("flash with: esptool.py --chip esp32s3 write_flash 0x0", out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env", default="m5stick-s3", help="PlatformIO env to merge (default: m5stick-s3)"
    )
    parser.add_argument(
        "--out", default="buddy-sticks3-merged.bin", help="output file path"
    )
    args = parser.parse_args()
    merge(args.env, Path(args.out))
