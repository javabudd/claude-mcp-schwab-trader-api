"""Trade-intent tools registered on the shared FastMCP instance.

This provider is the only one in traider that writes — but it writes
*locally*, to a SQLite file the user owns (default
``~/.traider/intents.db``, override with ``TRAIDER_INTENT_DB``). It
is **not** a brokerage write path: nothing here places, modifies, or
cancels an order on Schwab or anywhere else, and nothing here syncs
to an external service. The traider read-only constraint applies to
external systems; this is a personal journal of *why* each share or
contract exists, queryable in future conversations.

Workflow this is meant to support:

1. While planning a trade with the analyst, call
   ``record_trade_intent`` with the symbol, side, quantity, target
   price, the thesis, and the levels (stop / target). The record is
   created in ``planned`` status.
2. After the user fills the order in their brokerage, call
   ``update_trade_intent`` with the actual ``fill_price`` and a
   ``status`` of ``open``.
3. On any future session, call ``list_trade_intents`` (filter by
   symbol or account) before recommending a trim/add — the prior
   reasoning shows up alongside the position.
4. When the position is closed, call ``update_trade_intent`` with
   ``status="closed"`` and a closing note via ``append_note``.

The store is local; nothing here should ever be construed as
'placing a trade'.
"""
from __future__ import annotations

import atexit
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_provider_logger
from ...settings import TraiderSettings
from . import account_profile as _profile_mod
from . import rules as _rules_mod
from .store import (
    VALID_CLASSES,
    VALID_INSTRUMENTS,
    VALID_LIFECYCLES,
    VALID_SIDES,
    VALID_STATUSES,
    IntentStore,
    validate_inputs,
)

logger = logging.getLogger("traider.intent")
_store: IntentStore | None = None


def _get_store() -> IntentStore:
    global _store
    if _store is None:
        logger.info("initializing intent SQLite store")
        _store = IntentStore()
        atexit.register(_store.close)
        logger.info("intent store ready path=%s", _store.db_path)
    return _store


