"""Package entry point, also usable by launchd without PYTHONPATH."""
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from slotfather.cli import main

if __name__ == "__main__":
    sys.exit(main())
