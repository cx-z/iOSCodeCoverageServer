"""Structured append-only server logging."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def write_log(
    logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object
) -> None:
    """Serializes one UTC timestamped JSON event through thread-safe logging handlers."""

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": logging.getLevelName(level),
        "event": event,
    }
    record.update(fields)
    logger.log(level, json.dumps(record, ensure_ascii=False, separators=(",", ":")))

def configure_logging(log_file: Path) -> logging.Logger:
    """Creates console and append-only file handlers for the foreground server."""

    resolved_log_file = log_file.resolve()
    resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ios_coverage_server")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing_handler in logger.handlers[:]:
        logger.removeHandler(existing_handler)
        existing_handler.close()

    formatter = logging.Formatter("%(message)s")
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(resolved_log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger
