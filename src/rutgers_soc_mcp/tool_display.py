"""Tool labels and response presentation metadata for MCP hosts.

`server_display` attaches a lightweight `_meta.source` record to tool-call responses so MCP
clients that display per-call source attribution can label results consistently across all
three catalog servers without embedding external URLs or binary icon assets.
"""

from __future__ import annotations

from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

LABELS = {
    "SoC_Get_Course": "SoC Get Course",
    "SoC_Search_Courses": "SoC Search Courses",
    "Middlesex_Search_Options": "Middlesex Search Options",
    "Middlesex_Search_Classes": "Middlesex Search Classes",
    "Middlesex_Class_Details": "Middlesex Class Details",
    "NJTransfer_Find_Middlesex_Equivalents": "NJ Transfer Find Middlesex Equivalents",
    "NJTransfer_Middlesex_Course": "NJ Transfer Middlesex Course",
    "NJTransfer_Search": "NJ Transfer Search",
}


def tool_display(name: str) -> dict[str, Any]:
    title = LABELS[name]
    return {
        "name": name,
        "title": title,
        "meta": {
            "openai/toolInvocation/invoking": title,
            "openai/toolInvocation/invoked": title,
        },
    }


def server_display(name: str) -> dict[str, Any]:
    source = {"name": name}

    async def present(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method == "tools/call" and isinstance(result, dict):
            tool = (ctx.params or {}).get("name")
            if tool in LABELS:
                result = {
                    **result,
                    "_meta": {
                        **(result.get("_meta") or {}),
                        "source": source,
                    },
                }
        return result

    return {"title": name, "middleware": [present]}
