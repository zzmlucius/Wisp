#!/usr/bin/env python3
"""Run the upper four Finegraintest RPS levels with round progress."""
from pathlib import Path

from Finegraintest import main


if __name__ == '__main__':
    raise SystemExit(main(rps_values=(1250, 1500, 1750, 2000),
                          entrypoint=Path(__file__), round_progress=True))
