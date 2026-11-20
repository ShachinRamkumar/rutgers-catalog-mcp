"""MCP tools for Middlesex College to Rutgers Engineering equivalencies."""

from __future__ import annotations

import atexit
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nj_transfer_mcp.client import (
    NJ_TRANSFER_LOOKUP,
    RECEIVING_NAME,
    SENDING_NAME,
    NJTransferClient,
    NJTransferError,
    current_equivalencies,
    normalize_middlesex_course_code,
    normalize_rutgers_course_code,
)
from rutgers_soc_mcp.tool_display import server_display, tool_display

RUTGERS_SOE_TRANSFER_POLICY = (
    "https://soe.rutgers.edu/admissions-and-aid/undergraduate-admissions/"
    "transferring-from-outside-Rutgers"
)
RUTGERS_SOE_PREAPPROVAL = (
    "https://soe.rutgers.edu/academic-advising-and-policies/academic-policies/"
    "summer-or-winter-courses"
)

INSTRUCTIONS = (
    "Read-only NJ Transfer equivalencies from Middlesex College to Rutgers School of "
    "Engineering. Starting from a Rutgers course: NJTransfer_Find_Middlesex_Equivalents. "
    "Starting from a Middlesex course: NJTransfer_Middlesex_Course. Titles, partial codes, "
    "electives, or non-transferable courses: NJTransfer_Search. Always report minimum-grade "
    "and footnote restrictions and treat results as advisory; SOE approval may still be "
    "required. Then use Middlesex_Search_Classes for current Middlesex sections and "
    "SoC_Get_Course for the Rutgers course."
)

mcp = MCPServer(
    "NJ Transfer: Middlesex to Rutgers Engineering",
    instructions=INSTRUCTIONS,
    **server_display("NJ Transfer: Middlesex to Rutgers Engineering"),
)
_client = NJTransferClient()
atexit.register(_client.close)


def _records():
    try:
        records, cache_info = _client.get_equivalencies()
    except NJTransferError as exc:
        raise ToolError(str(exc)) from exc
    return current_equivalencies(records), records, cache_info


def _details(matches) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[str]]:
    details: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[str] = []
    for item in matches:
        key = item.detail_key
        try:
            details[key] = _client.get_equivalency_details(item)
        except (NJTransferError, httpx.HTTPError, OSError) as exc:
            errors.append(f"{item.middlesex_course}: {exc}")
    return details, errors


def _envelope(matches, cache_info, *, expand_details: bool = False) -> dict[str, Any]:
    detail_map, detail_errors = _details(matches) if expand_details else ({}, [])
    return {
        "institutions": {"from": SENDING_NAME, "to": RECEIVING_NAME},
        "match_count": len(matches),
        "matches": [item.as_dict(detail_map.get(item.detail_key)) for item in matches],
        "details_expanded": expand_details and not detail_errors,
        "detail_errors": detail_errors,
        "data": cache_info.as_dict(),
        "official_source": NJ_TRANSFER_LOOKUP,
        "rutgers_soe_policy": RUTGERS_SOE_TRANSFER_POLICY,
        "rutgers_soe_preapproval": RUTGERS_SOE_PREAPPROVAL,
        "advisory": (
            "NJ Transfer evaluations are term-sensitive. Review every minimum-grade and footnote "
            "restriction, confirm the course is offered, and obtain Rutgers SOE approval when "
            "required."
        ),
    }


@mcp.tool(**tool_display("NJTransfer_Find_Middlesex_Equivalents"))
def find_middlesex_equivalencies(rutgers_course_code: str) -> dict[str, Any]:
    """Find current Middlesex courses equivalent to an exact Rutgers SOE course.

    Args:
        rutgers_course_code: Rutgers code such as 01:640:152, 01640152, or 1-640-152.
    """
    normalized = normalize_rutgers_course_code(rutgers_course_code)
    if normalized is None:
        raise ToolError("rutgers_course_code must look like 01:640:152 or 01640152")

    current, _, cache_info = _records()
    matches = [item for item in current if normalized in item.rutgers_course_codes]
    result = _envelope(matches, cache_info, expand_details=True)
    result["rutgers_course_code"] = normalized
    if not matches:
        result["interpretation"] = (
            "No current active Middlesex course has this exact Rutgers course equivalency in NJ "
            "Transfer. This is not proof that every Middlesex course is non-transferable."
        )
    return result


@mcp.tool(**tool_display("NJTransfer_Middlesex_Course"))
def get_middlesex_course_equivalency(
    middlesex_course_code: str,
    include_history: bool = False,
) -> dict[str, Any]:
    """Get the Rutgers SOE evaluation for an exact Middlesex course.

    Args:
        middlesex_course_code: Middlesex course ID such as MAT132, MAT-132, or CSC 235.
        include_history: Include superseded historical evaluations as well as the current one.
    """
    normalized = normalize_middlesex_course_code(middlesex_course_code)
    if normalized is None:
        raise ToolError("middlesex_course_code must look like MAT132, MAT-132, or CSC 235")

    current, all_records, cache_info = _records()
    source = all_records if include_history else current
    matches = [item for item in source if item.middlesex_course == normalized]
    matches.sort(key=lambda item: item.evaluation_key, reverse=True)
    result = _envelope(matches, cache_info, expand_details=True)
    result["middlesex_course_code"] = normalized
    result["includes_historical_evaluations"] = include_history
    if not matches:
        result["interpretation"] = (
            "NJ Transfer has no active Middlesex course with this course ID for Rutgers SOE."
        )
    return result


@mcp.tool(**tool_display("NJTransfer_Search"))
def search_equivalencies(
    query: str,
    include_non_transferable: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Search current Middlesex-to-Rutgers-SOE evaluations.

    Args:
        query: Title, Middlesex course ID, Rutgers course code, or equivalency text.
        include_non_transferable: Include Middlesex courses evaluated as Not Transferable.
        limit: Maximum matches to return, from 1 to 25.
    """
    cleaned = " ".join(query.strip().lower().split())
    if len(cleaned) < 2:
        raise ToolError("query must contain at least two characters")
    if limit < 1 or limit > 25:
        raise ToolError("limit must be between 1 and 25")

    current, _, cache_info = _records()
    normalized_rutgers = normalize_rutgers_course_code(query)
    normalized_middlesex = normalize_middlesex_course_code(query)
    compact = recompact(query)

    def score(item) -> tuple[int, str]:
        mcc = item.middlesex_course.lower()
        title = item.title.lower()
        raw = item.equivalency_raw.lower()
        rutgers_codes = " ".join(item.rutgers_course_codes).lower()
        haystack = " ".join((mcc, title, raw, rutgers_codes))
        if normalized_middlesex and item.middlesex_course == normalized_middlesex:
            rank = 0
        elif normalized_rutgers and normalized_rutgers in item.rutgers_course_codes:
            rank = 1
        elif title == cleaned:
            rank = 2
        elif mcc.startswith(compact) or title.startswith(cleaned):
            rank = 3
        elif compact in recompact(mcc) or cleaned in title:
            rank = 4
        elif cleaned in raw or cleaned in rutgers_codes:
            rank = 5
        elif cleaned in haystack:
            rank = 6
        else:
            rank = 100
        return rank, mcc

    ranked = sorted(((score(item), item) for item in current), key=lambda pair: pair[0])
    matches = [
        item
        for (rank, _), item in ranked
        if rank < 100 and (include_non_transferable or item.transferable)
    ][:limit]
    result = _envelope(matches, cache_info)
    result["query"] = query
    result["include_non_transferable"] = include_non_transferable
    return result


def recompact(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def main() -> None:
    """Run the local STDIO MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
