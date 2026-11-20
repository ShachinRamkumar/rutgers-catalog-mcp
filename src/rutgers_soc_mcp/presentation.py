"""Convert Rutgers SOC payloads into compact MCP-friendly structured results."""

from __future__ import annotations

import re
from typing import Any

from rutgers_soc_mcp.client import CacheInfo, Term, clean_text

_DAY_NAMES = {
    "M": "Monday",
    "T": "Tuesday",
    "W": "Wednesday",
    "H": "Thursday",
    "TH": "Thursday",
    "R": "Thursday",
    "F": "Friday",
    "S": "Saturday",
    "U": "Sunday",
}


def _person_names(instructors: Any) -> list[str]:
    if not isinstance(instructors, list):
        return []
    return [clean_text(item.get("name")) for item in instructors if clean_text(item.get("name"))]


def _location_names(locations: Any) -> list[str]:
    if not isinstance(locations, list):
        return []
    return [
        clean_text(item.get("description"))
        for item in locations
        if clean_text(item.get("description"))
    ]


def _format_clock(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in {3, 4}:
        return None
    digits = digits.zfill(4)
    hour = int(digits[:2])
    minute = int(digits[2:])
    if hour > 23 or minute > 59:
        return None
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _meeting(meeting: dict[str, Any]) -> dict[str, Any]:
    building = clean_text(meeting.get("buildingCode"))
    room = clean_text(meeting.get("roomNumber"))
    return {
        "day": _DAY_NAMES.get(
            str(meeting.get("meetingDay", "")).upper(), meeting.get("meetingDay")
        ),
        "start": _format_clock(meeting.get("startTimeMilitary") or meeting.get("startTime")),
        "end": _format_clock(meeting.get("endTimeMilitary") or meeting.get("endTime")),
        "mode": clean_text(meeting.get("meetingModeDesc")),
        "campus": clean_text(meeting.get("campusName") or meeting.get("campusAbbrev")),
        "building": building or None,
        "room": room or None,
        "location": " ".join(part for part in (building, room) if part) or None,
    }


def section_result(section: dict[str, Any], open_indexes: set[str] | None = None) -> dict[str, Any]:
    index = str(section.get("index", ""))
    is_open = index in open_indexes if open_indexes is not None else bool(section.get("openStatus"))
    return {
        "section": str(section.get("number", "")),
        "index": index,
        "open": is_open,
        "instructors": _person_names(section.get("instructors")),
        "meeting_times": [
            _meeting(item) for item in (section.get("meetingTimes") or []) if isinstance(item, dict)
        ],
        "campus_locations": _location_names(section.get("sectionCampusLocations")),
        "restrictions": clean_text(section.get("openToText")),
        "eligibility": clean_text(section.get("sectionEligibility")),
        "notes": clean_text(section.get("sectionNotes")),
        "comments": clean_text(section.get("commentsText")),
        "final_exam": clean_text(section.get("finalExam")),
    }


def _course_notes(course: dict[str, Any]) -> list[str]:
    values = (
        course.get("courseNotes"),
        course.get("subjectNotes"),
        course.get("subjectGroupNotes"),
        course.get("unitNotes"),
    )
    notes: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if cleaned and cleaned not in notes:
            notes.append(cleaned)
    return notes


def _course_url(course_code: str, term: Term, campus: str) -> str:
    # The fragment opens the official SOC with the term/campus selected. The SOC
    # application performs course filtering client-side, so a stable per-course
    # deep link is not currently exposed.
    return f"https://classes.rutgers.edu/soc/#keyword?keyword={course_code}&semester={term.code}{term.year}&campus={campus}"


def course_result(
    course: dict[str, Any],
    *,
    term: Term,
    campus: str,
    cache_info: CacheInfo,
    include_sections: bool,
    open_indexes: set[str] | None = None,
    availability_info: CacheInfo | None = None,
    section_limit: int = 25,
    section_offset: int = 0,
) -> dict[str, Any]:
    raw_sections = [item for item in (course.get("sections") or []) if isinstance(item, dict)]
    sections = [section_result(item, open_indexes) for item in raw_sections]
    open_count = sum(1 for item in sections if item["open"])
    course_code = str(course.get("courseString", ""))
    result: dict[str, Any] = {
        "course_code": course_code,
        "title": clean_text(course.get("expandedTitle") or course.get("title")),
        "short_title": clean_text(course.get("title")),
        "supplement": clean_text(course.get("supplementCode")),
        "core_codes": course.get("coreCodes") or [],
        "credits": course.get("credits"),
        "level": course.get("level"),
        "school": course.get("school"),
        "subject": {
            "code": course.get("subject"),
            "description": clean_text(course.get("subjectDescription")),
        },
        "description": clean_text(course.get("courseDescription")),
        "prerequisites": clean_text(course.get("preReqNotes")),
        "notes": _course_notes(course),
        "campus_locations": _location_names(course.get("campusLocations")),
        "synopsis_url": course.get("synopsisUrl") or None,
        "official_soc_url": _course_url(course_code, term, campus),
        "term": term.as_dict(campus),
        "section_summary": {
            "total": len(sections),
            "open": open_count,
            "availability_is_live_overlay": (
                availability_info is not None and not availability_info.stale
            ),
        },
        "data": {
            **cache_info.as_dict(),
            "stale": cache_info.stale or bool(availability_info and availability_info.stale),
        },
        "course_data": cache_info.as_dict(),
        "availability_data": availability_info.as_dict() if availability_info else None,
        "advisory": (
            "Schedule data can change. Prerequisite notes and section restrictions are advisory; "
            "Rutgers determines registration eligibility."
        ),
    }
    if include_sections:
        end = section_offset + section_limit
        result["sections"] = sections[section_offset:end]
        result["section_offset"] = section_offset
        result["sections_truncated"] = end < len(sections)
        result["next_section_offset"] = end if end < len(sections) else None
    return result


def search_result(course: dict[str, Any], *, term: Term, campus: str) -> dict[str, Any]:
    sections = [item for item in (course.get("sections") or []) if isinstance(item, dict)]
    return {
        "course_code": course.get("courseString"),
        "title": clean_text(course.get("expandedTitle") or course.get("title")),
        "supplement": clean_text(course.get("supplementCode")),
        "core_codes": course.get("coreCodes") or [],
        "credits": course.get("credits"),
        "school": course.get("school"),
        "subject": clean_text(course.get("subjectDescription")),
        "prerequisites": clean_text(course.get("preReqNotes")),
        "description": clean_text(course.get("courseDescription"))[:300],
        "section_count": len(sections),
        "official_soc_url": _course_url(str(course.get("courseString", "")), term, campus),
    }
