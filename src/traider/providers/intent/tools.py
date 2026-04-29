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
from .store import (
    VALID_INSTRUMENTS,
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

        logger.info(
            "record_trade_intent symbol=%s side=%s qty=%s status=%s instrument=%s",
            symbol, side, quantity, status, instrument_type,
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

        logger.info(
            "update_trade_intent id=%s status=%s fill=%s",
            intent_id, status, fill_price,
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
