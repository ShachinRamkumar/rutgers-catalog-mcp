from pathlib import Path

import pytest
from mcp import Client

from nj_transfer_mcp import server
from nj_transfer_mcp.client import CacheInfo, parse_export
from tests.test_nj_transfer_client import SAMPLE_EXPORT


class FakeNJTransferClient:
    cache_dir = Path("/tmp/unused")

    def get_equivalencies(self):
        return parse_export(SAMPLE_EXPORT), CacheInfo("test", "2026-09-01T12:00:00+00:00", False)

    def get_equivalency_details(self, record):
        if record.middlesex_course == "MAT132":
            return {
                "minimum_grade": "C",
                "footnote_details": [{"code": "R44", "text": "Dual enrollment policy"}],
                "linked_course_requirement": None,
            }
        return {}


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_client", FakeNJTransferClient())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_find_middlesex_equivalencies_from_rutgers_code() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "NJTransfer_Find_Middlesex_Equivalents", {"rutgers_course_code": "01640152"}
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["match_count"] == 1
    match = result.structured_content["matches"][0]
    assert match["middlesex_course"] == "MAT132"
    assert match["minimum_grade"] == "C"
    assert match["footnote_details"][0]["code"] == "R44"


@pytest.mark.anyio
async def test_get_middlesex_course_can_include_history() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "NJTransfer_Middlesex_Course",
            {"middlesex_course_code": "MAT-132", "include_history": True},
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["match_count"] == 2
    assert result.structured_content["matches"][0]["rutgers_evaluation_effective_term"] == (
        "Summer 2019"
    )


@pytest.mark.anyio
async def test_search_can_include_non_transferable_courses() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        hidden = await client.call_tool("NJTransfer_Search", {"query": "accounting"})
        included = await client.call_tool(
            "NJTransfer_Search",
            {"query": "accounting", "include_non_transferable": True},
        )

    assert hidden.structured_content is not None
    assert hidden.structured_content["match_count"] == 0
    assert included.structured_content is not None
    assert included.structured_content["matches"][0]["middlesex_course"] == "ACC101"


@pytest.mark.anyio
async def test_bad_rutgers_code_is_visible_tool_error() -> None:
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "NJTransfer_Find_Middlesex_Equivalents", {"rutgers_course_code": "calculus"}
        )

    assert result.is_error is True
    assert "01:640:152" in result.content[0].text
