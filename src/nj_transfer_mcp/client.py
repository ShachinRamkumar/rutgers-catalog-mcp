"""Fetch and normalize NJ Transfer equivalencies."""

from __future__ import annotations

import contextlib
import csv
import html
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import httpx
from filelock import FileLock
from platformdirs import user_cache_path

NJ_TRANSFER_BASE = "https://www.njtransfer.org"
NJ_TRANSFER_LOOKUP = f"{NJ_TRANSFER_BASE}/artweb/precs.cgi"
SENDING_CODE = "MI"
SENDING_NAME = "Middlesex College"
RECEIVING_CODE = "EN"
RECEIVING_NAME = "Rutgers-School of Engineering"

_COURSE_CODE_RE = re.compile(r"^(\d{1,2})\D+(\d{1,3})\D+(\d{1,3})$")
_ART_ID_RE = re.compile(r"chgri\.cgi\?(\d+)")
_HIDDEN_ART_ID_RE = re.compile(r'name=["\']ArtId["\'][^>]*value=["\'](\d+)["\']', re.I)
_DOWNLOAD_RE = re.compile(
    r'href=["\'](https://(?:www\.)?njtransfer\.org/artweb/tempfiles/[^"\']+\.listeq\.txt)["\']',
    re.I,
)
_RUTGERS_CODE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_FOOTNOTE_RE = re.compile(r'"([^"]+)"')
_FOOTNOTE_DETAIL_RE = re.compile(
    r"<strong>\s*Foot Note:\s*</strong>\s*<em>(.*?)</em>\s*(.*?)(?=<br\s*/?>|</td>)",
    re.IGNORECASE | re.DOTALL,
)
_LINK_DETAIL_RE = re.compile(
    r"<strong>\s*LINK\s*</strong>\s*:\s*(.*?)(?=<br\s*/?>|</td>)",
    re.IGNORECASE | re.DOTALL,
)
_MINIMUM_GRADE_RE = re.compile(
    r"<strong>\s*Minimum Grade:\s*</strong>\s*([^<]*)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SESSION_PATTERNS = (
    (re.compile(r"(precs\.cgi|chgri\.cgi)\?\d+", re.IGNORECASE), r"\1?<redacted>"),
    (re.compile(r"(crs-srch\.cgi\?Z~)\d+(~)", re.IGNORECASE), r"\1<redacted>\2"),
    (re.compile(r"(tempfiles/)[^/\s\"']+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(ArtId=)\d+", re.IGNORECASE), r"\1<redacted>"),
)


def sanitize_error(message: Any) -> str:
    """Redact transient NJ Transfer session identifiers from error messages."""
    text = str(message)
    for pattern, replacement in _SESSION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_TERM_MONTHS = {
    "01": "Winter",
    "02": "Spring",
    "06": "Summer",
    "09": "Fall",
}


class NJTransferError(RuntimeError):
    """Raised when NJ Transfer data cannot be fetched or decoded."""


@dataclass(frozen=True)
class CacheInfo:
    source: str
    fetched_at: str
    stale: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class Equivalency:
    middlesex_course: str
    middlesex_effective_term: str
    rutgers_evaluation_effective_term: str
    rutgers_gen_ed: tuple[str, ...]
    minimum_grade: str | None
    equivalency_raw: str
    rutgers_course_codes: tuple[str, ...]
    footnotes: tuple[str, ...]
    rutgers_min_credits: str
    rutgers_max_credits: str
    transferable: bool
    middlesex_min_credits: str
    middlesex_max_credits: str
    active: bool
    repeatable: bool
    title: str
    middlesex_gen_ed: tuple[str, ...]
    notes: str

    @property
    def evaluation_key(self) -> tuple[int, int]:
        return (
            _term_number(self.rutgers_evaluation_effective_term),
            _term_number(self.middlesex_effective_term),
        )

    @property
    def detail_key(self) -> tuple[str, str, str]:
        return (
            self.middlesex_course,
            self.middlesex_effective_term,
            self.rutgers_evaluation_effective_term,
        )

    @property
    def equivalency_kind(self) -> str:
        upper = self.equivalency_raw.upper()
        if not self.transferable or upper == "NOT TRANSFERABLE":
            return "not_transferable"
        if self.rutgers_course_codes:
            return "exact_course"
        if "NM" in upper:
            return "non_major_credit"
        if "EC" in upper or upper.startswith("TRT"):
            return "elective_credit"
        return "other_transfer_credit"

    def as_dict(self, details: dict[str, Any] | None = None) -> dict[str, Any]:
        details = details or {}
        return {
            "middlesex_course": self.middlesex_course,
            "middlesex_title": self.title,
            "middlesex_credits": _credit_range(
                self.middlesex_min_credits, self.middlesex_max_credits
            ),
            "middlesex_effective_term": format_term(self.middlesex_effective_term),
            "rutgers_equivalency": self.equivalency_raw,
            "rutgers_course_codes": list(self.rutgers_course_codes),
            "rutgers_credits": _credit_range(self.rutgers_min_credits, self.rutgers_max_credits),
            "rutgers_evaluation_effective_term": format_term(
                self.rutgers_evaluation_effective_term
            ),
            "equivalency_kind": self.equivalency_kind,
            "transferable": self.transferable,
            "minimum_grade": details.get("minimum_grade") or self.minimum_grade,
            "footnotes": list(self.footnotes),
            "footnote_details": details.get("footnote_details", []),
            "linked_course_requirement": details.get("linked_course_requirement"),
            "detail_data": details.get("data"),
            "rutgers_gen_ed": list(self.rutgers_gen_ed),
            "middlesex_gen_ed": list(self.middlesex_gen_ed),
            "repeatable": self.repeatable,
            "notes": self.notes or None,
        }


