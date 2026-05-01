"""User-authored account-profile loader.

A profile captures *framing* about an account that the brokerage API
can't supply: the user's age, the role this account plays in their
total wealth, risk capacity, and analyst-facing notes. Surfaces via
MCP tools so the model can reason about "is this allocation right
for me" without re-asking the same clarifying questions every
session.

File location:
  - ``$TRAIDER_ACCOUNT_PROFILES`` if set, else
  - ``~/.traider/account-profiles.yaml``.

Missing file is fine — :func:`get_index` returns an empty index and
all tool calls degrade to empty values. Cloners get the example file
at the repo root (``account-profiles.example.yaml``) as a starting
template; nothing breaks if they ignore it.

Schema (loose; unknown keys pass through verbatim with an info log):

.. code-block:: yaml

    defaults:
      user_age: 37
      total_wealth_context: |
        Multi-line free text describing the user's broader picture
        (other accounts, real estate, crypto, income, etc.).
      risk_capacity: high          # high | medium | low (free text — not enforced)
      notes_to_analyst: |
        Anything the user wants the analyst to keep in mind across
        every account.

    accounts:
      "my-trading-alias":          # user-chosen key; can be Schwab hashValue
        role: trading-sleeve       # trading-sleeve | primary-wealth | retirement | …
        description: Free text.
        notes_to_analyst: |
          Per-account framing that overrides defaults.

This module is local-only — it does not call any external service.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("traider.intent.account_profile")

_DEFAULT_PATH = Path.home() / ".traider" / "account-profiles.yaml"

# Documented fields. Unknown fields are *not* rejected — they pass
# through to the tool output verbatim. This keeps the schema flexible
# while still letting the loader log a hint when the user typos a
# documented field name.
_KNOWN_FIELDS = frozenset({
    "user_age",
    "total_wealth_context",
    "role",
    "risk_capacity",
    "description",
    "notes_to_analyst",
})


def _resolve_path() -> Path:
    raw = os.environ.get("TRAIDER_ACCOUNT_PROFILES")
    return Path(raw).expanduser() if raw else _DEFAULT_PATH


def empty_profile() -> dict[str, Any]:
    """Baseline profile shape returned when nothing is configured."""
    return {
        "user_age": None,
        "total_wealth_context": None,
        "role": None,
        "risk_capacity": None,
        "description": None,
        "notes_to_analyst": None,
    }


class AccountProfileIndex:
    """In-memory view of the loaded YAML, with defaults + per-account blocks."""

    def __init__(
        self,
        defaults: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
        source: Path | None,
    ) -> None:
        self._defaults = defaults
        self._accounts = accounts
        self._source = source

    @property
    def source(self) -> Path | None:
        return self._source

    @property
    def has_file(self) -> bool:
        return self._source is not None

    def account_keys(self) -> list[str]:
        return sorted(self._accounts.keys())

    def get(self, account_id: str | None) -> dict[str, Any]:
        """Merged profile: empty baseline ← defaults ← per-account.

        Always returns a dict. Missing fields are ``None``. The result
        also carries ``_source`` (file path or ``None``) and
        ``_matched_account_key`` (the key the per-account block was
        merged from, or ``None`` if no match).
        """
        merged: dict[str, Any] = empty_profile()
        merged.update(self._defaults)
        matched: str | None = None
        if account_id and account_id in self._accounts:
            merged.update(self._accounts[account_id])
            matched = account_id
        merged["_source"] = str(self._source) if self._source else None
        merged["_matched_account_key"] = matched
        merged["_has_file"] = self.has_file
        return merged

    def list_all(self) -> dict[str, Any]:
        """Full dump for ``list_account_profiles``."""
        return {
            "source": str(self._source) if self._source else None,
            "has_file": self.has_file,
            "defaults": dict(self._defaults),
            "accounts": {k: dict(v) for k, v in self._accounts.items()},
        }


def _warn_unknown(target: Path, where: str, block: dict[str, Any]) -> None:
    for key in block.keys():
        if key not in _KNOWN_FIELDS:
            logger.info(
                "%s: %s has non-standard key %r — passed through verbatim "
                "(documented keys: %s)",
                target, where, key, sorted(_KNOWN_FIELDS),
            )


def load_profiles(path: Path | None = None) -> AccountProfileIndex:
    """Read and parse the profile YAML, falling back to an empty index.

    Soft-fails on missing file, YAML parse error, or wrong top-level
    shape — logs the issue and returns an empty index so every
    downstream tool keeps working. This is intentional: profile data
    is framing, not data the analysis depends on for correctness.
    """
    target = path or _resolve_path()
    if not target.exists():
        logger.info(
            "no account-profiles file at %s — using empty defaults; "
            "see account-profiles.example.yaml for the schema",
            target,
        )
        return AccountProfileIndex({}, {}, None)
    try:
        with target.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        # Soft-fail: log a single-line warning (no traceback — this is a
        # peripheral config file the user owns; a parse error should be
        # noticed but should not break unrelated tools).
        logger.warning(
            "invalid YAML in %s — using empty defaults (%s)", target, exc,
        )
        return AccountProfileIndex({}, {}, None)
    if raw is None:
        logger.info("%s is empty — using empty defaults", target)
        return AccountProfileIndex({}, {}, target)
    if not isinstance(raw, dict):
        logger.warning(
            "%s top level must be a mapping; using empty defaults", target,
        )
        return AccountProfileIndex({}, {}, target)

    defaults_block = raw.get("defaults") or {}
    if not isinstance(defaults_block, dict):
        logger.warning(
            "%s: 'defaults' must be a mapping; ignoring", target,
        )
        defaults_block = {}
    else:
        _warn_unknown(target, "defaults", defaults_block)

    accounts_block = raw.get("accounts") or {}
    if not isinstance(accounts_block, dict):
        logger.warning(
            "%s: 'accounts' must be a mapping; ignoring", target,
        )
        accounts_block = {}

    accounts: dict[str, dict[str, Any]] = {}
    for key, block in accounts_block.items():
        if not isinstance(block, dict):
            logger.warning(
                "%s: account %r value must be a mapping; ignoring",
                target, key,
            )
            continue
        _warn_unknown(target, f"accounts[{key!r}]", block)
        accounts[str(key)] = dict(block)

    logger.info(
        "loaded account profiles from %s (defaults=%d keys, accounts=%d)",
        target, len(defaults_block), len(accounts),
    )
    return AccountProfileIndex(dict(defaults_block), accounts, target)


_index: AccountProfileIndex | None = None


def get_index() -> AccountProfileIndex:
    """Return the process-wide index, loading on first call."""
    global _index
    if _index is None:
        _index = load_profiles()
    return _index


def reload_index() -> AccountProfileIndex:
    """Force-reload from disk. Useful after editing the file."""
    global _index
    _index = load_profiles()
    return _index
