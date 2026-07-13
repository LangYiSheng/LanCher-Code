from __future__ import annotations

import logging
from pathlib import Path

import pytest

import lancher_code.logging_system as logging_system
from lancher_code.config_system.paths import get_error_log_path, get_global_logs_dir


@pytest.fixture(autouse=True)
def reset_logging() -> None:
    logging_system.close_logging()
    logging_system._sensitive_values.clear()
    yield
    logging_system.close_logging()
    logging_system._sensitive_values.clear()


def test_log_paths_are_under_user_lancher_directory(tmp_path: Path) -> None:
    assert get_global_logs_dir(tmp_path) == tmp_path / ".lancher" / "logs"
    assert get_error_log_path(tmp_path) == tmp_path / ".lancher" / "logs" / "lancher-error.log"


def test_configure_creates_file_and_does_not_stack_handlers(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "error.log"
    assert logging_system.configure_logging(log_path=path) == path
    logging_system.configure_logging(log_path=path)
    logger = logging.getLogger(logging_system.LOGGER_NAME)
    assert len([h for h in logger.handlers if getattr(h, logging_system._HANDLER_MARKER, False)]) == 1
    logging_system.get_logger("test").error("event=test_error")
    logging_system.close_logging()
    assert "event=test_error" in path.read_text(encoding="utf-8")


def test_rotates_and_limits_backup_count(tmp_path: Path) -> None:
    path = tmp_path / "error.log"
    logging_system.configure_logging(log_path=path, max_bytes=120, backup_count=2)
    logger = logging_system.get_logger("rotation")
    for index in range(30):
        logger.error("event=rotation index=%d padding=%s", index, "x" * 80)
    logging_system.close_logging()
    assert path.exists()
    assert path.with_name("error.log.1").exists()
    assert path.with_name("error.log.2").exists()
    assert not path.with_name("error.log.3").exists()


def test_redacts_registered_and_pattern_credentials_in_message_and_traceback(tmp_path: Path) -> None:
    path = tmp_path / "error.log"
    secret = "super-secret-value"
    logging_system.configure_logging(log_path=path, max_bytes=300, backup_count=2)
    logging_system.register_sensitive_values([secret])
    logger = logging_system.get_logger("redaction")
    try:
        raise RuntimeError(f"Authorization: Bearer token-123 api_key=key-456 known={secret}")
    except RuntimeError:
        logger.exception("event=secret_failure token=plain-token password=hunter2 value=%s", secret)
    for index in range(12):
        logger.error("event=padding value=%s index=%d %s", secret, index, "x" * 80)
    logging_system.close_logging()
    combined = "".join(file.read_text(encoding="utf-8") for file in tmp_path.glob("error.log*"))
    assert secret not in combined
    assert "token-123" not in combined
    assert "key-456" not in combined
    assert "plain-token" not in combined
    assert "hunter2" not in combined
    assert "[REDACTED]" in combined


def test_unwritable_log_path_falls_back_to_stderr(monkeypatch, tmp_path: Path, capsys) -> None:
    class BrokenHandler:
        def __init__(self, *args, **kwargs) -> None:
            raise OSError("cannot write")

    monkeypatch.setattr(logging_system, "RotatingFileHandler", BrokenHandler)
    assert logging_system.configure_logging(log_path=tmp_path / "error.log") is None
    logging_system.get_logger("fallback").error("event=fallback_error")
    logging_system.close_logging()
    captured = capsys.readouterr()
    assert "logging_initialization_failed" in captured.err
    assert "fallback_error" in captured.err
