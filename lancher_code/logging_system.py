from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from lancher_code.config_system.paths import get_error_log_path

LOGGER_NAME = "lancher_code"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
_HANDLER_MARKER = "_lancher_error_handler"
_sensitive_values: set[str] = set()
_sensitive_lock = threading.Lock()

_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+(?:\s+[^\s,;]+)?)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(?i)((?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*)([^\s,;]+)"),
)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log_text(super().format(record))


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def register_sensitive_values(values: Iterable[str | None]) -> None:
    with _sensitive_lock:
        for value in values:
            if isinstance(value, str) and len(value) >= 4:
                _sensitive_values.add(value)


def sanitize_log_text(text: object) -> str:
    sanitized = str(text)
    with _sensitive_lock:
        values = sorted(_sensitive_values, key=len, reverse=True)
    for value in values:
        sanitized = sanitized.replace(value, "[REDACTED]")
    for pattern in _CREDENTIAL_PATTERNS:
        sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def configure_logging(
    *,
    log_path: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path | None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    close_logging()
    formatter = RedactingFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    target = log_path or get_error_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            target,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        result: Path | None = target
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
        result = None
    setattr(handler, _HANDLER_MARKER, True)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if result is None:
        logger.error("event=logging_initialization_failed fallback=stderr")
    return result


def close_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        try:
            handler.flush()
        finally:
            handler.close()
            logger.removeHandler(handler)
