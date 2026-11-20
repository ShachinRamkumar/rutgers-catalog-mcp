import pytest
from mcp import Client

from middlesex_courses_mcp import server as middlesex
from nj_transfer_mcp import server as transfer
from rutgers_soc_mcp import server as soc
from rutgers_soc_mcp.tool_display import server_display
from tests.test_middlesex_courses_server import FakeMiddlesexClient
from tests.test_nj_transfer_server import FakeNJTransferClient
from tests.test_server import FakeSocClient

CASES = [
    (soc, "SoC_Get_Course", {"course_code": "14:332:331"}, "SoC Get Course"),
    (soc, "SoC_Search_Courses", {"query": "computer"}, "SoC Search Courses"),
    (middlesex, "Middlesex_Search_Options", {}, "Middlesex Search Options"),
    (middlesex, "Middlesex_Search_Classes", {"query": "computer"}, "Middlesex Search Classes"),
    (middlesex, "Middlesex_Class_Details", {"section_id": "188940"}, "Middlesex Class Details"),
    (
        transfer,
        "NJTransfer_Find_Middlesex_Equivalents",
        {"rutgers_course_code": "01:640:152"},
        "NJ Transfer Find Middlesex Equivalents",
    ),
    (
        transfer,
        "NJTransfer_Middlesex_Course",
        {"middlesex_course_code": "MAT132"},
        "NJ Transfer Middlesex Course",
    ),
    (transfer, "NJTransfer_Search", {"query": "calculus"}, "NJ Transfer Search"),
]


@pytest.fixture
def isolated_backends(monkeypatch):
    monkeypatch.setattr(soc, "_client", FakeSocClient())
    monkeypatch.setattr(middlesex, "_client", FakeMiddlesexClient())
    monkeypatch.setattr(transfer, "_client", FakeNJTransferClient())


@pytest.mark.anyio
@pytest.mark.parametrize("server,name,arguments,title", CASES)
async def test_discovery_and_wire_results_include_presentation(
    isolated_backends, server, name, arguments, title
):
    async with Client(server.mcp, raise_exceptions=True) as client:
        tool = next(item for item in (await client.list_tools()).tools if item.name == name)
        assert tool.title == title
        assert tool.meta["openai/toolInvocation/invoking"] == title
        assert tool.meta["openai/toolInvocation/invoked"] == title
        assert len(title) <= 64
        assert client.server_info.title == server.mcp.title

        result = await client.call_tool(name, arguments)
        assert not result.is_error
        assert result.meta["source"] == {"name": server.mcp.title}
        assert "io.modelcontextprotocol/serverInfo" in result.meta
        assert result.structured_content
        assert "source" not in result.structured_content


@pytest.mark.anyio
async def test_tool_errors_preserve_source_metadata_and_error_flag(isolated_backends):
    async with Client(soc.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("SoC_Get_Course", {"course_code": "invalid"})
        assert result.is_error
        assert "Use SoC_Search_Courses" in result.content[0].text
        assert result.meta["source"] == {"name": "Rutgers Schedule of Classes"}

    async with Client(transfer.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "NJTransfer_Find_Middlesex_Equivalents", {"rutgers_course_code": "invalid"}
        )
        assert result.is_error
        assert result.meta["source"] == {"name": "NJ Transfer: Middlesex to Rutgers Engineering"}


@pytest.mark.anyio
async def test_existing_result_metadata_survives_presentation_layer():
    from mcp.server import MCPServer
    from mcp.types import CallToolResult, TextContent

    mcp = MCPServer("Display test", **server_display("Display test"))

    @mcp.tool(name="SoC_Get_Course")
    def get_course() -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text="result")], _meta={"kept": 42})

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("SoC_Get_Course", {})
        assert result.meta["kept"] == 42
        assert result.content[0].text == "result"
        assert result.meta["source"] == {"name": "Display test"}
