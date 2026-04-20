"""Runtime settings shared across the hub and per-connector modules.

Settings carry the *parsed* shape of the environment so individual
connector ``register()`` functions don't each re-parse env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TraiderSettings:
    """Shared settings passed to every connector's ``register()``.

    Attributes:
        profiles: Ordered tuple of enabled profile names (e.g.
            ``("schwab", "fred", "news")``). Driven by
            ``TRAIDER_PROFILES`` (comma-separated).
        log_dir: Base directory for per-connector log files. Each
            connector writes to ``<log_dir>/<profile>.log``. Override
            with ``TRAIDER_LOG_DIR`` (default: ``logs/`` in cwd).
        extra: Opaque pass-through map of the process env, so
            connector-specific vars (``FRED_API_KEY``,
            ``SCHWAB_TOKEN_FILE``, …) remain readable without
            re-reading ``os.environ`` in every module.
    """

    profiles: tuple[str, ...]
    log_dir: Path
    extra: dict[str, str] = field(default_factory=dict)

    def log_file(self, profile: str) -> Path:
        """Path to the log file for one connector/profile."""
        return self.log_dir / f"{profile}.log"


def _parse_profiles(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def load_settings() -> TraiderSettings:
    """Build a ``TraiderSettings`` from the current process env."""
    profiles = _parse_profiles(os.environ.get("TRAIDER_PROFILES"))
    log_dir = Path(os.environ.get("TRAIDER_LOG_DIR", "logs")).resolve()
    return TraiderSettings(
        profiles=profiles,
        log_dir=log_dir,
        extra=dict(os.environ),
    )
