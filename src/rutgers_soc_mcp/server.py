"""MCP tools for querying Rutgers' public Schedule of Classes data."""

from __future__ import annotations

import atexit
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from rutgers_soc_mcp.client import (
    CAMPUSES,
    SOC_HOME,
    SocApiError,
    SocClient,
    clean_text,
    normalize_course_code,
    normalize_subject_course,
    resolve_campus,
    resolve_semester,
)
from rutgers_soc_mcp.presentation import course_result, search_result
from rutgers_soc_mcp.tool_display import server_display, tool_display

INSTRUCTIONS = (
    "Read-only official Rutgers Schedule of Classes. SoC_Get_Course takes an exact course code "
    "(14:332:331, 14332331, or 332:331); SoC_Search_Courses takes a title, subject or partial "
    "code. Defaults: New Brunswick and the current term; for another term pass semester as "
    "the user says it, e.g. 'Spring 2027' (or its code 12027). Winter Session is named for the "
    "year it ends in: late December 2026 is Winter 2027. Each result's term names the semester. "
    "include_sections=true adds times, instructors, open status and each section's five-digit "
    "index; follow next_section_offset for more sections and review every supplement variant. "
    "Prerequisites, restrictions and open status are advisory: report the data timestamp. "
    "Related servers: NJTransfer_* tools map Middlesex College courses to Rutgers School of "
    "Engineering, and Middlesex_* tools search live Middlesex College sections."
)

mcp = MCPServer(
    "Rutgers Schedule of Classes",
    instructions=INSTRUCTIONS,
    **server_display("Rutgers Schedule of Classes"),
)
_client = SocClient()
atexit.register(_client.close)


def _inputs(semester: str | None, campus: str) -> tuple[Any, str]:
    try:
        resolved_term = resolve_semester(semester)
        resolved_campus = resolve_campus(campus)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return resolved_term, resolved_campus


def _courses(semester: str | None, campus: str):
    resolved_term, resolved_campus = _inputs(semester, campus)
    try:
        courses, cache_info = _client.get_courses(resolved_term, resolved_campus)
    except SocApiError as exc:
        raise ToolError(str(exc)) from exc
    return courses, cache_info, resolved_term, resolved_campus


@mcp.tool(**tool_display("SoC_Get_Course"))
def get_course(
    course_code: str,
    semester: str | None = None,
    campus: str = "NB",
    include_sections: bool = False,
    supplement: str | None = None,
    section_offset: int = 0,
    section_limit: int = 25,
) -> dict[str, Any]:
    """Get an exact Rutgers course and optionally its sections.

    Args:
        course_code: Rutgers code such as 14:332:331, 14332331, or 332:331.
        semester: Term as the user says it, e.g. Spring 2027 (season plus four-digit
            year), or its code such as 12027. Defaults to the current academic term.
        campus: Rutgers campus code, usually NB, NK, or CM. Defaults to NB.
        include_sections: Include section times, instructors, restrictions, and open status.
        supplement: Select a variant such as LB; use an empty string for the main record.
        section_offset: Zero-based section offset; follow next_section_offset for more.
        section_limit: Sections per variant to return, from 1 to 100.
    """
    if section_offset < 0:
        raise ToolError("section_offset must be at least 0")
    if not 1 <= section_limit <= 100:
        raise ToolError("section_limit must be between 1 and 100")
    normalized = normalize_course_code(course_code)
    suffix = normalize_subject_course(course_code)
    if not normalized and not suffix:
        raise ToolError(
            "course_code must look like 14:332:331, 14332331, or 332:331. "
            "Use SoC_Search_Courses for a title or keyword."
        )
    courses, cache_info, resolved_term, resolved_campus = _courses(semester, campus)
    if normalized:
        matches = [item for item in courses if item.get("courseString") == normalized]
    elif suffix:
        matches = [
            item for item in courses if str(item.get("courseString", "")).endswith(f":{suffix}")
        ]

    if not matches:
        term_label = f"{resolved_term.name.title()} {resolved_term.year}"
        raise ToolError(
            f"No course matching {course_code!r} was found for {term_label} at "
            f"{CAMPUSES[resolved_campus]}. Try another term/campus or use SoC_Search_Courses."
        )
    codes = sorted({str(item["courseString"]) for item in matches})
    if len(codes) > 1:
        choices = ", ".join(codes[:10])
        raise ToolError(f"{course_code!r} is ambiguous. Use a full course code: {choices}")
    if supplement is not None:
        matches = [
            item
            for item in matches
            if clean_text(item.get("supplementCode")).upper() == supplement.strip().upper()
        ]
        if not matches:
            raise ToolError(f"No supplement {supplement!r} was found for {codes[0]}")

    open_indexes = None
    availability_info = None
    if include_sections:
        try:
            open_indexes, availability_info = _client.get_open_sections(
                resolved_term, resolved_campus
            )
        except SocApiError:
            # The course payload itself contains openStatus; use it as a graceful
            # fallback if the short-lived overlay endpoint is unavailable.
            open_indexes = None

    variants = [
        course_result(
            item,
            term=resolved_term,
            campus=resolved_campus,
            cache_info=cache_info,
            include_sections=include_sections,
            open_indexes=open_indexes,
            availability_info=availability_info,
            section_offset=section_offset,
            section_limit=section_limit,
        )
        for item in matches
    ]
    if len(variants) == 1:
        return variants[0]
    return {
        "course_code": codes[0],
        "term": resolved_term.as_dict(resolved_campus),
        "variant_count": len(variants),
        "variants": variants,
        "interpretation": "These records share a course code. Review each supplement separately.",
    }


