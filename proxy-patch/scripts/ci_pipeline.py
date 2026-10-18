#!/usr/bin/env python3
"""Give the frozen formatter a terminal input without recording a terminal session."""
from pathlib import Path
import os
import pty
import subprocess
import sys


def main():
    master, slave = pty.openpty()
    try:
        # The pinned gci interprets piped CI stdin as Go source, even when
        # explicit file paths are supplied. Output still uses normal CI pipes.
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("pipeline.py")), *sys.argv[1:]],
            stdin=slave,
        )
        return result.returncode
    finally:
        os.close(slave)
        os.close(master)


if __name__ == "__main__":
    sys.exit(main())
