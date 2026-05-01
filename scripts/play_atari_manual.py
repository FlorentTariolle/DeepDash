"""Compatibility wrapper for manual Atari play."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.manual_play import main


if __name__ == "__main__":
    main()
