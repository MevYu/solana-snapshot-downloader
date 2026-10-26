#!/usr/bin/env python3
"""Run directly from a source checkout: python3 snapshot_downloader.py --help."""

import sys

if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is required. Check: python3 --version")

from downloader.cli import main

if __name__ == "__main__":
    sys.exit(main())
