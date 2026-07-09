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

# no_ota.csv (the current board_build.partitions) has no ota_0/ota_1
# slots, so there's no boot_app0.bin to merge in — see the comment in
# .github/workflows/release.yml for the full explanation.
PARTS = [
    (0x0, "bootloader.bin"),
    (0x8000, "partitions.bin"),
    (0x10000, "firmware.bin"),
]


def merge(env: str, out: Path) -> None:
    build_dir = PROJECT / ".pio" / "build" / env
    if not build_dir.exists():
        sys.exit(f"{build_dir} not found — run `pio run -e {env}` first")

    missing = [name for _, name in PARTS if not (build_dir / name).exists()]
    if missing:
        sys.exit(f"missing build output(s) in {build_dir}: {', '.join(missing)}")

    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "merge_bin",
        "-o", str(out),
        "--flash_mode", "keep",
        "--flash_size", "8MB",
    ]
    for offset, name in PARTS:
        cmd += [hex(offset), str(build_dir / name)]

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
