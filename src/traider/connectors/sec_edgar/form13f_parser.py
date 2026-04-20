"""Parser for SEC Form 13F ``informationTable`` XML.

13F-HR is filed quarterly by institutional managers >$100M AUM,
within 45 days of quarter-end. The informationTable lists every long
equity / listed-option position (shorts are not disclosed).

The informationTable XML is namespaced. SEC has used both the
``ns1:`` prefix and the default namespace over time, so the parser
matches by local-name to be tolerant.

Schema of each position in the output:

    {
        "name_of_issuer": "APPLE INC",
        "title_of_class": "COM",
        "cusip": "037833100",
        "figi": "BBG000B9XRY4",            # if present (not required)
        "value_usd": 1234567.0,             # 13F values are in USD
                                            # (dollars from 2022; thousands before)
        "shares_or_principal": 9876.0,
        "shares_or_principal_type": "SH",   # "SH" shares or "PRN" principal
        "put_call": "PUT",                   # None for stock
        "investment_discretion": "SOLE",
        "voting_sole": 9876,
        "voting_shared": 0,
        "voting_none": 0,
    }

Note on ``value_usd``: SEC required values in **thousands of dollars**
pre-2022-07 and **whole dollars** after. The parser returns the raw
number as filed — callers should check the filing's ``periodOfReport``
to decide which. A convenience ``unit`` field on the envelope reports
the best guess.
"""
from __future__ import annotations

import logging
from typing import Any

from lxml import etree

logger = logging.getLogger("sec_edgar_connector.form13f")


class Form13FParseError(RuntimeError):
    """Raised when an informationTable XML can't be parsed."""


def parse(xml_bytes: bytes, *, period_of_report: str | None = None) -> dict[str, Any]:
    """Parse one 13F informationTable into a list of positions.

    Args:
        xml_bytes: Raw bytes of the informationTable XML.
        period_of_report: ``YYYY-MM-DD`` from the filing's primary doc;
            used only to tag the ``unit`` on the envelope (dollars vs.
            thousands).
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise Form13FParseError(f"invalid 13F XML: {exc}") from exc

    if _local(root.tag) != "informationTable":
        raise Form13FParseError(
            f"expected <informationTable> root, got <{root.tag}>"
        )

    positions: list[dict[str, Any]] = []
    for info in _children(root, "infoTable"):
        positions.append(_parse_info(info))

    return {
        "period_of_report": period_of_report,
        "unit": _infer_unit(period_of_report),
        "count": len(positions),
        "positions": positions,
    }


def _parse_info(info: etree._Element) -> dict[str, Any]:
    voting = _first_child(info, "votingAuthority")
    shares = _first_child(info, "shrsOrPrnAmt")
    return {
        "name_of_issuer": _child_text(info, "nameOfIssuer"),
        "title_of_class": _child_text(info, "titleOfClass"),
        "cusip": _child_text(info, "cusip"),
        "figi": _child_text(info, "figi"),
        "value_usd": _float(_child_text(info, "value")),
        "shares_or_principal": _float(
            _child_text(shares, "sshPrnamt") if shares is not None else None
        ),
        "shares_or_principal_type": (
            _child_text(shares, "sshPrnamtType") if shares is not None else None
        ),
        "put_call": _child_text(info, "putCall"),
        "investment_discretion": _child_text(info, "investmentDiscretion"),
        "voting_sole": _int(
            _child_text(voting, "Sole") if voting is not None else None
        ),
        "voting_shared": _int(
            _child_text(voting, "Shared") if voting is not None else None
        ),
        "voting_none": _int(
            _child_text(voting, "None") if voting is not None else None
        ),
    }


def _infer_unit(period_of_report: str | None) -> str:
    # SEC changed the unit on 13F value from thousands to dollars for
    # periods ending on or after 2022-09-30 (published mid-2022). If we
    # don't know the period, document both possibilities.
    if not period_of_report:
        return "dollars_or_thousands_unknown"
    try:
        year, month, _ = period_of_report.split("-", 2)
    except ValueError:
        return "dollars_or_thousands_unknown"
    if (int(year), int(month)) >= (2022, 9):
        return "dollars"
    return "thousands_of_dollars"


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _children(el: etree._Element, name: str) -> list[etree._Element]:
    return [c for c in el if _local(c.tag) == name]


def _first_child(el: etree._Element, name: str) -> etree._Element | None:
    matches = _children(el, name)
    return matches[0] if matches else None


def _child_text(el: etree._Element | None, name: str) -> str | None:
    if el is None:
        return None
    c = _first_child(el, name)
    if c is None or c.text is None:
        return None
    return c.text.strip() or None


def _float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
