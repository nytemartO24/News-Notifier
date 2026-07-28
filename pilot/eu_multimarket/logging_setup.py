"""Shared logging helper for the pilot scripts.

Mirrors the START/END-with-exit-code convention used by deploy/run.sh on
the VPS (see deploy/README.md's Monitoring section), so a quiet run is
still distinguishable from a run that never happened — logs go to both
the console and pilot/eu_multimarket/logs/<name>.log.
"""

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"


def setup_logger(name: str) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(LOG_DIR / f"{name}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
