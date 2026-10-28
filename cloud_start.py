"""Cloud entrypoint: secrets come from the host's environment, never this file."""
import os
from pathlib import Path

from esports_bot.__main__ import main

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    if not os.environ.get("BOT_DB"):
        raise SystemExit("BOT_DB должен указывать на базу на постоянном диске хоста.")
    import sys
    sys.argv = [sys.argv[0], "run"]
    raise SystemExit(main())
