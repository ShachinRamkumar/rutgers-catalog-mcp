"""MCP tools for Middlesex College's public course and section catalog."""

from __future__ import annotations

import atexit
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from middlesex_courses_mcp.client import (
    CATALOG_URL,
    MiddlesexCatalogClient,
    MiddlesexCatalogError,
)
from rutgers_soc_mcp.tool_display import server_display, tool_display

INSTRUCTIONS = (
    "Read-only Middlesex College (New Jersey) course sections. Middlesex_Search_Options lists "
    "valid terms, subjects and locations when one needs clarifying; Middlesex_Search_Classes "
    "finds live sections; Middlesex_Class_Details takes a numeric section_id from a search "
    "result for prerequisites, fees, meetings and instructors. Report the retrieval timestamp "
    "and treat seat availability as advisory. Before recommending a Middlesex course for "
    "Rutgers credit, check NJTransfer_Middlesex_Course (NJ Transfer server) for its Rutgers "
    "School of Engineering equivalency and minimum grade."
)

mcp = MCPServer(
    "Middlesex College Course Search",
    instructions=INSTRUCTIONS,
    **server_display("Middlesex College Course Search"),
)
_client = MiddlesexCatalogClient()
atexit.register(_client.close)


def _tool_call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except (MiddlesexCatalogError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(**tool_display("Middlesex_Search_Options"))
def list_class_search_options() -> dict[str, Any]:
    """List current searchable terms, subjects, locations, levels, and course types."""
    data = _tool_call(_client.get_search_options)

    def pairs(key: str, *, subject: bool = False) -> list[dict[str, Any]]:
        output = []
        for item in data.get(key) or []:
            code = item.get("Code") if subject else item.get("Item1")
            name = item.get("Description") if subject else item.get("Item2")
            output.append({"code": code, "name": name})
        return output

    return {
        "terms": pairs("Terms"),
        "subjects": pairs("Subjects", subject=True),
        "locations": pairs("Locations"),
        "academic_levels": pairs("AcademicLevels"),
        "course_types": pairs("CourseTypes"),
        "days": [item.get("Item1") for item in data.get("DaysOfWeek") or []],
        "retrieved_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "official_source": CATALOG_URL,
    }


@mcp.tool(**tool_display("Middlesex_Search_Classes"))
def search_classes(
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
    """Search live Middlesex College class sections.

    Args:
        query: Optional title/description keyword or class code such as CSC-105-01.
        term: Term code or name, e.g. 26/FA or Fall 2026. Defaults to the current season.
        subject: Subject code or name, e.g. CSC or Computer Science.
        course_number: Course number; requires subject.
        section_number: Exact section number; requires subject and course_number.
        locations: Location codes or names, e.g. NET or Online Course.
        days: Day names such as Monday or Wed.
        open_only: Return only sections currently marked open.
        online: True for online-only, false for not-online, or null for either.
        starts_at_time: Optional catalog time filter such as 9:00 AM.
        ends_by_time: Optional catalog time filter such as 5:00 PM.
        page: One-based result page.
        limit: Number of results to return from the page, 1-30.
    """
    return _tool_call(
        _client.search_classes,
        query=query,
        term=term,
        subject=subject,
        course_number=course_number,
        section_number=section_number,
        locations=locations,
        days=days,
        open_only=open_only,
        online=online,
        starts_at_time=starts_at_time,
        ends_by_time=ends_by_time,
        page=page,
        limit=limit,
    )


@mcp.tool(**tool_display("Middlesex_Class_Details"))
def get_class_details(section_id: str) -> dict[str, Any]:
    """Get prerequisites, fees, meetings, and instructors for one search result.

    Args:
        section_id: Numeric section_id returned by Middlesex_Search_Classes, e.g. 188940.
    """
    return _tool_call(_client.get_class_details, section_id)


def main() -> None:
    """Run the local STDIO MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
