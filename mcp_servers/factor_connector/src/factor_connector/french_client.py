"""Client for the Ken French Data Library.

Ken French (Dartmouth Tuck) hosts the canonical Fama-French factor
series, momentum/reversal factors, and a large collection of sort-
based and industry portfolio returns at
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html

Files are served as ZIP-wrapped CSVs under a stable URL pattern:

    https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/<name>_CSV.zip

Each CSV contains one or more sections separated by blank lines:

    <header notes>                   # the first section is free-form prose
                                     # (CRSP vintage, construction notes, …)

     Average Value Weighted Returns -- Monthly   # section title
    ,Col1,Col2,Col3                               # column header row
    192607, 1.44, 13.90, ...                      # data rows (period, values)
                                                  # blank line terminates

     Average Equal Weighted Returns -- Monthly
    ...

Factor files (F-F_Research_Data_Factors, momentum, reversal) have an
implicit first section with no title (the "(monthly) Factors" table).

## Caching

The source updates once a month (factor files) or daily only for the
`*_daily_CSV.zip` variants. Polite use means not re-fetching the ZIP
on every tool call. The client caches each ZIP on disk under
``~/.cache/traider-factor-connector/`` with a mtime-based TTL (default
24h). A cache-expired fetch that fails **raises** — per hub AGENTS.md
no silent fallback to stale data. Force-refresh via ``refresh=True``.

Each parsed response carries cache metadata so the model can see
exactly what it's looking at:

    {
        "source_url": "...",
        "fetched_at":  "2026-04-19T22:15:00+00:00",  # when this ZIP was fetched
        "from_cache":  True,                          # came from disk, not http
        "cache_age_seconds": 12345,                   # 0 on a fresh fetch
        "ttl_seconds": 86400,                         # TTL applied to this call
        "sections": [ ... ],                          # parsed tables
    }
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("factor_connector.french")

_BASE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"

DEFAULT_TTL_SECONDS = 24 * 3600

_SENTINEL_MISSING = {"-99.99", "-999", "-999.0"}


class FrenchError(RuntimeError):
    """Base class for Ken French data-library client errors."""


class FrenchFetchError(FrenchError):
    """ZIP fetch failed (network error or non-2xx response)."""


class FrenchParseError(FrenchError):
    """CSV inside the ZIP did not match the expected shape."""


@dataclass(frozen=True)
class Section:
    """One contiguous data block inside a Ken French CSV."""

    title: str | None
    columns: list[str]
    rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "columns": self.columns,
            "row_count": len(self.rows),
            "rows": self.rows,
        }


@dataclass(frozen=True)
class ParsedFile:
    """A fully parsed Ken French CSV."""

    header_notes: str
    sections: list[Section]

    def section_titles(self) -> list[str | None]:
        return [s.title for s in self.sections]

    def find_section(self, title_contains: str) -> Section | None:
        needle = title_contains.lower()
        for s in self.sections:
            if s.title and needle in s.title.lower():
                return s
        return None


@dataclass
class _CacheEntry:
    bytes_: bytes
    fetched_at: datetime


@dataclass
class FrenchClient:
    cache_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "FACTOR_CACHE_DIR",
                str(Path.home() / ".cache" / "traider-factor-connector"),
            )
        )
    )
    timeout: float = 30.0
    user_agent: str = "traider-factor-connector/0.1 (+https://github.com)"
    _http: httpx.Client | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def zip_url(self, dataset_filename: str) -> str:
        """Build a dataset URL from its filename stem (without `_CSV.zip`)."""
        return f"{_BASE_URL}/{dataset_filename}_CSV.zip"

    def fetch_zip(
        self,
        dataset_filename: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        refresh: bool = False,
    ) -> tuple[bytes, bool, datetime]:
        """Return the raw ZIP bytes for ``<dataset_filename>_CSV.zip``.

        Uses a disk cache at ``cache_dir`` with the file's own mtime as
        the fetched-at timestamp. Returns ``(bytes, from_cache,
        fetched_at)``.

        If the cache entry is still within ``ttl_seconds`` and
        ``refresh=False``, we serve it without touching the network.
        Otherwise we fetch; a fetch failure on an expired entry raises
        (we do not serve stale data silently).
        """
        url = self.zip_url(dataset_filename)
        cache_path = self.cache_dir / f"{dataset_filename}_CSV.zip"

        with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            if not refresh and cache_path.exists():
                mtime = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
                age = (datetime.now(timezone.utc) - mtime).total_seconds()
                if age < ttl_seconds:
                    logger.info(
                        "french cache hit dataset=%s age=%.0fs ttl=%ds",
                        dataset_filename, age, ttl_seconds,
                    )
                    return cache_path.read_bytes(), True, mtime

            logger.info("french fetch dataset=%s url=%s", dataset_filename, url)
            try:
                resp = self._client().get(url)
            except httpx.HTTPError as exc:
                raise FrenchFetchError(
                    f"could not fetch {url}: {exc}"
                ) from exc
            if resp.status_code >= 400:
                body = resp.text[:300]
                raise FrenchFetchError(
                    f"Ken French returned {resp.status_code} for {url}: {body}"
                )
            body = resp.content
            tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(cache_path)
            fetched_at = datetime.now(timezone.utc)
            os.utime(cache_path, (time.time(), fetched_at.timestamp()))
            return body, False, fetched_at

    def fetch_csv_text(
        self,
        dataset_filename: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        refresh: bool = False,
    ) -> tuple[str, bool, datetime]:
        """Fetch the ZIP and extract the single CSV inside."""
        body, from_cache, fetched_at = self.fetch_zip(
            dataset_filename, ttl_seconds=ttl_seconds, refresh=refresh,
        )
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = [
                    n for n in zf.namelist()
                    if n.lower().endswith(".csv") or n.lower().endswith(".txt")
                ]
                if not names:
                    raise FrenchParseError(
                        f"ZIP for {dataset_filename} contains no CSV/TXT member: "
                        f"{zf.namelist()!r}"
                    )
                text = zf.read(names[0]).decode("latin-1")
        except zipfile.BadZipFile as exc:
            raise FrenchParseError(
                f"malformed ZIP for {dataset_filename}: {exc}"
            ) from exc
        return text, from_cache, fetched_at

    def load(
        self,
        dataset_filename: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        refresh: bool = False,
    ) -> tuple[ParsedFile, dict[str, Any]]:
        """Fetch + parse a Ken French CSV.

        Returns ``(parsed_file, metadata)`` where metadata is a dict
        suitable for merging into an MCP tool response.
        """
        text, from_cache, fetched_at = self.fetch_csv_text(
            dataset_filename, ttl_seconds=ttl_seconds, refresh=refresh,
        )
        parsed = parse_csv(text)
        age = 0 if not from_cache else int(
            (datetime.now(timezone.utc) - fetched_at).total_seconds()
        )
        meta: dict[str, Any] = {
            "dataset_filename": dataset_filename,
            "source_url": self.zip_url(dataset_filename),
            "fetched_at": fetched_at.isoformat(),
            "from_cache": from_cache,
            "cache_age_seconds": age,
            "ttl_seconds": ttl_seconds,
        }
        return parsed, meta


def parse_csv(text: str) -> ParsedFile:
    """Parse a Ken French CSV into header notes + typed sections.

    The file layout is:

    1. A preamble of free-form prose (CRSP vintage, construction notes).
    2. One or more data sections, separated by blank lines. Each
       section has:
         - an optional **multi-line** title (prose lines with no commas),
         - a column header row starting with ``,`` (``,Col1,Col2,...``),
         - data rows keyed by a numeric period (``YYYYMMDD`` / ``YYYYMM``
           / ``YYYY``) in the first column.
    3. A trailing copyright line.

    The first data section in factor files has no title — the periodic
    block follows the preamble directly.
    """
    if not text:
        raise FrenchParseError("empty CSV")

    blocks = _split_blocks(text.splitlines())
    if not blocks:
        raise FrenchParseError("no non-empty blocks found in CSV")

    header_notes: list[str] = []
    sections: list[Section] = []
    seen_first_data_block = False

    for block in blocks:
        header_idx = _find_header_row(block)
        if header_idx is None:
            # Block has no data header — it's prose. Prepended prose
            # before the first data block is header_notes; prose blocks
            # after (e.g. the copyright line) are discarded.
            if not seen_first_data_block:
                header_notes.extend(line.rstrip() for line in block if line.strip())
            continue
        seen_first_data_block = True
        title_lines = [line.strip() for line in block[:header_idx] if line.strip()]
        title = " ".join(title_lines) if title_lines else None
        columns = _parse_column_header(block[header_idx])
        rows: list[dict[str, Any]] = []
        for line in block[header_idx + 1:]:
            if not line.strip():
                continue
            if not line.lstrip()[:1].isdigit():
                # Rare: stray prose mid-block. Skip rather than fail —
                # upstream has added side-notes between rows historically.
                logger.debug("skipping non-data line inside block: %r", line)
                continue
            parsed_row = _parse_data_row(line, columns)
            if parsed_row is not None:
                rows.append(parsed_row)
        sections.append(Section(title=title, columns=columns, rows=rows))

    if not sections:
        raise FrenchParseError(
            "no data sections found — file layout may have changed"
        )

    return ParsedFile(
        header_notes="\n".join(header_notes).strip(),
        sections=sections,
    )


def _split_blocks(lines: list[str]) -> list[list[str]]:
    """Split on blank lines. Empty blocks are dropped."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _find_header_row(block: list[str]) -> int | None:
    """Return the index of the ``,Col1,Col2,…`` header row, or None.

    Matches single-column factor files (``,Mom``) as well as multi-
    column portfolio files (``,NoDur,Durbl,Manuf,…``).
    """
    for idx, line in enumerate(block):
        if line.startswith(",") and line[1:].strip():
            return idx
    return None


