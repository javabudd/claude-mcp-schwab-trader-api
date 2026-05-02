"""Compact analyst summaries of option-chain payloads.

Raw chains can run tens of thousands of tokens for a single expiration
once strike counts are reasonable, which is unusable for direct LLM
consumption. This module extracts the analyst-relevant stats — ATM
straddle / implied move, IV skew wings, OI and volume clusters — into
a bounded-size summary keyed per expiration.

Input shape is the canonical chain payload every market-data backend
in this repo emits: top-level ``underlyingPrice`` / ``symbol`` /
``isDelayed`` plus ``callExpDateMap`` and ``putExpDateMap`` keyed by
``"YYYY-MM-DD:dte"`` → strike (string) → list of contract dicts.
"""
from __future__ import annotations

from typing import Any


def _mark(contract: dict[str, Any]) -> float | None:
    m = contract.get("mark")
    if m is not None:
        return float(m)
    bid, ask = contract.get("bid"), contract.get("ask")
    if bid is not None and ask is not None and (bid or ask):
        return (float(bid) + float(ask)) / 2.0
    last = contract.get("last")
    return float(last) if last is not None else None


def _leg(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "strike": contract.get("strikePrice"),
        "symbol": contract.get("symbol"),
        "mark": _mark(contract),
        "bid": contract.get("bid"),
        "ask": contract.get("ask"),
        "iv": contract.get("volatility"),
        "delta": contract.get("delta"),
        "openInterest": contract.get("openInterest"),
        "totalVolume": contract.get("totalVolume"),
        "inTheMoney": contract.get("inTheMoney"),
    }


def _pick_atm(strikes: list[float], underlying: float) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - underlying))


def _strike_list(strike_map: dict[str, list[dict[str, Any]]]) -> list[float]:
    out: list[float] = []
    for k in strike_map.keys():
        try:
            out.append(float(k))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def _first(contracts: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not contracts:
        return None
    return contracts[0]


def _rank_by(
    strike_map: dict[str, list[dict[str, Any]]],
    field: str,
    top_n: int,
) -> list[dict[str, Any]]:
    rows: list[tuple[float, int, dict[str, Any]]] = []
    for strike_key, contracts in strike_map.items():
        c = _first(contracts)
        if not c:
            continue
        value = c.get(field)
        if not value:
            continue
        try:
            strike = float(strike_key)
            rows.append((strike, int(value), c))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[1], reverse=True)
    return [
        {"strike": s, field: v, "symbol": c.get("symbol")}
        for s, v, c in rows[:top_n]
    ]


def _skew_wings(
    call_map: dict[str, list[dict[str, Any]]],
    put_map: dict[str, list[dict[str, Any]]],
    atm: float,
    strikes: list[float],
    wings: int,
) -> list[dict[str, Any]]:
    if atm is None or not strikes:
        return []
    atm_idx = strikes.index(atm) if atm in strikes else min(
        range(len(strikes)), key=lambda i: abs(strikes[i] - atm),
    )
    lo = max(0, atm_idx - wings)
    hi = min(len(strikes), atm_idx + wings + 1)
    out: list[dict[str, Any]] = []
    for s in strikes[lo:hi]:
        key = _lookup_key(call_map, s) or _lookup_key(put_map, s)
        if key is None:
            continue
        call_c = _first(call_map.get(key))
        put_c = _first(put_map.get(key))
        out.append({
            "strike": s,
            "distanceFromAtm": round(s - atm, 4),
            "callIv": call_c.get("volatility") if call_c else None,
            "putIv": put_c.get("volatility") if put_c else None,
        })
    return out


def _lookup_key(
    strike_map: dict[str, list[dict[str, Any]]],
    strike: float,
) -> str | None:
    for k in strike_map.keys():
        try:
            if float(k) == strike:
                return k
        except (TypeError, ValueError):
            continue
    return None


def _summarize_expiration(
    exp_key: str,
    call_map: dict[str, list[dict[str, Any]]],
    put_map: dict[str, list[dict[str, Any]]],
    underlying: float,
    wings: int,
    top_n: int,
) -> dict[str, Any]:
    expiration, _, dte_str = exp_key.partition(":")
    try:
        dte = int(dte_str) if dte_str else None
    except ValueError:
        dte = None

    strikes = sorted(set(_strike_list(call_map)) | set(_strike_list(put_map)))
    atm = _pick_atm(strikes, underlying)

    atm_call = _first(call_map.get(_lookup_key(call_map, atm) or ""))
    atm_put = _first(put_map.get(_lookup_key(put_map, atm) or ""))

    call_leg = _leg(atm_call) if atm_call else None
    put_leg = _leg(atm_put) if atm_put else None

    straddle = None
    implied_move_pct = None
    implied_range: list[float] | None = None
    if call_leg and put_leg and call_leg["mark"] and put_leg["mark"]:
        straddle = round(call_leg["mark"] + put_leg["mark"], 4)
        implied_move_pct = round(100.0 * straddle / underlying, 4)
        implied_range = [
            round(underlying - straddle, 4),
            round(underlying + straddle, 4),
        ]

    return {
        "expiration": expiration,
        "daysToExpiration": dte,
        "atmStrike": atm,
        "atmCall": call_leg,
        "atmPut": put_leg,
        "straddleCost": straddle,
        "impliedMovePct": implied_move_pct,
        "impliedRange": implied_range,
        "skew": _skew_wings(call_map, put_map, atm, strikes, wings),
        "topCallOpenInterest": _rank_by(call_map, "openInterest", top_n),
        "topPutOpenInterest": _rank_by(put_map, "openInterest", top_n),
        "topCallVolume": _rank_by(call_map, "totalVolume", top_n),
        "topPutVolume": _rank_by(put_map, "totalVolume", top_n),
    }


def summarize_chain(
    chain: dict[str, Any],
    wings: int = 5,
    top_n: int = 5,
) -> dict[str, Any]:
    """Produce a bounded-size analyst view of an option chain.

    Per expiration: ATM straddle cost and implied one-day move, IV skew
    across ±``wings`` strikes around ATM, top ``top_n`` strikes by open
    interest and volume on each side.

    Pass-through: ``symbol``, ``underlyingPrice``, ``isDelayed``,
    ``dataQualityWarning`` (when the source provider emits it),
    ``strategy``, ``status``.

    Raises ``ValueError`` if the payload has no underlying price — the
    ATM anchor can't be chosen without it.
    """
    underlying = chain.get("underlyingPrice")
    if underlying is None:
        raise ValueError("chain missing underlyingPrice — cannot anchor ATM")
    underlying = float(underlying)

    call_expmap = chain.get("callExpDateMap") or {}
    put_expmap = chain.get("putExpDateMap") or {}
    exp_keys = sorted(set(call_expmap.keys()) | set(put_expmap.keys()))

    expirations = [
        _summarize_expiration(
            exp,
            call_expmap.get(exp) or {},
            put_expmap.get(exp) or {},
            underlying,
            wings,
            top_n,
        )
        for exp in exp_keys
    ]

    summary: dict[str, Any] = {
        "symbol": chain.get("symbol"),
        "underlyingPrice": underlying,
        "status": chain.get("status"),
        "strategy": chain.get("strategy"),
        "isDelayed": chain.get("isDelayed"),
        "wings": wings,
        "topN": top_n,
        "expirations": expirations,
    }
    if "dataQualityWarning" in chain:
        summary["dataQualityWarning"] = chain["dataQualityWarning"]
    return summary