@mcp.tool(**tool_display("SoC_Search_Courses"))
def search_courses(
    query: str,
    semester: str | None = None,
    campus: str = "NB",
    limit: int = 10,
) -> dict[str, Any]:
    """Search Rutgers courses by title, course code, subject, or description.

    Results are courses offered in the selected Schedule of Classes term. Use
    SoC_Get_Course on a result for full details and optional sections.

    Args:
        query: At least two characters, e.g. computer architecture, 14:332, or ECE.
        semester: Term as the user says it, e.g. Spring 2027 (season plus four-digit
            year), or its code such as 12027. Defaults to the current academic term.
        campus: Rutgers campus code, usually NB, NK, or CM. Defaults to NB.
        limit: Maximum matches to return, from 1 to 25.
    """
    cleaned_query = " ".join(query.lower().split())
    if len(cleaned_query) < 2:
        raise ToolError("query must contain at least two characters")
    if limit < 1 or limit > 25:
        raise ToolError("limit must be between 1 and 25")

    courses, cache_info, resolved_term, resolved_campus = _courses(semester, campus)
    normalized = normalize_course_code(query)
    compact_query = "".join(character for character in cleaned_query if character.isalnum())

    def score(course: dict[str, Any]) -> tuple[int, str]:
        code = str(course.get("courseString", "")).lower()
        compact_code = "".join(character for character in code if character.isalnum())
        title = str(course.get("expandedTitle") or course.get("title") or "").lower()
        subject = str(course.get("subjectDescription") or "").lower()
        description = str(course.get("courseDescription") or "").lower()
        haystack = " ".join((code, title, subject, description))

        if normalized and code == normalized.lower():
            rank = 0
        elif compact_query and compact_code == compact_query:
            rank = 1
        elif title == cleaned_query:
            rank = 2
        elif code.startswith(cleaned_query) or title.startswith(cleaned_query):
            rank = 3
        elif cleaned_query in code or cleaned_query in title:
            rank = 4
        elif cleaned_query in subject:
            rank = 5
        elif cleaned_query in haystack:
            rank = 6
        else:
            rank = 100
        return rank, code

    ranked = sorted(
        ((score(course), course) for course in courses),
        key=lambda item: item[0],
    )
    matches = [course for (rank, _), course in ranked if rank < 100][:limit]
    return {
        "query": query,
        "term": resolved_term.as_dict(resolved_campus),
        "match_count": len(matches),
        "matches": [
            search_result(course, term=resolved_term, campus=resolved_campus) for course in matches
        ],
        "data": cache_info.as_dict(),
        "official_source": SOC_HOME,
    }


def main() -> None:
    """Run the local STDIO MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
