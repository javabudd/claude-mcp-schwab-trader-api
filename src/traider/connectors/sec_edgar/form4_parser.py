"""Parser for SEC Form 4 ``ownershipDocument`` XML.

Form 4 is filed by officers, directors, and 10%+ owners within 2
business days of a transaction. The primary document is a small XML
file (schema ``ownershipDocument``) with both non-derivative and
derivative transactions.

This parser pulls only the fields a trading analyst cares about — filer
identity and role, transaction date, acquired/disposed, shares, price,
post-transaction holdings. We don't try to model every derivative
conversion rule; callers who need more can pull the raw XML.

Schema of each non-derivative transaction in the output:

    {
        "owner_name": "DOE JANE",
        "owner_cik": "0001234567",
        "is_director": True,
        "is_officer": True,
        "is_ten_percent_owner": False,
        "officer_title": "Chief Executive Officer",
        "security_title": "Common Stock",
        "transaction_date": "2026-04-10",
        "transaction_code": "S",      # P=buy, S=sell, A=grant, M=exercise, ...
        "shares": 1234.0,
        "price_per_share": 185.23,
        "acquired_or_disposed": "D",  # "A" acquired or "D" disposed
        "shares_owned_after": 98765.0,
        "ownership_form": "D",         # "D" direct or "I" indirect
    }

Derivative transactions get the same fields plus ``underlying_shares``
and ``conversion_or_exercise_price``.
"""
from __future__ import annotations

import logging
from typing import Any

from lxml import etree

logger = logging.getLogger("sec_edgar_connector.form4")


class Form4ParseError(RuntimeError):
    """Raised when a Form 4 XML document can't be parsed."""


def parse(xml_bytes: bytes) -> dict[str, Any]:
    """Parse one Form 4 ``ownershipDocument`` XML into a dict.

    Returns a payload with ``issuer``, ``reporting_owners``,
    ``non_derivative_transactions``, and ``derivative_transactions``.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise Form4ParseError(f"invalid Form 4 XML: {exc}") from exc

    if root.tag != "ownershipDocument":
        raise Form4ParseError(
            f"expected <ownershipDocument> root, got <{root.tag}>"
        )

    return {
        "document_type": _text(root, "documentType"),
        "period_of_report": _text(root, "periodOfReport"),
        "issuer": _parse_issuer(root),
        "reporting_owners": _parse_reporting_owners(root),
        "non_derivative_transactions": _parse_transactions(
            root, "nonDerivativeTable/nonDerivativeTransaction",
            derivative=False,
        ),
        "derivative_transactions": _parse_transactions(
            root, "derivativeTable/derivativeTransaction",
            derivative=True,
        ),
    }


def _parse_issuer(root: etree._Element) -> dict[str, Any]:
    issuer = root.find("issuer")
    if issuer is None:
        return {}
    return {
        "cik": _text(issuer, "issuerCik"),
        "name": _text(issuer, "issuerName"),
        "trading_symbol": _text(issuer, "issuerTradingSymbol"),
    }


def _parse_reporting_owners(root: etree._Element) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    for owner in root.findall("reportingOwner"):
        ident = owner.find("reportingOwnerId")
        rel = owner.find("reportingOwnerRelationship")
        owners.append({
            "cik": _text(ident, "rptOwnerCik") if ident is not None else None,
            "name": _text(ident, "rptOwnerName") if ident is not None else None,
            "is_director": _bool(rel, "isDirector"),
            "is_officer": _bool(rel, "isOfficer"),
            "is_ten_percent_owner": _bool(rel, "isTenPercentOwner"),
            "is_other": _bool(rel, "isOther"),
            "officer_title": _text(rel, "officerTitle"),
        })
    return owners


def _parse_transactions(
    root: etree._Element, xpath: str, *, derivative: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tx in root.findall(xpath):
        record: dict[str, Any] = {
            "security_title": _nested_text(tx, "securityTitle/value"),
            "transaction_date": _nested_text(tx, "transactionDate/value"),
            "transaction_code": _nested_text(
                tx, "transactionCoding/transactionCode"
            ),
            "shares": _float(
                _nested_text(tx, "transactionAmounts/transactionShares/value")
            ),
            "price_per_share": _float(
                _nested_text(
                    tx, "transactionAmounts/transactionPricePerShare/value"
                )
            ),
            "acquired_or_disposed": _nested_text(
                tx, "transactionAmounts/transactionAcquiredDisposedCode/value"
            ),
            "shares_owned_after": _float(
                _nested_text(
                    tx,
                    "postTransactionAmounts/sharesOwnedFollowingTransaction/value",
                )
            ),
            "ownership_form": _nested_text(
                tx, "ownershipNature/directOrIndirectOwnership/value"
            ),
        }
        if derivative:
            record["conversion_or_exercise_price"] = _float(
                _nested_text(tx, "conversionOrExercisePrice/value")
            )
            record["underlying_security_title"] = _nested_text(
                tx, "underlyingSecurity/underlyingSecurityTitle/value"
            )
            record["underlying_shares"] = _float(
                _nested_text(tx, "underlyingSecurity/underlyingSecurityShares/value")
            )
        out.append(record)
    return out


def _text(el: etree._Element | None, tag: str) -> str | None:
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _nested_text(el: etree._Element | None, path: str) -> str | None:
    if el is None:
        return None
    child = el.find(path)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _bool(el: etree._Element | None, tag: str) -> bool:
    raw = _text(el, tag)
    if raw is None:
        return False
    return raw.strip() in ("1", "true", "True")


def _float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