def _resolve_rule_refs(
    refs: list[str] | list[dict[str, Any]] | None,
    *,
    intent_class: str | None,
    intent_account_type: str | None,
    intent_params: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve rule_refs from a list of names (or pre-built dicts) to the
    canonical ``[{rule, version, content_hash_at_fill}]`` shape, capturing
    the current merged rule's version + hash at the moment of recording.

    Validates:
      - every rule name resolves in the index,
      - the intent's class is in each rule's applies_to_class,
      - the intent's account_type (if given) matches the rule's
        applies_to_account_type (if the rule restricts it),
      - the intent's params include every key listed in each rule's
        requires_intent_params.

    Raises ValueError on any failure with a message that names the rule
    and the specific check that failed.
    """
    if not refs:
        return []
    index = _rules_mod.get_index()
    out: list[dict[str, Any]] = []
    intent_params = intent_params or {}
    for entry in refs:
        if isinstance(entry, str):
            name = entry
            stored_version: int | None = None
            stored_hash: str | None = None
        elif isinstance(entry, dict):
            name = entry.get("rule")  # type: ignore[assignment]
            stored_version = entry.get("version")
            stored_hash = entry.get("content_hash_at_fill")
        else:
            raise ValueError(
                f"rule_refs entry must be str or dict; got {type(entry).__name__}"
            )
        if not name:
            raise ValueError("rule_refs entry missing rule name")
        rule = index.get(name)
        if rule is None:
            raise ValueError(
                f"rule {name!r} does not resolve to a rule file in the rules index"
            )
        if intent_class and intent_class not in rule.applies_to_class:
            raise ValueError(
                f"intent class {intent_class!r} not in rule {name!r} "
                f"applies_to_class={list(rule.applies_to_class)}"
            )
        if (
            intent_account_type
            and rule.applies_to_account_type
            and intent_account_type not in rule.applies_to_account_type
        ):
            raise ValueError(
                f"intent account_type {intent_account_type!r} not in rule "
                f"{name!r} applies_to_account_type={list(rule.applies_to_account_type)}"
            )
        missing = [
            p for p in rule.requires_intent_params if p not in intent_params
        ]
        if missing:
            raise ValueError(
                f"intent params missing keys required by rule {name!r}: "
                f"{missing}"
            )
        out.append({
            "rule": name,
            "version": stored_version if stored_version is not None else rule.version,
            "content_hash_at_fill": stored_hash or rule.content_hash,
        })
    return out


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_provider_logger("traider.intent", settings.log_file("intent"))

    @mcp.tool()
    def record_trade_intent(
        symbol: str,
        side: str,
        quantity: float,
        thesis: str,
        instrument_type: str = "equity",
        target_price: float | None = None,
        fill_price: float | None = None,
        status: str = "planned",
        horizon: str | None = None,
        stop_price: float | None = None,
        target_exit_price: float | None = None,
        catalysts: str | None = None,
        tags: list[str] | None = None,
        option_details: dict[str, Any] | None = None,
        parent_intent_id: str | None = None,
        account_id: str | None = None,
        external_order_id: str | None = None,
        notes: str | None = None,
        # v0.5 structured framework references and per-position payload.
        # See rules/README.md for the rule schema.
        class_: str | None = None,
        lifecycle: str | None = None,
        sleeve_id: str | None = None,
        rule_refs: list[str] | list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        catalysts_structured: list[dict[str, Any]] | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """Save a new trade-intent record to the local intent journal.

        This is a **local write only** — it does NOT place an order on
        any brokerage. Use it to capture the *why* behind a planned or
        just-filled position so future sessions can recall the
        reasoning when looking at the current book.

        Recommended usage:

        - At trade-design time (status defaults to ``planned``): record
          ``symbol``, ``side``, ``quantity``, ``target_price``,
          ``stop_price``, ``target_exit_price``, ``thesis``,
          ``horizon``, and any ``catalysts`` / ``tags``.
        - After fill: call ``update_trade_intent`` with
          ``fill_price`` and ``status="open"``.
        - For multi-leg / scaled entries: pass ``parent_intent_id`` of
          the original record to link a leg or add to its parent.

        Args:
            symbol: Ticker (or option underlier). Stored upper-cased.
            side: ``buy`` / ``sell`` / ``short`` / ``cover``.
            quantity: Shares for equities/ETFs, contracts for options
                (signed positive — direction lives in ``side``).
            thesis: Free-text reason for the trade. The single most
                important field — this is what the user will read in
                a future session to remember why the position exists.
            instrument_type: ``equity`` (default) / ``etf`` /
                ``option`` / ``future`` / ``crypto``.
            target_price: Intended fill price (limit). Optional for
                market orders.
            fill_price: Actual fill, if known at record time. Usually
                left blank and added later via ``update_trade_intent``.
            status: ``planned`` (default) / ``open`` /
                ``partially_filled`` / ``closed`` / ``canceled``.
            horizon: Free-text time horizon, e.g. ``intraday``,
                ``swing-2w``, ``LTCG-hold``.
            stop_price: Hard stop level. Pair with the level
                discussion in ``RISK.md``.
            target_exit_price: Profit-take level used to size the
                trade's R/R.
            catalysts: Free-text list of upcoming catalysts the trade
                is exposed to (e.g. ``"ER 2026-04-30, FOMC 2026-05-01"``).
            tags: Free-form labels (``["hedge", "earnings-play"]``).
                Useful for grouping later.
            option_details: For options trades, a dict capturing
                option type / strike / expiration / structure, e.g.
                ``{"type": "put", "strike": 480, "expiration":
                "2026-05-16", "structure": "long-put"}``. Multi-leg
                structures can either record one intent per leg with
                a shared ``parent_intent_id`` or one combined intent
                with a list of legs in this field — pick whichever
                matches how the user thinks about the position.
            parent_intent_id: ID of a prior intent this one extends
                (an add, a scale, an option leg, a roll).
            account_id: Optional brokerage account hash so the model
                can match intents to a specific account when the user
                runs more than one. Use the same ``hashValue`` the
                Schwab account tools return.
            external_order_id: Optional brokerage order ID, for
                hand-correlation if the user looks the trade up later.
            notes: Free-text journal entry, captured at record time.

        Returns:
            The full saved record (with the auto-generated ``id``,
            ``created_at``, ``updated_at``).
        """
        validate_inputs(instrument_type, side, status)
        if quantity <= 0:
            raise ValueError(f"quantity must be > 0; got {quantity}")
        if not thesis or not thesis.strip():
            raise ValueError("thesis is required — record the *why* of the trade")
        if class_ is not None and class_ not in VALID_CLASSES:
            raise ValueError(
                f"class must be one of {sorted(VALID_CLASSES)}; got {class_!r}"
            )
        if lifecycle is not None and lifecycle not in VALID_LIFECYCLES:
            raise ValueError(
                f"lifecycle must be one of {sorted(VALID_LIFECYCLES)}; "
                f"got {lifecycle!r}"
            )

        # Resolve rule_refs (validates names, class fit, account-type fit,
        # required intent params; captures version + content hash at fill).
        resolved_refs = _resolve_rule_refs(
            rule_refs,
            intent_class=class_,
            intent_account_type=account_type,
            intent_params=params,
        )

        logger.info(
            "record_trade_intent symbol=%s side=%s qty=%s status=%s "
            "instrument=%s class=%s lifecycle=%s rules=%s",
            symbol, side, quantity, status, instrument_type,
            class_, lifecycle,
            [r["rule"] for r in resolved_refs] if resolved_refs else None,
        )
        try:
            return _get_store().insert(
                symbol=symbol,
                side=side,
                quantity=quantity,
                thesis=thesis,
                instrument_type=instrument_type,
                target_price=target_price,
                fill_price=fill_price,
                status=status,
                horizon=horizon,
                stop_price=stop_price,
                target_exit_price=target_exit_price,
                catalysts=catalysts,
                tags=tags,
                option_details=option_details,
                parent_intent_id=parent_intent_id,
                account_id=account_id,
                external_order_id=external_order_id,
                notes=notes,
                class_=class_,
                lifecycle=lifecycle,
                sleeve_id=sleeve_id,
                rule_refs=resolved_refs or None,
                params=params,
                catalysts_structured=catalysts_structured,
            )
        except Exception:
            logger.exception("record_trade_intent failed")
            raise

    @mcp.tool()
    def update_trade_intent(
        intent_id: str,
        status: str | None = None,
        fill_price: float | None = None,
        stop_price: float | None = None,
        target_exit_price: float | None = None,
        target_price: float | None = None,
        quantity: float | None = None,
        thesis: str | None = None,
        horizon: str | None = None,
        catalysts: str | None = None,
        tags: list[str] | None = None,
        option_details: dict[str, Any] | None = None,
        external_order_id: str | None = None,
        append_note: str | None = None,
        # v0.5 structured fields (see record_trade_intent for semantics).
        class_: str | None = None,
        lifecycle: str | None = None,
        sleeve_id: str | None = None,
        rule_refs: list[str] | list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        catalysts_structured: list[dict[str, Any]] | None = None,
        account_type: str | None = None,
    ) -> dict[str, Any]:
        """Amend an existing intent record (post-fill, on an exit, etc.).

        Use this to capture state changes the user makes outside the
        chat: a fill price after the order executed, a stop adjusted
        to breakeven, a thesis update if the setup evolved, or a
        ``status="closed"`` when the trade is exited. Any field not
        passed is left as-is. Pass ``append_note`` to add a
        UTC-timestamped line to the running journal in ``notes``
        (the existing notes are preserved).

        Args:
            intent_id: The ID returned from ``record_trade_intent``.
            status: New status — ``planned`` / ``open`` /
                ``partially_filled`` / ``closed`` / ``canceled``.
            fill_price: Actual fill, set this once the order executes.
            stop_price / target_exit_price / target_price: Adjusted
                levels (e.g. trail stop, raise target).
            quantity: Updated size (e.g. partial fill, scaled out).
            thesis: Replace the thesis text outright. Most of the time
                you want ``append_note`` instead — keep the original
                reasoning intact and journal what changed.
            horizon / catalysts / tags / option_details /
                external_order_id: Updated metadata.
            append_note: Free-text line to append to the journal
                with a UTC timestamp.

        Returns:
            The updated record, or an error envelope if no intent
            with that ID exists.
        """
        if status is not None:
            validate_inputs(
                instrument_type=next(iter(VALID_INSTRUMENTS)),
                side=next(iter(VALID_SIDES)),
                status=status,
            )
        if class_ is not None and class_ not in VALID_CLASSES:
            raise ValueError(
                f"class must be one of {sorted(VALID_CLASSES)}; got {class_!r}"
            )
        if lifecycle is not None and lifecycle not in VALID_LIFECYCLES:
            raise ValueError(
                f"lifecycle must be one of {sorted(VALID_LIFECYCLES)}; "
                f"got {lifecycle!r}"
            )

        # If new rule_refs were passed, re-resolve and re-validate against
        # the intent's effective class/account/params (existing values
        # used as fallback when not being updated this call).
        resolved_refs: list[dict[str, Any]] | None = None
        if rule_refs is not None:
            existing = _get_store().get(intent_id) or {}
            effective_class = class_ or existing.get("class")
            effective_params = params if params is not None else existing.get("params")
            resolved_refs = _resolve_rule_refs(
                rule_refs,
                intent_class=effective_class,
                intent_account_type=account_type,
                intent_params=effective_params,
            )

        logger.info(
            "update_trade_intent id=%s status=%s fill=%s class=%s lifecycle=%s",
            intent_id, status, fill_price, class_, lifecycle,
        )
        try:
            updated = _get_store().update(
                intent_id,
                status=status,
                fill_price=fill_price,
                stop_price=stop_price,
                target_exit_price=target_exit_price,
                target_price=target_price,
                quantity=quantity,
                thesis=thesis,
                horizon=horizon,
                catalysts=catalysts,
                tags=tags,
                option_details=option_details,
                external_order_id=external_order_id,
                append_note=append_note,
                class_=class_,
                lifecycle=lifecycle,
                sleeve_id=sleeve_id,
                rule_refs=resolved_refs,
                params=params,
                catalysts_structured=catalysts_structured,
            )
        except Exception:
            logger.exception("update_trade_intent failed id=%s", intent_id)
            raise
        if updated is None:
            return {"error": f"no intent with id={intent_id!r}"}
        return updated

    @mcp.tool()
    def get_trade_intent(intent_id: str) -> dict[str, Any]:
        """Fetch one trade-intent record by ID."""
        logger.info("get_trade_intent id=%s", intent_id)
        try:
            record = _get_store().get(intent_id)
        except Exception:
            logger.exception("get_trade_intent failed id=%s", intent_id)
            raise
        if record is None:
            return {"error": f"no intent with id={intent_id!r}"}
        return record

    @mcp.tool()
    def list_trade_intents(
        symbol: str | None = None,
        status: str | None = None,
        account_id: str | None = None,
        instrument_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        class_: str | None = None,
        lifecycle: str | None = None,
        sleeve_id: str | None = None,
        rule_name: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List trade intents, newest first, filtered by the given keys.

        **Call this before recommending action on any held position.**
        Intent records are authoritative on stops, targets, sizing
        rules (concentration caps, trim ladders), catalyst plans, and
        tax notes — the original thesis, levels, and catalysts are
        what tell you why each share or contract is in the book. A
        recommendation that contradicts an open intent's stated
        discipline is a failure of analysis, not a contribution to
        it: defer to the intent's framework, flag drift from it, and
        recommend within it.

        Filter by ``symbol`` for per-name reasoning, by
        ``status="open"`` for everything currently in the book, by
        ``account_id`` to scope to a single brokerage account.

        Args:
            symbol: Filter to one ticker (case-insensitive — stored
                upper-cased).
            status: One of ``planned`` / ``open`` /
                ``partially_filled`` / ``closed`` / ``canceled``.
            account_id: Schwab ``hashValue`` (or any user-chosen
                account identifier) to match per-account.
            instrument_type: Filter to ``equity`` / ``etf`` /
                ``option`` / ``future`` / ``crypto``.
            since / until: ISO timestamps filtering on
                ``created_at`` (e.g. ``2026-01-01``).
            limit: Page size (default 100). Records are returned
                newest first.

        Returns:
            ``{"count": N, "intents": [...]}`` where each intent is the
            full record shape returned by ``record_trade_intent``.
        """
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_STATUSES)}; got {status!r}"
            )
        if instrument_type is not None and instrument_type not in VALID_INSTRUMENTS:
            raise ValueError(
                f"instrument_type must be one of {sorted(VALID_INSTRUMENTS)}; "
                f"got {instrument_type!r}"
            )
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be 1..1000; got {limit}")

        logger.info(
            "list_trade_intents symbol=%s status=%s account=%s instrument=%s "
            "since=%s until=%s limit=%d",
            symbol, status, account_id, instrument_type, since, until, limit,
        )
        try:
            rows = _get_store().list(
                symbol=symbol,
                status=status,
                account_id=account_id,
                instrument_type=instrument_type,
                since=since,
                until=until,
                class_=class_,
                lifecycle=lifecycle,
                sleeve_id=sleeve_id,
                rule_name=rule_name,
                limit=limit,
            )
        except Exception:
            logger.exception("list_trade_intents failed")
            raise
        return {"count": len(rows), "intents": rows}

    @mcp.tool()
    def delete_trade_intent(intent_id: str, confirm: bool = False) -> dict[str, Any]:
        """Permanently delete a trade-intent record.

        Almost always the wrong move — prefer ``update_trade_intent``
        with ``status="canceled"`` so the historical record (and its
        thesis) survives. Use delete only when the entry was a typo
        or duplicate. Requires ``confirm=True`` to actually run.

        Args:
            intent_id: The ID of the record to remove.
            confirm: Must be ``True`` to perform the delete.

        Returns:
            ``{"deleted": bool, "id": str}``.
        """
        if not confirm:
            return {
                "deleted": False,
                "id": intent_id,
                "error": "delete requires confirm=True; prefer status='canceled' instead",
            }
        logger.info("delete_trade_intent id=%s", intent_id)
        try:
            removed = _get_store().delete(intent_id)
        except Exception:
            logger.exception("delete_trade_intent failed id=%s", intent_id)
            raise
        return {"deleted": removed, "id": intent_id}

    # ------------------------------------------------------------------
    # Rules surface

    @mcp.tool()
    def list_rules(
        applies_to_class: str | None = None,
        governs_decision: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """List framework rules from ``rules/`` (with ``rules.local/`` overrides).

        Returns lightweight summaries — call ``get_rule(name)`` for the
        full body. Use this to discover which rules apply to a position
        class or which rules govern a particular decision (e.g. trim,
        monetize, hold-through-event).

        Args:
            applies_to_class: Filter to rules that govern this position
                class (``leadership`` / ``thematic`` / ``speculative``
                / ``hedge`` / ``dry-powder`` / ``diversifier`` /
                ``index-core``).
            governs_decision: Filter to rules that fire on this
                decision type (``add`` / ``trim`` / ``open`` / ``exit``
                / ``monetize`` / ``roll`` / ``rotate`` / ``rebalance``
                / ``portfolio-check`` / ``hold-through-event`` / etc.).
            kind: Filter to rules of one kind (``concentration-cap``,
                ``lifecycle``, ``hedge-mgmt``, ``tax-discipline``,
                ``sizing``).

        Returns:
            ``{"count": N, "rules": [...]}`` where each entry has
            ``name``, ``version``, ``kind``, ``applies_to_class``,
            ``governs_decisions``, ``summary`` (first line of
            rationale), ``content_hash``, ``overridden`` (bool).
        """
        try:
            index = _rules_mod.get_index()
            matches = index.filter(
                applies_to_class=applies_to_class,
                governs_decision=governs_decision,
                kind=kind,
            )
        except Exception:
            logger.exception("list_rules failed")
            raise
        return {
            "count": len(matches),
            "rules": [r.summary() for r in matches],
        }

    @mcp.tool()
    def get_rule(name: str, include_rationale: bool = True) -> dict[str, Any]:
        """Fetch one rule by name with its full body.

        Returns the merged-in-place form (seed + overlay) — the same
        shape ``record_trade_intent`` resolves against. Pass
        ``include_rationale=False`` to omit the (often long) prose
        rationale when only parameters are needed.

        Returns:
            The rule dict, or ``{"error": "..."}`` if not found.
        """
        try:
            rule = _rules_mod.get_index().get(name)
        except Exception:
            logger.exception("get_rule failed name=%s", name)
            raise
        if rule is None:
            return {"error": f"no rule named {name!r}"}
        return rule.to_dict(include_rationale=include_rationale)

    @mcp.tool()
    def reload_rules() -> dict[str, Any]:
        """Force the rules index to re-read ``rules/`` and ``rules.local/``.

        Use after editing a rule file or override during a session, to
        avoid restarting the server. Returns a summary of what loaded.
        """
        try:
            index = _rules_mod.reload_index()
        except Exception:
            logger.exception("reload_rules failed")
            raise
        return {
            "count": len(index),
            "rules": [r.summary() for r in index.all()],
        }

    # ------------------------------------------------------------------
    # Account profile surface

    @mcp.tool()
    def get_account_profile(account_id: str | None = None) -> dict[str, Any]:
        """Return the user's account profile (defaults + per-account merge).

        Profiles are user-authored YAML at
        ``~/.traider/account-profiles.yaml`` (override with
        ``TRAIDER_ACCOUNT_PROFILES``) capturing framing the brokerage
        API can't supply: the user's age, the role this account plays
        in their total wealth (``trading-sleeve`` vs
        ``primary-wealth`` vs ``retirement``), risk capacity, and
        analyst-facing notes.

        **Call this on allocation, sizing, or "is this too
        aggressive/conservative for my age" questions** — before
        applying any general-purpose framework like "60/40 at 37" or
        "cash drag is high." The user's framing of *this* account
        determines whether such heuristics apply.

        If no profile file exists, returns an empty profile (every
        field ``None``); ``_has_file: False`` indicates no file was
        found. In that case, ask the user the framing questions you
        need rather than guessing — and consider pointing them at
        ``account-profiles.example.yaml`` at the repo root.

        Args:
            account_id: User-chosen key from the YAML's ``accounts:``
                block (a friendly alias like ``"schwab-trading"``, or
                a Schwab ``hashValue`` if the user keyed by that).
                ``None`` returns just the ``defaults:`` block.

        Returns:
            Merged profile dict. Always contains the documented
            fields (``user_age``, ``total_wealth_context``, ``role``,
            ``risk_capacity``, ``description``, ``notes_to_analyst``)
            plus ``_source`` (file path), ``_matched_account_key``
            (which account block was merged, if any), and
            ``_has_file`` (whether a profile file was found at all).
            Unknown YAML keys pass through verbatim.
        """
        try:
            return _profile_mod.get_index().get(account_id)
        except Exception:
            logger.exception("get_account_profile failed account_id=%s", account_id)
            raise

    @mcp.tool()
    def list_account_profiles() -> dict[str, Any]:
        """List every account block configured in the profile YAML.

        Use to discover which account aliases the user has set up,
        or to confirm whether a profile file exists at all.

        Returns:
            ``{"source": str | None, "has_file": bool,
              "defaults": {...}, "accounts": {alias: {...}, ...}}``.
        """
        try:
            return _profile_mod.get_index().list_all()
        except Exception:
            logger.exception("list_account_profiles failed")
            raise

    @mcp.tool()
    def reload_account_profiles() -> dict[str, Any]:
        """Force the profile loader to re-read the YAML from disk.

        Use after the user edits ``~/.traider/account-profiles.yaml``
        during a session, to avoid restarting the server.
        """
        try:
            index = _profile_mod.reload_index()
        except Exception:
            logger.exception("reload_account_profiles failed")
            raise
        return index.list_all()

    @mcp.tool()
    def validate_intent_rule_refs(intent_id: str | None = None) -> dict[str, Any]:
        """Verify that intent rule_refs resolve and detect drift.

        For one intent (if ``intent_id`` is passed) or every open intent
        (default), the tool reports:

          - ``dangling``: refs whose rule name no longer resolves to a
            file in ``rules/`` (the rule was deleted or renamed).
          - ``drifted``: refs whose stored ``content_hash_at_fill``
            differs from the current merged rule's hash (the rule has
            been edited since the intent was filed; recommendations
            against this intent should re-evaluate the framework).
          - ``stale_versions``: refs whose stored ``version`` is older
            than the current rule's version (a *material* edit
            happened upstream).

        Run this before recommending action on any held position when
        rule files have changed since the intent was filed.

        Returns:
            ``{intent_id: {dangling: [...], drifted: [...],
            stale_versions: [...]}, ...}``.
        """
        store = _get_store()
        index = _rules_mod.get_index()
        if intent_id:
            rec = store.get(intent_id)
            if rec is None:
                return {"error": f"no intent with id={intent_id!r}"}
            return {intent_id: index.validate_refs(rec.get("rule_refs") or [])}
        out: dict[str, Any] = {}
        for rec in store.list(status="open", limit=1000):
            refs = rec.get("rule_refs") or []
            if not refs:
                continue
            result = index.validate_refs(refs)
            if any(result.values()):
                out[rec["id"]] = result
        return {"count": len(out), "issues": out}

    @mcp.tool()
    def get_position_context(symbol: str) -> dict[str, Any]:
        """Bundle of position context for one symbol.

        One-call replacement for the typical fan-out (positions +
        intents + rules + drift checks + account framing) when
        evaluating a position.

        Returns:
            ``{
              "symbol": str,
              "intents": [...],                 # open intents on this symbol
              "applicable_rules": [...],        # full bodies, deduplicated
              "rule_drift": {...},              # validate_refs output, per intent
              "sleeves": {sleeve_id: [...]},    # legs of any sleeve this symbol participates in
              "account_profile": {
                  "defaults": {...},            # always present (empty if no file)
                  "by_account": {acct_id: {...}},  # one entry per unique account_id on the open intents
                  "_has_file": bool,
              },
            }``
        """
        store = _get_store()
        index = _rules_mod.get_index()
        profile_index = _profile_mod.get_index()
        intents = store.list(symbol=symbol, status="open", limit=100)

        # Collect referenced rule names across intents.
        rule_names: list[str] = []
        rule_drift: dict[str, Any] = {}
        sleeve_ids: set[str] = set()
        account_ids: set[str] = set()
        for rec in intents:
            for ref in rec.get("rule_refs") or []:
                name = ref.get("rule")
                if name and name not in rule_names:
                    rule_names.append(name)
            sid = rec.get("sleeve_id")
            if sid:
                sleeve_ids.add(sid)
            acct = rec.get("account_id")
            if acct:
                account_ids.add(acct)
            refs = rec.get("rule_refs") or []
            if refs:
                rd = index.validate_refs(refs)
                if any(rd.values()):
                    rule_drift[rec["id"]] = rd

        applicable_rules = []
        for name in rule_names:
            rule = index.get(name)
            if rule is not None:
                applicable_rules.append(rule.to_dict(include_rationale=False))

        sleeves: dict[str, list[dict[str, Any]]] = {}
        for sid in sleeve_ids:
            sleeves[sid] = store.list_sleeve_legs(sid)

        # Defaults always present; per-account merge for any account_id
        # seen on the open intents (lets multi-account users see the
        # right framing per intent).
        by_account = {acct: profile_index.get(acct) for acct in sorted(account_ids)}
        account_profile = {
            "defaults": profile_index.get(None),
            "by_account": by_account,
            "_has_file": profile_index.has_file,
        }

        return {
            "symbol": symbol.upper(),
            "intents": intents,
            "applicable_rules": applicable_rules,
            "rule_drift": rule_drift,
            "sleeves": sleeves,
            "account_profile": account_profile,
        }
