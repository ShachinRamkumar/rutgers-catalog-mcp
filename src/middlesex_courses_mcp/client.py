"""Client and normalization helpers for the Middlesex College course catalog."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://middlesexcollege-ss.colleague.elluciancloud.com"
CATALOG_URL = f"{BASE_URL}/Student/Courses"
OPTIONS_URL = f"{CATALOG_URL}/GetCatalogAdvancedSearch"
SEARCH_URL = f"{CATALOG_URL}/PostSearchCriteria"
SECTION_DETAILS_URL = f"{CATALOG_URL}/SectionDetails"

_TOKEN_RE = re.compile(
    r'<input[^>]+name=["\']__RequestVerificationToken["\'][^>]+value=["\']([^"\']+)',
    re.IGNORECASE,
)
_SECTION_CODE_RE = re.compile(
    r"^\s*([A-Za-z]{2,5})[\s_-]*(\d{1,7})(?:[\s_-]+([A-Za-z0-9]{1,7}))?\s*$"
)
_SPACE_RE = re.compile(r"\s+")

DAY_CODES = {
    "sunday": "0",
    "sun": "0",
    "monday": "1",
    "mon": "1",
    "tuesday": "2",
    "tue": "2",
    "tues": "2",
    "wednesday": "3",
    "wed": "3",
    "thursday": "4",
    "thu": "4",
    "thur": "4",
    "thurs": "4",
    "friday": "5",
    "fri": "5",
    "saturday": "6",
    "sat": "6",
}


class MiddlesexCatalogError(RuntimeError):
    """Raised when Middlesex's guest catalog cannot be queried or decoded."""


def normalize_section_code(value: str) -> tuple[str, str, str | None] | None:
    """Parse CSC-105-01 or CSC 105 into a subject, course, and optional section."""
    match = _SECTION_CODE_RE.fullmatch(value)
    if not match:
        return None
    subject, course, section = match.groups()
    return subject.upper(), course.upper(), section.upper() if section else None


