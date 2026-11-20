from typing import Any

import pytest
from mcp import Client

from middlesex_courses_mcp import server


class FakeMiddlesexClient:
    def get_search_options(self) -> dict[str, Any]:
        return {
            "Terms": [{"Item1": "26/FA", "Item2": "Fall 2026"}],
            "Subjects": [{"Code": "CSC", "Description": "Computer Science"}],
            "Locations": [{"Item1": "NET", "Item2": "Online Course"}],
            "AcademicLevels": [{"Item1": "UG", "Item2": "Undergraduate"}],
            "CourseTypes": [],
            "DaysOfWeek": [{"Item1": "Monday", "Item2": "1"}],
        }

    def search_classes(self, **kwargs) -> dict[str, Any]:
        return {
            "query": kwargs["query"],
            "total_matches": 1,
            "classes": [{"section_id": "188940", "section_code": "CSC-105-01"}],
        }

    def get_class_details(self, section_id: str) -> dict[str, Any]:
        if not section_id.isdigit():
            raise ValueError("section_id must be numeric")
        return {"section_id": section_id, "requisites": []}


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_client", FakeMiddlesexClient())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_list_class_search_options_over_mcp() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("Middlesex_Search_Options", {})

    assert result.is_error is False
    assert result.structured_content["terms"][0] == {"code": "26/FA", "name": "Fall 2026"}


@pytest.mark.anyio
async def test_search_classes_over_mcp() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "Middlesex_Search_Classes",
            {"query": "computer", "term": "Fall 2026", "open_only": True},
        )

    assert result.is_error is False
    assert result.structured_content["classes"][0]["section_id"] == "188940"


@pytest.mark.anyio
async def test_get_class_details_returns_visible_tool_error() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("Middlesex_Class_Details", {"section_id": "not-an-id"})

    assert result.is_error is True
    assert "must be numeric" in result.content[0].text
