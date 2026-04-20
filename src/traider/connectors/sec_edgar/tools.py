"""SEC EDGAR tools registered on the shared FastMCP.

Tool surface covers the four things a trading analyst reaches EDGAR for:

- **Filings** — 10-K / 10-Q / 8-K / S-1 / proxy, by company or full-text search.
- **Insider transactions** — Form 4 parses by issuer.
- **Institutional holdings** — 13F informationTable parses per manager.
- **XBRL company facts** — structured financial concepts over time.

All responses include a ``source`` and ``fetched_at`` so the model (and
the user) can see exactly where the data came from and when. Raw SEC
shapes are passed through with minimal reshaping — per hub AGENTS.md,
hiding fields behind a translation layer makes debugging harder.
"""
from __future__ import annotations

import atexit
import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_profile_logger
from ...settings import TraiderSettings
from . import form4_parser, form13f_parser
from .edgar_client import SecEdgarClient, SecEdgarError
from .ticker_map import Company, TickerMap

logger = logging.getLogger("traider.sec_edgar")
_client: SecEdgarClient | None = None
_ticker_map: TickerMap | None = None


def _get_client() -> SecEdgarClient:
    global _client, _ticker_map
    if _client is None:
        logger.info("initializing SEC EDGAR client")
        _client = SecEdgarClient.from_env()
        _ticker_map = TickerMap(_client)
        atexit.register(_client.close)
        logger.info("SEC EDGAR client ready")
    return _client


def _get_ticker_map() -> TickerMap:
    _get_client()
    assert _ticker_map is not None
    return _ticker_map


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolve(ticker_or_cik: str) -> Company:
    return _get_ticker_map().lookup(ticker_or_cik)


def _flatten_recent(
    cik: str, recent: dict[str, Any],
) -> list[dict[str, Any]]:
    """SEC packs ``filings.recent`` as column-oriented arrays. Transpose."""
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    forms = recent.get("form", [])
    primary_docs = recent.get("primaryDocument", [])
    primary_doc_descs = recent.get("primaryDocDescription", [])
    is_xbrls = recent.get("isXBRL", [])
    rows: list[dict[str, Any]] = []
    for i, acc in enumerate(accessions):
        nodash = acc.replace("-", "")
        primary_doc = (
            primary_docs[i] if i < len(primary_docs) else None
        )
        primary_doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/"
            f"{primary_doc}" if primary_doc else None
        )
        rows.append({
            "accession_number": acc,
            "accession_nodash": nodash,
            "filing_date": filing_dates[i] if i < len(filing_dates) else None,
            "report_date": report_dates[i] if i < len(report_dates) else None,
            "form": forms[i] if i < len(forms) else None,
            "primary_doc_name": primary_doc,
            "primary_doc": primary_doc_url,
            "primary_doc_description": (
                primary_doc_descs[i]
                if i < len(primary_doc_descs) else None
            ),
            "is_xbrl": bool(is_xbrls[i]) if i < len(is_xbrls) else False,
        })
    return rows


def _pick_form4_xml(row: dict[str, Any]) -> str | None:
    """The Form 4 primary document is almost always ``<accession>.xml``
    or named like ``doc4.xml`` / ``form4.xml``. The submissions feed
    sometimes points at an ``.html`` rendering — fall back to the
    conventional XML name."""
    name = row.get("primary_doc_name") or ""
    if name.lower().endswith(".xml"):
        return name
    acc_nodash = row["accession_nodash"]
    return f"{acc_nodash}.xml"


def _pick_information_table(cik: str, row: dict[str, Any]) -> str | None:
    """Find the informationTable XML inside a 13F-HR filing.

    The filing's primary document is the cover page (``primary_doc.xml``
    / ``.html``); the holdings live in a separate XML whose name has
    varied over time (``infotable.xml``, ``informationtable.xml``,
    ``<accession>_infotable.xml``). We ask EDGAR's ``index.json`` for
    the filing and pick the one whose name matches.
    """
    name = (row.get("primary_doc_name") or "").lower()
    if "infotable" in name or "informationtable" in name:
        return row["primary_doc_name"]
    try:
        index = _get_client().filing_index(cik, row["accession_nodash"])
    except SecEdgarError:
        return None
    for item in index.get("directory", {}).get("item", []):
        iname = (item.get("name") or "").lower()
        if iname.endswith(".xml") and (
            "infotable" in iname or "informationtable" in iname
        ):
            return item["name"]
    return None


