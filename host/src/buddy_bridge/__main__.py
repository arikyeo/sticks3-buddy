"""``python -m buddy_bridge`` — same entry point as the ``buddy-bridge`` script.

Exists so environments without the console script on PATH (and the service
installer's ``pythonw.exe -m buddy_bridge daemon`` launchers) can run the CLI.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
