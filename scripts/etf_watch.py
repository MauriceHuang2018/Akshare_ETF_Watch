#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin wrapper: forward to scripts/v02/etf_watch.py (default v0.2)."""

import runpy
import sys
from pathlib import Path


def main() -> None:
    """Execute v0.2 etf_watch with current argv."""
    target = Path(__file__).resolve().parent / "v02" / "etf_watch.py"
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