def normalize_rutgers_course_code(value: str) -> str | None:
    """Normalize a Rutgers code to the standard 01:640:152 form."""
    raw = value.strip()
    match = _COURSE_CODE_RE.fullmatch(raw)
    if match:
        school, subject, course = match.groups()
        return f"{int(school):02d}:{int(subject):03d}:{int(course):03d}"

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        return f"{digits[:2]}:{digits[2:5]}:{digits[5:]}"
    return None


def normalize_middlesex_course_code(value: str) -> str | None:
    """Normalize a Middlesex course ID such as MAT-132 to MAT132."""
    compact = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if re.fullmatch(r"[A-Z]{2,5}\d{2,4}[A-Z]?", compact):
        return compact
    return None


def format_term(value: str) -> str:
    """Format NJ Transfer's YYYYMM term value without discarding the raw value."""
    raw = value.strip()
    if len(raw) == 6 and raw.isdigit():
        name = _TERM_MONTHS.get(raw[4:])
        if name:
            return f"{name} {raw[:4]}"
    return raw


def current_equivalencies(records: list[Equivalency]) -> list[Equivalency]:
    """Select the latest evaluation for each currently active Middlesex course."""
    latest: dict[str, Equivalency] = {}
    for record in records:
        if not record.active:
            continue
        existing = latest.get(record.middlesex_course)
        if existing is None or record.evaluation_key > existing.evaluation_key:
            latest[record.middlesex_course] = record
    return sorted(latest.values(), key=lambda item: item.middlesex_course)