def _normalize_accession(accession_number: str) -> str:
    """Return the 18-char nodash form regardless of input shape."""
    stripped = accession_number.replace("-", "").strip()
    if len(stripped) != 18 or not stripped.isdigit():
        raise SecEdgarError(
            f"accession_number must be 18 digits (optionally dashed); "
            f"got {accession_number!r}"
        )
    return stripped


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_profile_logger("traider.sec_edgar", settings.log_file("sec-edgar"))

    @mcp.tool()
    def search_companies(query: str, limit: int = 20) -> dict[str, Any]:
        """Ticker / name search over SEC's canonical ticker map.

        Case-insensitive substring match against ticker and company name.
        Returns at most ``limit`` hits. This is the cheapest way to find a
        CIK; every other tool in this profile accepts a ticker or CIK via
        ``ticker_or_cik``.

        Note: SEC's map covers US-listed operating companies. Funds and
        many foreign private issuers are only addressable by CIK — pass
        the numeric CIK directly to the per-company tools if a ticker
        search doesn't find them.
        """
        logger.info("search_companies query=%r limit=%d", query, limit)
        try:
            hits = _get_ticker_map().search(query, limit=limit)
        except Exception:
            logger.exception("search_companies failed query=%r", query)
            raise
        return {
            "source": "https://www.sec.gov/files/company_tickers.json",
            "ticker_map_fetched_at": _get_ticker_map().fetched_at,
            "query": query,
            "count": len(hits),
            "results": [c.to_dict() for c in hits],
        }

    @mcp.tool()
    def get_company_filings(
        ticker_or_cik: str,
        form_types: list[str] | None = None,
        since: str | None = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Recent filings for one company, newest first.

        Args:
            ticker_or_cik: Ticker (``"AAPL"``) or CIK (``"320193"`` /
                ``"0000320193"``).
            form_types: Filter to a list of form codes (e.g.
                ``["10-K", "10-Q", "8-K"]``). Amendments — ``10-K/A``,
                ``10-Q/A``, ``8-K/A`` — are separate codes; include them
                explicitly if you want them.
            since: ISO ``YYYY-MM-DD``; only filings on or after this date.
            limit: Max rows returned (post-filter).

        Each row includes ``accession_number``, ``filing_date``,
        ``report_date``, ``form``, ``primary_doc`` (the URL of the main
        document), and ``primary_doc_description``.

        Foreign private issuers file ``20-F`` (annual) and ``6-K``
        (interim) instead of ``10-K`` / ``10-Q``. Include those form
        codes explicitly if you're covering FPIs.
        """
        logger.info(
            "get_company_filings %s form_types=%s since=%s limit=%d",
            ticker_or_cik, form_types, since, limit,
        )
        try:
            company = _resolve(ticker_or_cik)
            submissions = _get_client().submissions(company.cik)
        except Exception:
            logger.exception("get_company_filings failed %s", ticker_or_cik)
            raise

        recent = submissions.get("filings", {}).get("recent", {})
        rows = _flatten_recent(company.cik, recent)
        if form_types:
            allowed = {f.upper() for f in form_types}
            rows = [r for r in rows if (r["form"] or "").upper() in allowed]
        if since:
            rows = [r for r in rows if (r["filing_date"] or "") >= since]
        rows = rows[:limit]

        return {
            "source": f"https://data.sec.gov/submissions/CIK{company.cik}.json",
            "fetched_at": _now_iso(),
            "company": company.to_dict(),
            "count": len(rows),
            "filings": rows,
        }

    @mcp.tool()
    def get_filing(ticker_or_cik: str, accession_number: str) -> dict[str, Any]:
        """Metadata + document index for one filing.

        ``accession_number`` accepts either form: ``0000320193-24-000123``
        or ``000032019324000123``.
        """
        logger.info(
            "get_filing %s accession=%s", ticker_or_cik, accession_number,
        )
        try:
            company = _resolve(ticker_or_cik)
            nodash = _normalize_accession(accession_number)
            index = _get_client().filing_index(company.cik, nodash)
        except Exception:
            logger.exception(
                "get_filing failed %s accession=%s",
                ticker_or_cik, accession_number,
            )
            raise

        base = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(company.cik)}/{nodash}"
        )
        items = index.get("directory", {}).get("item", [])
        documents = [
            {
                "name": item.get("name"),
                "type": item.get("type"),
                "size": item.get("size"),
                "url": f"{base}/{item.get('name')}",
            }
            for item in items
        ]
        return {
            "source": f"{base}/index.json",
            "fetched_at": _now_iso(),
            "company": company.to_dict(),
            "accession_number": accession_number,
            "accession_nodash": nodash,
            "documents": documents,
        }

    @mcp.tool()
    def search_filings(
        query: str,
        form_types: list[str] | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Full-text search over all EDGAR filings (``efts.sec.gov``).

        Use this when the user cares about *what a filing says* rather
        than which filings a known company has made. The query accepts
        phrase operators (``"going concern"``) and boolean syntax that
        EDGAR's search front-end supports.

        Returns snippets plus accession numbers / filer CIKs — not the
        full documents. Follow up with :func:`get_filing` to pull the
        document list for any hit.
        """
        logger.info(
            "search_filings query=%r form_types=%s start=%s end=%s limit=%d",
            query, form_types, date_start, date_end, limit,
        )
        try:
            raw = _get_client().full_text_search(
                query,
                forms=form_types,
                date_start=date_start,
                date_end=date_end,
            )
        except Exception:
            logger.exception("search_filings failed query=%r", query)
            raise

        hits_raw = raw.get("hits", {}).get("hits", [])[:limit]
        hits: list[dict[str, Any]] = []
        for hit in hits_raw:
            src = hit.get("_source", {})
            display_names = src.get("display_names") or []
            adsh = src.get("adsh") or hit.get("_id", "").split(":")[0]
            hits.append({
                "accession_number": adsh,
                "form": src.get("form"),
                "filing_date": src.get("file_date"),
                "ciks": src.get("ciks"),
                "display_names": display_names,
                "snippet": " … ".join(
                    hit.get("highlight", {}).get("_all", [])
                ) or None,
                "score": hit.get("_score"),
            })

        return {
            "source": "https://efts.sec.gov/LATEST/search-index",
            "fetched_at": _now_iso(),
            "query": query,
            "form_types": form_types,
            "date_start": date_start,
            "date_end": date_end,
            "total": raw.get("hits", {}).get("total", {}),
            "count": len(hits),
            "results": hits,
        }

    @mcp.tool()
    def get_insider_transactions(
        ticker_or_cik: str,
        since: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Parsed Form 4 (insider-transaction) filings for one issuer.

        Fetches the issuer's recent Form 4 filings from the submissions
        feed, pulls the primary XML document of each, and parses into a
        flat list of transactions. Scope is **issuer-only** in v1: to see
        a single insider's trades across all their companies, you'd need
        to fan out by reporting-owner CIK — file an issue if that's
        useful.

        Args:
            ticker_or_cik: Issuer ticker or CIK.
            since: ISO ``YYYY-MM-DD``; only filings on or after this date.
            limit: Max Form 4 filings to parse (each may contain multiple
                transactions). Default 20 balances signal vs. HTTP cost
                (10 req/sec rate limit).

        Transaction codes worth knowing: ``P`` open-market purchase,
        ``S`` open-market sale, ``A`` grant/award, ``M`` exercise of
        derivative, ``F`` tax withholding, ``G`` gift. Open-market
        ``P`` / ``S`` are the high-signal ones; ``A`` / ``F`` are
        compensation plumbing.
        """
        logger.info(
            "get_insider_transactions %s since=%s limit=%d",
            ticker_or_cik, since, limit,
        )
        try:
            company = _resolve(ticker_or_cik)
            submissions = _get_client().submissions(company.cik)
            recent = submissions.get("filings", {}).get("recent", {})
            filings = _flatten_recent(company.cik, recent)
            form4_rows = [
                r for r in filings if (r["form"] or "").upper() == "4"
            ]
            if since:
                form4_rows = [
                    r for r in form4_rows if (r["filing_date"] or "") >= since
                ]
            form4_rows = form4_rows[:limit]

            parsed: list[dict[str, Any]] = []
            for row in form4_rows:
                xml_name = _pick_form4_xml(row)
                if not xml_name:
                    continue
                try:
                    xml = _get_client().archive_document(
                        company.cik, row["accession_nodash"], xml_name,
                    )
                    doc = form4_parser.parse(xml)
                except Exception as exc:
                    logger.warning(
                        "form4 parse failed cik=%s accession=%s doc=%s: %s",
                        company.cik, row["accession_number"], xml_name, exc,
                    )
                    continue
                parsed.append({
                    "accession_number": row["accession_number"],
                    "filing_date": row["filing_date"],
                    "primary_doc": row["primary_doc"],
                    "document": doc,
                })
        except Exception:
            logger.exception(
                "get_insider_transactions failed %s", ticker_or_cik,
            )
            raise

        return {
            "source": f"https://data.sec.gov/submissions/CIK{company.cik}.json",
            "fetched_at": _now_iso(),
            "company": company.to_dict(),
            "count": len(parsed),
            "filings": parsed,
        }

    @mcp.tool()
    def get_institutional_portfolio(
        cik: str,
        accession_number: str | None = None,
    ) -> dict[str, Any]:
        """Parsed 13F informationTable for one institutional filer.

        Args:
            cik: The manager's CIK (not the issuer of the stocks — the
                *filer*, e.g. Berkshire Hathaway's CIK 0001067983).
            accession_number: Specific 13F accession to parse. If omitted,
                the most recent 13F-HR is used.

        Note: 13F values were filed in **thousands of dollars** for
        periods ending before 2022-09-30 and **whole dollars** after.
        The ``unit`` field on the response reports which applies. SEC
        requires 13F within 45 days of quarter-end, so the most recent
        filing reflects a quarter that's at least 45 days old.

        Reverse lookup (who holds a given ticker) is not supported in v1
        — it would require an in-process index of every manager's
        holdings. Use this tool per-manager.
        """
        logger.info(
            "get_institutional_portfolio cik=%s accession=%s",
            cik, accession_number,
        )
        try:
            company = _resolve(cik)
            submissions = _get_client().submissions(company.cik)
            recent = submissions.get("filings", {}).get("recent", {})
            filings = _flatten_recent(company.cik, recent)
            if accession_number:
                target_nodash = _normalize_accession(accession_number)
                matches = [
                    r for r in filings
                    if r["accession_nodash"] == target_nodash
                ]
                if not matches:
                    raise SecEdgarError(
                        f"accession {accession_number} not found on CIK {company.cik}"
                    )
                chosen = matches[0]
            else:
                hr = [
                    r for r in filings
                    if (r["form"] or "").upper() == "13F-HR"
                ]
                if not hr:
                    raise SecEdgarError(
                        f"no 13F-HR filings found for CIK {company.cik}"
                    )
                chosen = hr[0]

            xml_name = _pick_information_table(company.cik, chosen)
            if not xml_name:
                raise SecEdgarError(
                    f"no informationTable XML in {chosen['accession_number']}"
                )
            xml = _get_client().archive_document(
                company.cik, chosen["accession_nodash"], xml_name,
            )
            parsed = form13f_parser.parse(
                xml, period_of_report=chosen.get("report_date"),
            )
        except Exception:
            logger.exception(
                "get_institutional_portfolio failed cik=%s", cik,
            )
            raise

        return {
            "source": f"https://data.sec.gov/submissions/CIK{company.cik}.json",
            "fetched_at": _now_iso(),
            "filer": company.to_dict(),
            "accession_number": chosen["accession_number"],
            "form": chosen["form"],
            "filing_date": chosen["filing_date"],
            "report_date": chosen.get("report_date"),
            "information_table": parsed,
        }

    @mcp.tool()
    def get_company_facts(ticker_or_cik: str) -> dict[str, Any]:
        """Full XBRL ``companyfacts`` blob for one company.

        Returns SEC's raw ``companyfacts/CIK…json`` essentially
        unchanged. The payload can be large for mega-caps (several MB);
        prefer :func:`get_company_concept` when you only need one line
        item. The blob is organized as ``facts[taxonomy][concept]`` with
        ``units`` keyed by currency / share-count.
        """
        logger.info("get_company_facts %s", ticker_or_cik)
        try:
            company = _resolve(ticker_or_cik)
            facts = _get_client().company_facts(company.cik)
        except Exception:
            logger.exception("get_company_facts failed %s", ticker_or_cik)
            raise
        return {
            "source": (
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company.cik}.json"
            ),
            "fetched_at": _now_iso(),
            "company": company.to_dict(),
            "facts": facts,
        }

    @mcp.tool()
    def get_company_concept(
        ticker_or_cik: str,
        concept: str,
        taxonomy: str = "us-gaap",
    ) -> dict[str, Any]:
        """One XBRL concept's reported values over time for one company.

        Args:
            ticker_or_cik: Target company.
            concept: XBRL concept name, e.g. ``"Revenues"``,
                ``"NetIncomeLoss"``, ``"Assets"``, ``"CashAndCashEquivalentsAtCarryingValue"``.
            taxonomy: ``us-gaap`` (default), ``ifrs-full`` (FPIs), or
                ``dei`` (document entity info).

        **Concept names are not uniform across filers.** Some companies
        report ``Revenues``, others ``SalesRevenueNet``, others
        ``RevenueFromContractWithCustomerExcludingAssessedTax``. If a
        concept isn't reported, EDGAR returns 404 — the tool surfaces
        that as ``SecEdgarError``. Try a close alias, or pull
        :func:`get_company_facts` to see what the filer actually tags.
        """
        logger.info(
            "get_company_concept %s concept=%s taxonomy=%s",
            ticker_or_cik, concept, taxonomy,
        )
        try:
            company = _resolve(ticker_or_cik)
            payload = _get_client().company_concept(
                company.cik, concept, taxonomy=taxonomy,
            )
        except Exception:
            logger.exception(
                "get_company_concept failed %s concept=%s",
                ticker_or_cik, concept,
            )
            raise
        return {
            "source": (
                f"https://data.sec.gov/api/xbrl/companyconcept/CIK{company.cik}/"
                f"{taxonomy}/{concept}.json"
            ),
            "fetched_at": _now_iso(),
            "company": company.to_dict(),
            "concept": concept,
            "taxonomy": taxonomy,
            "payload": payload,
        }

    @mcp.tool()
    def get_frame(
        concept: str,
        period: str,
        taxonomy: str = "us-gaap",
        unit: str = "USD",
    ) -> dict[str, Any]:
        """Cross-sectional XBRL frame: one concept across all filers for one period.

        Args:
            concept: XBRL concept, e.g. ``"Revenues"``, ``"Assets"``.
            period: EDGAR frame notation. Duration periods:
                ``"CY2024"`` (annual), ``"CY2024Q4"`` (quarterly). For
                *instantaneous* concepts (balance-sheet items like
                ``Assets``), append ``I``: ``"CY2024Q4I"``. Calendar-year
                periods only — fiscal-year-offset values are not in
                frames.
            taxonomy: ``us-gaap`` (default), ``ifrs-full``, or ``dei``.
            unit: ``USD`` (default), ``shares``, etc.

        Useful for peer comps ("rank every filer by Revenues this
        quarter") and macro aggregates. Not every concept/period
        combination is populated; EDGAR returns 404 if the frame is
        empty.
        """
        logger.info(
            "get_frame concept=%s period=%s taxonomy=%s unit=%s",
            concept, period, taxonomy, unit,
        )
        try:
            payload = _get_client().frame(
                concept, period, taxonomy=taxonomy, unit=unit,
            )
        except Exception:
            logger.exception(
                "get_frame failed concept=%s period=%s", concept, period,
            )
            raise
        return {
            "source": (
                f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/"
                f"{concept}/{unit}/{period}.json"
            ),
            "fetched_at": _now_iso(),
            "concept": concept,
            "period": period,
            "taxonomy": taxonomy,
            "unit": unit,
            "payload": payload,
        }
