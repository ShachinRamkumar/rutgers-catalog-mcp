from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from rutgers_soc_mcp import server
from rutgers_soc_mcp.client import CacheInfo, Term

SAMPLE_COURSE: dict[str, Any] = {
    "courseString": "14:332:331",
    "expandedTitle": "COMPUTER ARCHITECTURE & ASSEMBLY LANGUAGE",
    "title": "COMP ARCH & ASM LANG",
    "credits": 3,
    "level": "U",
    "school": {"code": "14", "description": "School of Engineering"},
    "subject": "332",
    "subjectDescription": "Electrical and Computer Engineering",
    "courseDescription": "Processor organization and assembly language.",
    "preReqNotes": "14:332:231 and 14:332:233",
    "courseNotes": "",
    "subjectNotes": "",
    "subjectGroupNotes": "",
    "unitNotes": "",
    "campusLocations": [{"code": "2", "description": "Busch"}],
    "sections": [
        {
            "number": "01",
            "index": "17422",
            "openStatus": False,
            "instructors": [{"name": "WEI, SHENG"}],
            "meetingTimes": [
                {
                    "meetingDay": "T",
                    "startTimeMilitary": "1020",
                    "endTimeMilitary": "1140",
                    "meetingModeDesc": "LEC",
                    "campusName": "BUSCH",
                    "buildingCode": "HLL",
                    "roomNumber": "114",
                }
            ],
            "sectionCampusLocations": [{"description": "Busch"}],
            "openToText": "UNIT: 14 (School of Engineering)",
            "sectionEligibility": "",
            "sectionNotes": "COREQ 14:332:333",
            "commentsText": "See online schedule for prereqs",
            "finalExam": "12/18/2026 - 0800 - 1100 AM",
        }
    ],
}


class FakeSocClient:
    cache_dir = Path("/tmp/unused")

    def get_courses(self, term: Term, campus: str):
        return [SAMPLE_COURSE], CacheInfo("test", "2026-08-31T12:00:00+00:00", False)

    def get_open_sections(self, term: Term, campus: str):
        return {"17422"}, CacheInfo("test", "2026-08-31T12:01:00+00:00", False)


@pytest.fixture(autouse=True)
def fake_soc_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_client", FakeSocClient())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_get_course_over_mcp() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "SoC_Get_Course",
            {
                "course_code": "14332331",
                "semester": "Fall 2026",
                "include_sections": True,
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["course_code"] == "14:332:331"
    assert result.structured_content["sections"][0]["open"] is True
    assert result.structured_content["sections"][0]["meeting_times"][0]["start"] == "10:20 AM"
    assert result.structured_content["term"]["semester"] == "92026"
    assert result.structured_content["term"]["semester_label"] == "Fall 2026"


@pytest.mark.anyio
async def test_search_courses_over_mcp() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "SoC_Search_Courses",
            {"query": "computer architecture", "semester": "92026"},
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["match_count"] == 1
    assert result.structured_content["matches"][0]["course_code"] == "14:332:331"


@pytest.mark.anyio
async def test_get_course_returns_visible_tool_error_for_bad_code() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("SoC_Get_Course", {"course_code": "not a course"})

    assert result.is_error is True
    assert "Use SoC_Search_Courses" in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("semester", ["Fall 26", "fall", "Autumn 2026", "92126"])
async def test_unclear_semester_is_refused(semester) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "SoC_Search_Courses", {"query": "computer architecture", "semester": semester}
        )
    assert result.is_error is True
    assert "Spring 2027" in result.content[0].text


@pytest.mark.anyio
async def test_semester_replaces_year_and_term() -> None:
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in ("SoC_Get_Course", "SoC_Search_Courses"):
        properties = tools[name].input_schema["properties"]
        assert "semester" in properties and not {"year", "term"} & set(properties)