def _parse_column_header(line: str) -> list[str]:
    reader = csv.reader([line])
    fields = next(reader)
    return [c.strip() for c in fields[1:]]


def _parse_data_row(line: str, columns: list[str]) -> dict[str, Any] | None:
    fields = [f.strip() for f in next(csv.reader([line]))]
    if not fields or not fields[0]:
        return None
    period = fields[0]
    values = fields[1:]
    row: dict[str, Any] = {"period": period, "date": _period_to_iso(period)}
    for col, raw in zip(columns, values):
        row[col] = _coerce_value(raw)
    return row


def _period_to_iso(period: str) -> str | None:
    """Map a Ken French period field to an ISO-ish date string.

    - ``YYYYMMDD`` → ``YYYY-MM-DD`` (daily / weekly files)
    - ``YYYYMM``   → ``YYYY-MM``    (monthly files)
    - ``YYYY``     → ``YYYY``       (annual block)
    """
    p = period.strip()
    if len(p) == 8 and p.isdigit():
        return f"{p[0:4]}-{p[4:6]}-{p[6:8]}"
    if len(p) == 6 and p.isdigit():
        return f"{p[0:4]}-{p[4:6]}"
    if len(p) == 4 and p.isdigit():
        return p
    return None


def _coerce_value(raw: str) -> Any:
    if raw == "" or raw in _SENTINEL_MISSING:
        return None
    try:
        return float(raw)
    except ValueError:
        return raw


def filter_rows_by_date(
    rows: list[dict[str, Any]],
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    """Restrict rows to ``[start_date, end_date]`` inclusive.

    Comparison is lexicographic on the ISO ``date`` field, which works
    for ``YYYY-MM-DD``, ``YYYY-MM``, and ``YYYY`` alike as long as both
    sides of the comparison share the same shape. Callers should pass
    bounds in a format compatible with the rows they're filtering
    (e.g. ``"2020-01"`` for monthly, ``"2020-01-02"`` for daily).
    """
    if start_date is None and end_date is None:
        return rows
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r.get("date")
        if d is None:
            continue
        if start_date is not None and d < start_date:
            continue
        if end_date is not None and d > end_date:
            continue
        out.append(r)
    return out
