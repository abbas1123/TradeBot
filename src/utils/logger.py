"""Structured logging via loguru: colored console + rotating file + a trades stream.

Use logger.bind(event="trade").info(...) for trade records (routed to trades.jsonl).
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False


def setup_logging(log_dir: str = "logs", level: str = "INFO"):
    global _CONFIGURED
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | {message}"
        ),
        colorize=True,
    )
    logger.add(
        f"{log_dir}/bot_{{time:YYYY-MM-DD}}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="zip",
        enqueue=True,  # safe across apscheduler worker threads
    )
    logger.add(
        f"{log_dir}/trades.jsonl",
        level="INFO",
        filter=lambda r: r["extra"].get("event") == "trade",
        serialize=True,
        rotation="10 MB",
    )
    _CONFIGURED = True
    return logger


def get_logger():
    if not _CONFIGURED:
        setup_logging()
    return logger
