"""Per-connector log-file wiring.

Each connector calls ``attach_profile_logger`` from its ``register()``
to pin its module logger to ``<log_dir>/<profile>.log``. The hub's
root logger keeps the aggregated ``traider.log`` in the same directory.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_attached: set[tuple[str, str]] = set()


def attach_profile_logger(logger_name: str, log_file: Path) -> logging.Logger:
    """Attach a rotating file handler for one connector's logger.

    Idempotent: called more than once with the same (logger, path)
    is a no-op, so reloading a profile during tests doesn't duplicate
    handlers.
    """
    key = (logger_name, str(log_file))
    lg = logging.getLogger(logger_name)
    if key in _attached:
        return lg

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    lg.addHandler(handler)
    if lg.level == logging.NOTSET or lg.level > logging.INFO:
        lg.setLevel(logging.INFO)
    _attached.add(key)
    return lg