def _compact(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _option_pair(item: dict[str, Any]) -> tuple[str, str]:
    code = item.get("Code", item.get("Item1", ""))
    label = item.get("Description", item.get("Item2", ""))
    return str(code), str(label)


def resolve_option(value: str, options: list[dict[str, Any]], label: str) -> str:
    """Resolve an option code or human label, rejecting ambiguous partial labels."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be empty")
    compact = _compact(cleaned)
    exact = [
        code
        for item in options
        for code, name in [_option_pair(item)]
        if compact in {_compact(code), _compact(name)}
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        (code, name)
        for item in options
        for code, name in [_option_pair(item)]
        if compact and compact in _compact(name)
    ]
    if len(partial) == 1:
        return partial[0][0]
    if partial:
        choices = ", ".join(f"{code} ({name})" for code, name in partial[:10])
        raise ValueError(f"{label} {value!r} is ambiguous; use one of: {choices}")
    choices = ", ".join(f"{code} ({name})" for code, name in map(_option_pair, options))
    raise ValueError(f"unknown {label} {value!r}; available values: {choices}")


def resolve_term(
    value: str | None,
    terms: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Resolve a term code/label or choose the current New Jersey academic season."""
    if not terms:
        raise ValueError("the catalog did not publish any searchable terms")
    if value:
        code = resolve_option(value, terms, "term")
        label = next(
            name for item in terms for item_code, name in [_option_pair(item)] if item_code == code
        )
        return code, label

    current = now or datetime.now(ZoneInfo("America/New_York"))
    if current.month <= 5:
        season = "spring"
    elif current.month == 6:
        season = "summer i"
    elif current.month <= 8:
        season = "summer ii"
    else:
        season = "fall"
    desired = _compact(f"{season} {current.year}")
    for item in terms:
        code, label = _option_pair(item)
        if _compact(label) == desired:
            return code, label

    # The endpoint is already ordered chronologically; its last entry is the
    # safest fallback when the current season is not among the published terms.
    return _option_pair(terms[-1])


def resolve_days(values: list[str] | None) -> list[str]:
    resolved: list[str] = []
    for value in values or []:
        code = DAY_CODES.get(value.strip().casefold())
        if code is None:
            raise ValueError(f"unknown day {value!r}; use Monday-Sunday names")
        if code not in resolved:
            resolved.append(code)
    return resolved


def _credits(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "minimum": section.get("MinimumCredits"),
        "maximum": section.get("MaximumCredits"),
        "variable_increment": section.get("VariableCreditIncrement"),
        "ceus": section.get("Ceus"),
        "display": section.get("CreditsDisplay"),
    }


def _meeting(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "days": item.get("Days", []),
        "days_display": item.get("DaysOfWeekDisplay", ""),
        "start_time": item.get("StartTime"),
        "end_time": item.get("EndTime"),
        "start_time_display": item.get("StartTimeDisplay", ""),
        "end_time_display": item.get("EndTimeDisplay", ""),
        "start_date": item.get("StartDate"),
        "end_date": item.get("EndDate"),
        "dates_display": item.get("DatesDisplay", ""),
        "instructional_method": item.get("InstructionalMethodDisplay", ""),
        "instructional_method_code": item.get("InstructionalMethodCode", ""),
        "building": item.get("BuildingDisplay", ""),
        "room": item.get("RoomDisplay", ""),
        "online": bool(item.get("IsOnline")),
    }


def normalize_section(section: dict[str, Any]) -> dict[str, Any]:
    """Return the useful, stable subset of an Ellucian section result."""
    course = section.get("Course") or {}
    term = section.get("Term") or {}
    meetings = section.get("FormattedMeetingTimes") or []
    return {
        "section_id": str(section.get("Id", "")),
        "section_code": section.get("SectionNameDisplay") or section.get("FullTitleDisplay"),
        "course_code": section.get("CourseName"),
        "subject": course.get("SubjectCode"),
        "course_number": course.get("Number"),
        "section_number": section.get("Number"),
        "synonym": section.get("Synonym"),
        "title": section.get("SectionTitleDisplay") or section.get("Title"),
        "description": section.get("CourseDescription") or course.get("Description"),
        "term": {
            "code": section.get("TermId") or term.get("Code"),
            "name": section.get("TermDisplay") or term.get("Description"),
            "start_date": term.get("StartDate"),
            "end_date": term.get("EndDate"),
        },
        "status": section.get("AvailabilityStatusDisplay") or section.get("AvailabilityStatus"),
        "seats": {
            "available": section.get("Available"),
            "capacity": section.get("Capacity"),
            "enrolled": section.get("Enrolled"),
            "waitlisted": section.get("Waitlisted"),
            "waitlist_available": section.get("WaitlistAvailable"),
            "counts_available": section.get("AreSeatCountsAvailable"),
            "display": section.get("AvailabilityDisplay"),
        },
        "credits": _credits(section),
        "location": {
            "code": section.get("LocationCode") or section.get("Location"),
            "name": section.get("LocationDisplay"),
        },
        "meetings": [_meeting(item) for item in meetings],
        "instructors": list(section.get("FacultyDisplay") or []),
        "academic_level": section.get("AcademicLevel"),
        "course_types": list(section.get("CourseTypesDisplay") or []),
        "instructional_methods": list(section.get("InstructionalMethodsDisplay") or []),
        "comments": section.get("Comments") or "",
        "additional_fees_apply": bool(section.get("AdditionalFeesApply")),
        "bookstore_url": section.get("BookstoreUrl"),
    }


def normalize_details(details: dict[str, Any]) -> dict[str, Any]:
    """Normalize the section-details response, including requisites and charges."""

    def requisite(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": _SPACE_RE.sub(" ", str(item.get("DisplayText", ""))).strip(),
            "timing": _SPACE_RE.sub(" ", str(item.get("DisplayTextExtension", ""))).strip(),
            "required": bool(item.get("IsRequired")),
        }

    return {
        "section_id": str(details.get("Id", "")),
        "section_code": "-".join(
            part for part in (details.get("CourseName"), details.get("Number")) if part
        ),
        "course_code": details.get("CourseName"),
        "title": details.get("Title"),
        "term": {"code": details.get("TermId"), "name": details.get("TermDisplay")},
        "dates": details.get("DatesDisplay"),
        "status": details.get("AvailabilityStatus"),
        "seats": {
            "available": details.get("Available"),
            "capacity": details.get("Capacity"),
            "enrolled": details.get("Enrolled"),
            "waitlisted": details.get("Waitlisted"),
            "waitlist_maximum": details.get("WaitlistMaximum"),
            "waitlist_available": details.get("WaitlistAvailable"),
            "counts_available": details.get("AreSeatCountsAvailable"),
            "display": details.get("AvailabilityDisplay"),
        },
        "credits": _credits(details),
        "requisites": [requisite(item) for item in details.get("RequisiteItems") or []],
        "corequisites": [requisite(item) for item in details.get("CorequisiteItems") or []],
        "meeting_locations": [
            {"time": item.get("Time"), "location": item.get("Location"), "dates": item.get("Dates")}
            for item in details.get("TimeLocationItems") or []
        ],
        "instructors": [
            {
                "name": item.get("Name"),
                "emails": list(item.get("EmailAddresses") or []),
                "phones": list(item.get("PhoneNumbers") or []),
            }
            for item in details.get("InstructorItems") or []
        ],
        "charges": [
            {"description": item.get("Description"), "amount": item.get("DisplayAmount")}
            for item in details.get("Charges") or []
        ],
        "comments": details.get("Comments") or "",
        "academic_level_code": details.get("AcademicLevelCode"),
        "learning_provider": details.get("LearningProvider"),
        "bookstore_url": details.get("BookstoreUrl"),
        "grading": {
            "audit_allowed": details.get("AllowAudit"),
            "pass_no_pass_allowed": details.get("AllowPassNoPass"),
            "pass_no_pass_only": details.get("OnlyPassNoPass"),
        },
    }


UPSTREAM_PAGE_SIZE = 30


class MiddlesexCatalogClient:
    """Query Middlesex College's public Ellucian guest course catalog."""

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "middlesex-courses-mcp/0.1 (+read-only guest catalog lookup)",
            },
        )
        self._owns_http_client = http_client is None
        self._token: str | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self.http.close()

    def _bootstrap(self) -> None:
        try:
            response = self.http.get(CATALOG_URL)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MiddlesexCatalogError(
                f"could not open the Middlesex guest catalog: {exc}"
            ) from exc
        match = _TOKEN_RE.search(response.text)
        if not match:
            raise MiddlesexCatalogError(
                "the Middlesex guest catalog did not provide a request token"
            )
        self._token = match.group(1)

    def _request_headers(self) -> dict[str, str]:
        if not self._token:
            self._bootstrap()
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "__IsGuestUser": "true",
            "__RequestVerificationToken": str(self._token),
        }

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            try:
                response = self.http.post(url, headers=self._request_headers(), json=payload)
                if response.status_code in {400, 403} and attempt == 0:
                    self._token = None
                    continue
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise MiddlesexCatalogError(f"Middlesex catalog request failed: {exc}") from exc
            if not isinstance(data, dict):
                raise MiddlesexCatalogError("Middlesex returned an unexpected catalog response")
            return data
        raise MiddlesexCatalogError("Middlesex rejected the refreshed guest catalog session")

    def get_search_options(self) -> dict[str, Any]:
        if not self._token:
            self._bootstrap()
        try:
            response = self.http.get(OPTIONS_URL)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MiddlesexCatalogError(
                f"could not retrieve Middlesex search options: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise MiddlesexCatalogError("Middlesex returned unexpected search options")
        return data

    def search_classes(
        self,
        *,
        query: str = "",
        term: str | None = None,
        subject: str | None = None,
        course_number: str | None = None,
        section_number: str | None = None,
        locations: list[str] | None = None,
        days: list[str] | None = None,
        open_only: bool = False,
        online: bool | None = None,
        starts_at_time: str | None = None,
        ends_by_time: str | None = None,
        page: int = 1,
        limit: int = 10,
    ) -> dict[str, Any]:
        options = self.get_search_options()
        terms = list(options.get("Terms") or [])
        term_code, term_name = resolve_term(term, terms)

        parsed = normalize_section_code(query) if query else None
        if parsed and not subject and not course_number and not section_number:
            subject, course_number, section_number = parsed
            query = ""

        subject_code = None
        if subject:
            subject_code = resolve_option(subject, list(options.get("Subjects") or []), "subject")
        if course_number and not subject_code:
            raise ValueError("course_number requires a subject code or name")
        if section_number and not course_number:
            raise ValueError("section_number requires course_number and subject")
        if page < 1:
            raise ValueError("page must be at least 1")
        if limit < 1 or limit > 30:
            raise ValueError("limit must be between 1 and 30")

        location_codes = [
            resolve_option(value, list(options.get("Locations") or []), "location")
            for value in locations or []
        ]
        online_categories: list[str] = []
        if online is True:
            online_categories = ["Online"]
        elif online is False:
            online_categories = ["NotOnline"]

        keyword_components = []
        if subject_code and course_number:
            keyword_components.append(
                {
                    "synonym": "",
                    "subject": subject_code,
                    "courseNumber": str(course_number).strip(),
                    "section": str(section_number or "").strip(),
                }
            )

        start = (page - 1) * limit
        upstream_page = start // UPSTREAM_PAGE_SIZE + 1
        offset = start % UPSTREAM_PAGE_SIZE
        payload = {
            "keyword": query.strip(),
            "terms": [term_code],
            "requirement": None,
            "subrequirement": None,
            "courseIds": None,
            "sectionIds": None,
            "requirementText": None,
            "subrequirementText": "",
            "group": None,
            "startTime": None,
            "endTime": None,
            "openSections": True if open_only else None,
            "openAndWaitlistedSections": None,
            "subjects": [subject_code] if subject_code and not course_number else [],
            "academicLevels": [],
            "courseLevels": [],
            "synonyms": [],
            "courseTypes": [],
            "topicCodes": [],
            "days": resolve_days(days),
            "locations": location_codes,
            "faculty": [],
            "onlineCategories": online_categories,
            "keywordComponents": keyword_components,
            "startDate": None,
            "endDate": None,
            "startsAtTime": starts_at_time,
            "endsByTime": ends_by_time,
            "pageNumber": upstream_page,
            "sortOn": "SectionName",
            "sortDirection": "Ascending",
            "quantityPerPage": UPSTREAM_PAGE_SIZE,
            "searchResultsView": "SectionListing",
        }
        data = self._post_json(SEARCH_URL, payload)
        if data.get("ErrorMessage"):
            raise MiddlesexCatalogError(data["ErrorMessage"])
        raw_sections = list(data.get("Sections") or [])
        total_matches = data.get("TotalItems", len(raw_sections))
        if (
            offset + limit > UPSTREAM_PAGE_SIZE
            and upstream_page * UPSTREAM_PAGE_SIZE < total_matches
        ):
            following = self._post_json(SEARCH_URL, {**payload, "pageNumber": upstream_page + 1})
            if following.get("ErrorMessage"):
                raise MiddlesexCatalogError(following["ErrorMessage"])
            raw_sections.extend(following.get("Sections") or [])
        sections = (
            [normalize_section(item) for item in raw_sections[offset : offset + limit]]
            if start < total_matches
            else []
        )
        return {
            "query": query,
            "term": {"code": term_code, "name": term_name},
            "total_matches": total_matches,
            "page": page,
            "total_pages": (total_matches + limit - 1) // limit,
            "returned": len(sections),
            "classes": sections,
            "error_message": data.get("ErrorMessage") or "",
            "retrieved_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "official_source": CATALOG_URL,
            "advisory": (
                "Seat counts and status can change; confirm in the official catalog before "
                "registering."
            ),
        }

    def get_class_details(self, section_id: str) -> dict[str, Any]:
        cleaned = section_id.strip()
        if not cleaned or not cleaned.isdigit():
            raise ValueError("section_id must be the numeric ID returned by search_classes")
        data = self._post_json(SECTION_DETAILS_URL, {"sectionId": cleaned, "studentId": None})
        result = normalize_details(data)
        result["retrieved_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
        result["official_source"] = CATALOG_URL
        result["advisory"] = (
            "Requirements, fees, seat counts, and status can change; confirm in the official "
            "catalog."
        )
        return result
