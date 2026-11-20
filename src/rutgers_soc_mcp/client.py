"""Client and normalization helpers for Rutgers Schedule of Classes data."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from filelock import FileLock
from platformdirs import user_cache_path

SOC_API_BASE = "https://classes.rutgers.edu/soc/api"
SOC_HOME = "https://classes.rutgers.edu/soc/"

TERM_CODES = {
    "winter": 0,
    "spring": 1,
    "summer": 7,
    "fall": 9,
}
TERM_ALIASES = {
    "0": "winter",
    "1": "spring",
    "7": "summer",
    "9": "fall",
    "autumn": "fall",
}

SEMESTER_RE = re.compile(r"^[0179]20[0-9]{2}$")
_SEMESTER_LABEL_RE = re.compile(r"(winter|spring|summer|fall)\s+(20[0-9]{2})", re.IGNORECASE)
SEMESTER_HELP = (
    "Semester label, such as 'Spring 2027' (Winter, Spring, Summer, or Fall "
    "plus a four-digit year), or its code such as 12027. Rutgers names Winter Session after "
    "the year it ends in: the session from late December 2026 is Winter 2027 (02027)."
)

CAMPUSES = {
    "NB": "New Brunswick",
    "NK": "Newark",
    "CM": "Camden",
    "ONLINE_NB": "Online (New Brunswick)",
    "ONLINE_NK": "Online (Newark)",
    "ONLINE_CM": "Online (Camden)",
    "B": "Burlington County Community College - Mt Laurel",
    "CC": "Camden County College - Blackwood",
    "H": "County College of Morris",
    "CU": "Cumberland County College",
    "MC": "Mercer County Community College",
    "L": "Lincroft / Rutgers-BCC",
    "AC": "Mays Landing / Rutgers-ACCC",
    "J": "McGuire-Dix-Lakehurst / Rutgers-JBMDL",
    "D": "Denville / Rutgers-Morris",
}

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_COURSE_CODE_RE = re.compile(r"^(\d{1,2})\D+(\d{1,3})\D+(\d{1,3})$")
_SUBJECT_COURSE_RE = re.compile(r"^(\d{1,3})\D+(\d{1,3})$")


class SocApiError(RuntimeError):
    """Raised when Rutgers SOC data cannot be fetched or decoded."""


@dataclass(frozen=True)
class Term:
    year: int
    name: str

    @property
    def code(self) -> int:
        return TERM_CODES[self.name]

    def as_dict(self, campus: str) -> dict[str, Any]:
        return {
            "year": self.year,
            "term": self.name.title(),
            "term_code": self.code,
            "semester": f"{self.code}{self.year}",
            "semester_label": f"{self.name.title()} {self.year}",
            "campus": campus,
            "campus_name": CAMPUSES[campus],
        }


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


def parse_semester(value: object) -> str:
    """Parse 'Spring 2027' or '12027' into '12027'. Raises ValueError otherwise."""
    if isinstance(value, str):
        text = " ".join(value.split())
        if SEMESTER_RE.fullmatch(text):
            return text
        match = _SEMESTER_LABEL_RE.fullmatch(text)
        if match:
            season, year = match[1].lower(), match[2]
            code = f"{TERM_CODES[season]}{year}"
            if SEMESTER_RE.fullmatch(code):
                return code
    raise ValueError(SEMESTER_HELP)


def current_term(now: datetime | None = None) -> Term:
    """Return a practical default SOC term for the current New Jersey date."""
    if now is None:
        now = datetime.now(ZoneInfo("America/New_York"))

    if now.month <= 5:
        name = "spring"
    elif now.month <= 7:
        name = "summer"
    else:
        name = "fall"
    return Term(year=now.year, name=name)


def resolve_term(year: int | None = None, term: str | int | None = None) -> Term:
    """Resolve optional user input to a validated Rutgers SOC term."""
    default = current_term()
    resolved_year = default.year if year is None else year
    if resolved_year < 2000 or resolved_year > 2100:
        raise ValueError("year must be between 2000 and 2100")

    if term is None:
        name = default.name
    else:
        raw = str(term).strip().lower()
        name = TERM_ALIASES.get(raw, raw)
        if name not in TERM_CODES:
            allowed = ", ".join(TERM_CODES)
            raise ValueError(f"term must be one of: {allowed}")
    return Term(year=resolved_year, name=name)


def resolve_semester(semester: str | None = None) -> Term:
    """'Spring 2027' or '12027' -> Term; None -> the current term. Raises ValueError."""
    if semester is None:
        return current_term()
    code = parse_semester(semester)
    return Term(year=int(code[1:]), name=TERM_ALIASES[code[0]])


def resolve_campus(campus: str) -> str:
    code = campus.strip().upper()
    if code not in CAMPUSES:
        allowed = ", ".join(CAMPUSES)
        raise ValueError(f"campus must be one of: {allowed}")
    return code


def normalize_course_code(value: str) -> str | None:
    """Normalize a full Rutgers course code, returning None for a text query."""
    raw = value.strip()
    match = _COURSE_CODE_RE.fullmatch(raw)
    if match:
        school, subject, course = match.groups()
        return f"{int(school):02d}:{int(subject):03d}:{int(course):03d}"

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        return f"{digits[:2]}:{digits[2:5]}:{digits[5:]}"
    return None


def normalize_subject_course(value: str) -> str | None:
    """Normalize a school-less subject/course pair such as 332:331."""
    raw = value.strip()
    if _COURSE_CODE_RE.fullmatch(raw):
        return None
    match = _SUBJECT_COURSE_RE.fullmatch(raw)
    if not match:
        return None
    subject, course = match.groups()
    return f"{int(subject):03d}:{int(course):03d}"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/?\s*em\s*>", " ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _parse_max_age(cache_control: str | None, fallback: int) -> int:
    if cache_control:
        match = re.search(r"(?:^|,)\s*max-age=(\d+)", cache_control, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return fallback


def _has_no_store(cache_control: str | None) -> bool:
    if not cache_control:
        return False
    return bool(re.search(r"(?:^|,)\s*no-store(?:\s*,|$)", cache_control, re.IGNORECASE))


class SocClient:
    """Fetch Rutgers SOC JSON with conditional requests and a local disk cache."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        configured_cache = os.environ.get("RUTGERS_SOC_CACHE_DIR")
        self.cache_dir = cache_dir or (
            Path(configured_cache)
            if configured_cache
            else user_cache_path("rutgers-soc-mcp", ensure_exists=True)
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "rutgers-soc-mcp/0.1 (+local read-only course lookup)",
            },
        )
        self._owns_http_client = http_client is None

    def close(self) -> None:
        if self._owns_http_client:
            self.http.close()

    def get_courses(self, term: Term, campus: str) -> tuple[list[dict[str, Any]], CacheInfo]:
        data, info = self._get_json(
            endpoint="courses.json",
            term=term,
            campus=campus,
            fallback_max_age=900,
        )
        return data, info

    def get_open_sections(self, term: Term, campus: str) -> tuple[set[str], CacheInfo]:
        data, info = self._get_json(
            endpoint="openSections.json",
            term=term,
            campus=campus,
            fallback_max_age=30,
        )
        return {str(index) for index in data}, info

    @staticmethod
    def _valid_dict_list(value: Any) -> bool:
        return value is None or (
            isinstance(value, list) and all(isinstance(entry, dict) for entry in value)
        )

    @classmethod
    def _valid_section(cls, section: Any) -> bool:
        return (
            isinstance(section, dict)
            and cls._valid_dict_list(section.get("instructors"))
            and cls._valid_dict_list(section.get("meetingTimes"))
            and cls._valid_dict_list(section.get("sectionCampusLocations"))
        )

    @classmethod
    def _validate_payload(cls, endpoint: str, payload: Any) -> None:
        valid = isinstance(payload, list)
        if valid and endpoint == "courses.json":
            valid = all(
                isinstance(item, dict)
                and isinstance(item.get("courseString"), str)
                and bool(item["courseString"])
                and cls._valid_dict_list(item.get("campusLocations"))
                and (
                    "coreCodes" not in item
                    or item["coreCodes"] is None
                    or isinstance(item["coreCodes"], list)
                )
                and (
                    "sections" not in item
                    or (
                        isinstance(item["sections"], list)
                        and all(cls._valid_section(sec) for sec in item["sections"])
                    )
                )
                for item in payload
            )
        elif valid:
            valid = all(
                (isinstance(item, int) and not isinstance(item, bool))
                or (isinstance(item, str) and item.isdigit())
                for item in payload
            )
        if not valid:
            raise SocApiError(f"Rutgers SOC returned an unexpected {endpoint} payload")

    def _read_bundle(self, endpoint: str, bundle_path: Path) -> tuple[Any, dict[str, Any]] | None:
        if not bundle_path.exists():
            return None
        try:
            raw = self._read_json(bundle_path)
            if not isinstance(raw, dict) or "payload" not in raw:
                return None
            payload = raw["payload"]
            meta = raw.get("meta")
            self._validate_payload(endpoint, payload)
            return payload, meta if isinstance(meta, dict) else {}
        except SocApiError:
            return None

    def _write_bundle(self, bundle_path: Path, payload: Any, meta: dict[str, Any]) -> None:
        serialized = json.dumps({"meta": meta, "payload": payload}, separators=(",", ":")).encode()
        self._atomic_write(bundle_path, serialized)

    def _get_json(
        self,
        *,
        endpoint: str,
        term: Term,
        campus: str,
        fallback_max_age: int,
    ) -> tuple[Any, CacheInfo]:
        stem = f"{endpoint.removesuffix('.json')}-{term.year}-{term.code}-{campus}"
        bundle_path = self.cache_dir / f"{stem}.cache.json"
        lock = FileLock(str(self.cache_dir / f".{stem}.lock"))

        with lock:
            cached_bundle = self._read_bundle(endpoint, bundle_path)
            cached = cached_bundle[0] if cached_bundle is not None else None
            meta = cached_bundle[1] if cached_bundle is not None else {}

            max_age = fallback_max_age
            if cached is not None:
                age = datetime.now(tz=UTC).timestamp() - bundle_path.stat().st_mtime
                try:
                    max_age = int(meta.get("max_age", fallback_max_age))
                except (TypeError, ValueError):
                    max_age = fallback_max_age
                if age <= max_age:
                    return cached, CacheInfo(
                        source="cache",
                        fetched_at=meta.get(
                            "fetched_at", _iso_timestamp(bundle_path.stat().st_mtime)
                        ),
                        stale=False,
                    )

            headers: dict[str, str] = {}
            if cached is not None and meta.get("etag"):
                headers["If-None-Match"] = str(meta["etag"])

            url = f"{SOC_API_BASE}/{endpoint}"
            params = {"year": term.year, "term": term.code, "campus": campus}
            try:
                response = self.http.get(url, params=params, headers=headers)
                if response.status_code == 304 and cached is not None:
                    now = datetime.now(tz=UTC)
                    cache_control = response.headers.get("cache-control")
                    if _has_no_store(cache_control):
                        bundle_path.unlink(missing_ok=True)
                    else:
                        updated_meta = {
                            **meta,
                            "etag": response.headers.get("etag") or meta.get("etag"),
                            "fetched_at": now.isoformat(),
                            "max_age": _parse_max_age(cache_control, max_age),
                        }
                        self._write_bundle(bundle_path, cached, updated_meta)
                    return cached, CacheInfo(
                        source="validated_cache", fetched_at=now.isoformat(), stale=False
                    )
                response.raise_for_status()
                payload = response.json()
                self._validate_payload(endpoint, payload)
            except (httpx.HTTPError, ValueError, SocApiError) as exc:
                if cached is not None:
                    return cached, CacheInfo(
                        source="stale_cache",
                        fetched_at=meta.get(
                            "fetched_at", _iso_timestamp(bundle_path.stat().st_mtime)
                        ),
                        stale=True,
                    )
                raise SocApiError(f"Could not fetch Rutgers SOC data: {exc}") from exc

            now = datetime.now(tz=UTC)
            cache_control = response.headers.get("cache-control")
            if _has_no_store(cache_control):
                bundle_path.unlink(missing_ok=True)
            else:
                new_meta = {
                    "etag": response.headers.get("etag"),
                    "fetched_at": now.isoformat(),
                    "max_age": _parse_max_age(cache_control, fallback_max_age),
                    "url": str(response.url),
                }
                self._write_bundle(bundle_path, payload, new_meta)
            return payload, CacheInfo(source="network", fetched_at=now.isoformat(), stale=False)

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise SocApiError(f"Could not read cached SOC data at {path}: {exc}") from exc

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