def parse_export(text: str) -> list[Equivalency]:
    """Parse the tilde-delimited export generated by NJ Transfer."""
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="~")
        required = {"SI", "Course", "RI", "Receiver Term", "Equivalency", "Transferable"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise NJTransferError("NJ Transfer returned an unexpected export format")

        records: list[Equivalency] = []
        for row in reader:
            if row.get("SI") != SENDING_CODE or row.get("RI") != RECEIVING_CODE:
                continue
            raw_equivalency = (row.get("Equivalency") or "").strip()
            codes = tuple(
                dict.fromkeys(
                    _format_rutgers_digits(value)
                    for value in _RUTGERS_CODE_RE.findall(raw_equivalency)
                )
            )
            quoted = _FOOTNOTE_RE.search(raw_equivalency)
            footnotes = (
                tuple(item.strip() for item in quoted.group(1).split(",") if item.strip())
                if quoted
                else ()
            )
            records.append(
                Equivalency(
                    middlesex_course=(row.get("Course") or "").strip().upper(),
                    middlesex_effective_term=(row.get("Sender Term") or "").strip(),
                    rutgers_evaluation_effective_term=(row.get("Receiver Term") or "").strip(),
                    rutgers_gen_ed=_comma_values(row.get("RI GEd")),
                    minimum_grade=(row.get("Min Grade") or "").strip() or None,
                    equivalency_raw=raw_equivalency,
                    rutgers_course_codes=codes,
                    footnotes=footnotes,
                    rutgers_min_credits=(row.get("RI Min Cr") or "").strip(),
                    rutgers_max_credits=(row.get("RI Max Cr") or "").strip(),
                    transferable=(row.get("Transferable") or "").strip().upper() == "Y",
                    middlesex_min_credits=(row.get("SI Min Cr") or "").strip(),
                    middlesex_max_credits=(row.get("SI Max Cr") or "").strip(),
                    active=(row.get("SI Active") or "").strip().upper() == "Y",
                    repeatable=(row.get("SI Repeatable") or "").strip().upper() == "Y",
                    title=(row.get("Course Title") or "").strip(),
                    middlesex_gen_ed=_comma_values(row.get("SI GEd")),
                    notes=(row.get("Notes") or "").strip(),
                )
            )
    except csv.Error as exc:
        raise NJTransferError(f"NJ Transfer export could not be parsed: {exc}") from None
    if not records:
        raise NJTransferError("NJ Transfer export contained no Middlesex-to-Rutgers-SOE rows")
    return records


class NJTransferClient:
    """Download the public NJ Transfer export with a local disk cache."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        http_client: httpx.Client | None = None,
        max_age_seconds: int = 86_400,
        detail_max_age_seconds: int = 900,
    ) -> None:
        configured_cache = os.environ.get("NJ_TRANSFER_CACHE_DIR")
        self.cache_dir = cache_dir or (
            Path(configured_cache)
            if configured_cache
            else user_cache_path("nj-transfer-mcp", ensure_exists=True)
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_age_seconds = max_age_seconds
        self.detail_max_age_seconds = detail_max_age_seconds
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=True,
            headers={
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip",
                "User-Agent": "nj-transfer-mcp/0.1 (+local read-only academic lookup)",
            },
        )
        self._owns_http_client = http_client is None
        self._art_id: str | None = None
        self._detail_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

    def close(self) -> None:
        if self._owns_http_client:
            self.http.close()

    @contextlib.contextmanager
    def _cache_lock(self):
        lock = FileLock(str(self.cache_dir / ".middlesex-rutgers-soe-active.lock"))
        try:
            lock.acquire()
        except OSError:
            yield
            return
        try:
            yield
        finally:
            lock.release()

    def get_equivalencies(self) -> tuple[list[Equivalency], CacheInfo]:
        data_path = self.cache_dir / "middlesex-rutgers-soe-active.txt"
        meta_path = self.cache_dir / "middlesex-rutgers-soe-active.meta.json"

        with self._cache_lock():
            meta = self._read_meta(meta_path)
            if meta.get("url") and meta["url"] != NJ_TRANSFER_LOOKUP:
                meta = {**meta, "url": NJ_TRANSFER_LOOKUP}
                with contextlib.suppress(OSError):
                    self._atomic_write(meta_path, json.dumps(meta).encode())
            cached_records: list[Equivalency] | None = None
            if data_path.exists():
                try:
                    cached_records = parse_export(data_path.read_text(encoding="utf-8"))
                except (NJTransferError, OSError, UnicodeError):
                    cached_records = None

            if cached_records is not None:
                age = datetime.now(tz=UTC).timestamp() - data_path.stat().st_mtime
                if age <= self.max_age_seconds:
                    return cached_records, CacheInfo(
                        source="cache",
                        fetched_at=meta.get(
                            "fetched_at", _iso_timestamp(data_path.stat().st_mtime)
                        ),
                        stale=False,
                    )

            try:
                export_text, _source_url = self._download_export()
                records = parse_export(export_text)
            except (httpx.HTTPError, NJTransferError, OSError, UnicodeError) as exc:
                if cached_records is not None:
                    return cached_records, CacheInfo(
                        source="stale_cache",
                        fetched_at=meta.get(
                            "fetched_at", _iso_timestamp(data_path.stat().st_mtime)
                        ),
                        stale=True,
                    )
                if isinstance(exc, NJTransferError):
                    raise NJTransferError(sanitize_error(exc)) from None
                raise NJTransferError(
                    f"Could not fetch NJ Transfer equivalencies: {sanitize_error(exc)}"
                ) from None

            now = datetime.now(tz=UTC)
            self._atomic_write(data_path, export_text.encode("utf-8"))
            self._atomic_write(
                meta_path,
                json.dumps({"fetched_at": now.isoformat(), "url": NJ_TRANSFER_LOOKUP}).encode(),
            )
            self._detail_cache.clear()
            return records, CacheInfo(source="network", fetched_at=now.isoformat(), stale=False)

    def get_equivalency_details(self, record: Equivalency) -> dict[str, Any]:
        """Fetch minimum-grade, footnote text, and linked-course requirements."""
        key = record.detail_key
        cached = self._detail_cache.get(key)
        if cached and monotonic() - cached[0] < self.detail_max_age_seconds:
            result = cached[1]
            return {**result, "data": {**result["data"], "source": "cache"}}

        sending_term = _term_query_label(record.middlesex_effective_term)
        receiving_term = _term_query_label(record.rutgers_evaluation_effective_term)
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                art_id = self._art_id or self._start_session()
                self._art_id = art_id
                detail_url = (
                    f"{NJ_TRANSFER_BASE}/artweb/crs-srch.cgi?Z~{art_id}~{SENDING_CODE}~"
                    f"{RECEIVING_CODE}~{record.middlesex_course}~{sending_term}~{receiving_term}~"
                )
                response = self.http.get(detail_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                self._art_id = None
                if attempt == 0:
                    continue
                raise NJTransferError(
                    f"Could not fetch details for {record.middlesex_course}: {sanitize_error(exc)}"
                ) from None
            if record.middlesex_course in response.text:
                break
            self._art_id = None
            if attempt == 1:
                raise NJTransferError(
                    f"NJ Transfer did not return details for {record.middlesex_course}"
                )

        assert response is not None
        grade_match = _MINIMUM_GRADE_RE.search(response.text)
        footnote_details = [
            {"code": _clean_html(code), "text": _clean_html(description)}
            for code, description in _FOOTNOTE_DETAIL_RE.findall(response.text)
        ]
        link_match = _LINK_DETAIL_RE.search(response.text)
        result = {
            "minimum_grade": _clean_html(grade_match.group(1)) if grade_match else None,
            "footnote_details": footnote_details,
            "linked_course_requirement": _clean_html(link_match.group(1)) if link_match else None,
            "data": CacheInfo("network", datetime.now(tz=UTC).isoformat(), False).as_dict(),
        }
        self._detail_cache[key] = (monotonic(), result)
        return result

    def _download_export(self) -> tuple[str, str]:
        self._art_id = self._start_session()

        listing = self.http.post(
            f"{NJ_TRANSFER_BASE}/artweb/listeqs.cgi?{SENDING_CODE}+{RECEIVING_CODE}",
            data={"Active": "Y", "doSearch": "Search"},
        )
        listing.raise_for_status()
        download_match = _DOWNLOAD_RE.search(listing.text)
        if not download_match:
            raise NJTransferError("NJ Transfer did not provide its equivalency export")

        download_url = download_match.group(1)
        parsed_url = urlparse(download_url)
        if parsed_url.hostname not in {"njtransfer.org", "www.njtransfer.org"}:
            raise NJTransferError("NJ Transfer returned an unsafe export URL")
        exported = self.http.get(download_url)
        exported.raise_for_status()
        return exported.text, NJ_TRANSFER_LOOKUP

    def _start_session(self) -> str:
        entry = self.http.get(NJ_TRANSFER_LOOKUP)
        entry.raise_for_status()
        art_match = _ART_ID_RE.search(entry.text)
        if not art_match:
            selected_id = _HIDDEN_ART_ID_RE.search(entry.text)
            if selected_id and SENDING_NAME in entry.text and RECEIVING_NAME in entry.text:
                return selected_id.group(1)
            raise NJTransferError("NJ Transfer did not start an equivalency lookup session")
        art_id = art_match.group(1)
        self.http.cookies.set("ArtId", art_id, domain="www.njtransfer.org", path="/")

        selected = self.http.post(
            f"{NJ_TRANSFER_LOOKUP}?{art_id}",
            data={
                "SIInst": SENDING_NAME,
                "RIInst": RECEIVING_NAME,
                "SubChgRI": "Submit",
                "Flipto": f"precs.cgi?{art_id}",
                "ArtId": art_id,
                "FromWhere": "precs.cgi",
                "Arg": f"?{art_id}",
                "SI": SENDING_CODE,
                "RI": RECEIVING_CODE,
            },
        )
        selected.raise_for_status()
        if SENDING_NAME not in selected.text or RECEIVING_NAME not in selected.text:
            raise NJTransferError("NJ Transfer did not accept the institution selection")
        selected_id = _HIDDEN_ART_ID_RE.search(selected.text)
        if selected_id:
            art_id = selected_id.group(1)
            self.http.cookies.set("ArtId", art_id, domain="www.njtransfer.org", path="/")
        return art_id

    @staticmethod
    def _read_meta(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_bytes())
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeError):
            return {}

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _format_rutgers_digits(value: str) -> str:
    return f"{value[:2]}:{value[2:5]}:{value[5:]}"


def _comma_values(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _term_number(value: str) -> int:
    return int(value) if value.isdigit() else 0


def _term_query_label(value: str) -> str:
    raw = value.strip()
    if len(raw) == 6 and raw.isdigit():
        term = _TERM_MONTHS.get(raw[4:], raw[4:])
        return f"{term.upper()}*{raw[:4]}"
    return raw.replace(" ", "*").upper()


def _credit_range(minimum: str, maximum: str) -> dict[str, str | None]:
    return {"minimum": minimum or None, "maximum": maximum or None}


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _clean_html(value: str) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(value))).strip()
