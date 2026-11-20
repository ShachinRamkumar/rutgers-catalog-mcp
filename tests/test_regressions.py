import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mcp import Client

from middlesex_courses_mcp.client import MiddlesexCatalogClient
from nj_transfer_mcp import client as transfer_client
from nj_transfer_mcp.client import NJTransferClient, current_equivalencies, parse_export
from rutgers_soc_mcp import server
from rutgers_soc_mcp.client import CacheInfo, SocApiError, SocClient, Term
from rutgers_soc_mcp.presentation import section_result
from tests.test_middlesex_courses_client import OPTIONS, SECTION
from tests.test_nj_transfer_client import SAMPLE_EXPORT
from tests.test_server import SAMPLE_COURSE


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_rutgers_thursday_uses_full_weekday() -> None:
    result = section_result({"meetingTimes": [{"meetingDay": "H"}]})
    assert result["meeting_times"][0]["day"] == "Thursday"


@pytest.mark.parametrize("limit", [1, 7, 10, 20, 29, 30])
@pytest.mark.parametrize("total", [0, 31, 63])
def test_middlesex_pagination_reaches_every_match(monkeypatch, limit: int, total: int) -> None:
    all_sections = [{**SECTION, "Id": str(index)} for index in range(1, total + 1)]
    with httpx.Client() as http:
        client = MiddlesexCatalogClient(http_client=http)
        monkeypatch.setattr(client, "get_search_options", lambda: OPTIONS)

        def post(_url, payload):
            size = 30
            start = (payload["pageNumber"] - 1) * size
            return {
                "Sections": all_sections[start : start + size],
                "TotalItems": len(all_sections),
                "TotalPages": (len(all_sections) + size - 1) // size,
                "CurrentPageIndex": payload["pageNumber"],
                "PageSize": size,
            }

        monkeypatch.setattr(client, "_post_json", post)
        first = client.search_classes(term="26/FA", limit=limit)
        assert first["total_pages"] == (len(all_sections) + limit - 1) // limit
        assert first["returned"] <= limit
        pages = [first] + [
            client.search_classes(term="26/FA", page=page, limit=limit)
            for page in range(2, first["total_pages"] + 1)
        ]
        assert all(page["returned"] <= limit for page in pages)
        beyond = client.search_classes(term="26/FA", limit=limit, page=first["total_pages"] + 1)
        assert beyond["classes"] == []
    ids = [item["section_id"] for page in pages for item in page["classes"]]
    assert ids == [str(index) for index in range(1, total + 1)]


@pytest.mark.parametrize("course_stale, availability_stale", [(True, False), (False, True)])
@pytest.mark.anyio
async def test_course_and_availability_freshness_stay_separate(
    monkeypatch, course_stale: bool, availability_stale: bool
) -> None:
    class FakeClient:
        def get_courses(self, *_):
            return [SAMPLE_COURSE], CacheInfo("cache", "2026-08-01", course_stale)

        def get_open_sections(self, *_):
            return {"17422"}, CacheInfo("network", "2026-09-04", availability_stale)

    monkeypatch.setattr(server, "_client", FakeClient())
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "SoC_Get_Course", {"course_code": "14:332:331", "include_sections": True}
        )
    data = result.structured_content
    assert not result.is_error
    assert data["data"]["stale"] is True
    assert data["data"]["fetched_at"] == "2026-08-01"
    assert data["availability_data"]["stale"] is availability_stale
    assert data["section_summary"]["availability_is_live_overlay"] is not availability_stale


@pytest.mark.parametrize("endpoint", ["courses", "openSections"])
def test_bad_upstream_payload_preserves_valid_cache(tmp_path: Path, endpoint: str) -> None:
    good = [{"courseString": "14:332:331"}] if endpoint == "courses" else [17422]
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=good if calls == 1 else {"error": "failure"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        fetch = client.get_courses if endpoint == "courses" else client.get_open_sections
        original, _ = fetch(Term(2026, "fall"), "NB")
        path = tmp_path / f"{endpoint}-2026-9-NB.cache.json"
        os.utime(path, (1, 1))
        for _ in range(2):
            result, info = fetch(Term(2026, "fall"), "NB")
            assert result == original
            assert info.stale is True
            assert json.loads(path.read_text())["payload"] == good


@pytest.mark.parametrize(
    "bad",
    [
        {"error": "failure"},
        [None],
        [{"bad": "record"}],
        [{"courseString": "14:332:331", "sections": None}],
        [{"courseString": "14:332:331", "sections": [{"instructors": [None]}]}],
    ],
)
def test_invalid_courses_are_not_cached(tmp_path: Path, bad) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad))
    ) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        with pytest.raises(SocApiError):
            client.get_courses(Term(2026, "fall"), "NB")
    assert not (tmp_path / "courses-2026-9-NB.cache.json").exists()


@pytest.mark.parametrize("content", [b"broken JSON", b'{"error":"old bad response"}', b"\xff"])
def test_corrupted_cache_is_replaced_from_network(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "courses-2026-9-NB.cache.json"
    path.write_bytes(content)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=[{"courseString": "14:332:331"}])
        )
    ) as http:
        courses, info = SocClient(cache_dir=tmp_path, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert courses[0]["courseString"] == "14:332:331"
    assert info.source == "network"


@pytest.mark.anyio
async def test_rutgers_section_pagination_reaches_every_section(monkeypatch) -> None:
    course = copy.deepcopy(SAMPLE_COURSE)
    course["sections"] = [{"index": str(index), "number": str(index)} for index in range(63)]

    class FakeClient:
        def get_courses(self, *_):
            return [course], CacheInfo("test", "2026-10-03", False)

        def get_open_sections(self, *_):
            return {"62"}, CacheInfo("network", "2026-10-03", False)

    monkeypatch.setattr(server, "_client", FakeClient())
    indexes = []
    offset = 0
    async with Client(server.mcp) as client:
        while offset is not None:
            result = await client.call_tool(
                "SoC_Get_Course",
                {
                    "course_code": "14:332:331",
                    "include_sections": True,
                    "section_offset": offset,
                },
            )
            assert not result.is_error
            data = result.structured_content
            indexes.extend(item["index"] for item in data["sections"])
            offset = data["next_section_offset"]
    assert indexes == [str(index) for index in range(63)]
    assert data["sections"][-1]["open"] is True


@pytest.mark.anyio
async def test_rutgers_lecture_lab_variants_are_accessible(monkeypatch) -> None:
    lecture = {**SAMPLE_COURSE, "supplementCode": "  "}
    lab = {**SAMPLE_COURSE, "supplementCode": "LB", "credits": 0}

    class FakeClient:
        def get_courses(self, *_):
            return [lecture, lab], CacheInfo("test", "2026-10-03", False)

    monkeypatch.setattr(server, "_client", FakeClient())
    async with Client(server.mcp) as client:
        all_records = await client.call_tool("SoC_Get_Course", {"course_code": "14:332:331"})
        selected = await client.call_tool(
            "SoC_Get_Course", {"course_code": "14:332:331", "supplement": "LB"}
        )
        main = await client.call_tool(
            "SoC_Get_Course", {"course_code": "14:332:331", "supplement": ""}
        )
    assert not all_records.is_error
    assert [item["supplement"] for item in all_records.structured_content["variants"]] == ["", "LB"]
    assert selected.structured_content["credits"] == 0
    assert main.structured_content["credits"] == 3


def test_transfer_details_expire_and_preserve_provenance(tmp_path: Path, monkeypatch) -> None:
    record = next(
        r
        for r in current_equivalencies(parse_export(SAMPLE_EXPORT))
        if r.middlesex_course == "MAT132"
    )
    clock = [0.0]
    calls = []
    monkeypatch.setattr(transfer_client, "monotonic", lambda: clock[0], raising=False)

    def handler(request):
        calls.append(request)
        grade = "C" if len(calls) == 1 else "B"
        return httpx.Response(200, text=f"MAT132 <strong>Minimum Grade:</strong> {grade}<br/>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(client, "_start_session", lambda: "123")
        first = client.get_equivalency_details(record)
        clock[0] = 10
        cached = client.get_equivalency_details(record)
        assert cached["minimum_grade"] == "C"
        assert len(calls) == 1
        assert cached["data"]["source"] == "cache"
        assert cached["data"]["fetched_at"] == first["data"]["fetched_at"]
        clock[0] = 901
        refreshed = client.get_equivalency_details(record)
    assert refreshed["minimum_grade"] == "B"
    assert len(calls) == 2
    assert refreshed["data"]["source"] == "network"


def test_rutgers_revalidation_uses_etag_and_retains_cache_hint(tmp_path: Path) -> None:
    path = tmp_path / "courses-2026-9-NB.cache.json"
    path.write_text(
        json.dumps(
            {
                "meta": {"etag": "test-etag", "max_age": 1200, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(path, (1, 1))

    def handler(request):
        assert request.headers["If-None-Match"] == "test-etag"
        return httpx.Response(304, headers={"Cache-Control": "max-age=1800"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result, info = SocClient(cache_dir=tmp_path, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert result[0]["courseString"] == "14:332:331"
    assert info.source == "validated_cache"
    assert info.stale is False
    assert json.loads(path.read_text())["meta"]["max_age"] == 1800


@pytest.mark.anyio
async def test_rutgers_overlay_failure_uses_timestamped_course_snapshot(monkeypatch) -> None:
    class FakeClient:
        def get_courses(self, *_):
            return [SAMPLE_COURSE], CacheInfo("cache", "2026-08-01", False)

        def get_open_sections(self, *_):
            raise SocApiError("offline")

    monkeypatch.setattr(server, "_client", FakeClient())
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "SoC_Get_Course", {"course_code": "14:332:331", "include_sections": True}
        )
    data = result.structured_content
    assert data["availability_data"] is None
    assert data["section_summary"]["availability_is_live_overlay"] is False
    assert data["sections"][0]["open"] is False
    assert data["course_data"]["fetched_at"] == "2026-08-01"


@pytest.mark.anyio
async def test_rutgers_schoolless_ambiguity_lists_distinct_full_codes(monkeypatch) -> None:
    class FakeClient:
        def get_courses(self, *_):
            return [SAMPLE_COURSE, {**SAMPLE_COURSE, "courseString": "01:332:331"}], CacheInfo(
                "test", "2026-10-03", False
            )

    monkeypatch.setattr(server, "_client", FakeClient())
    async with Client(server.mcp) as client:
        result = await client.call_tool("SoC_Get_Course", {"course_code": "332:331"})
    assert result.is_error
    assert "01:332:331, 14:332:331" in result.content[0].text


@pytest.mark.parametrize(
    "arguments", [{"section_offset": -1}, {"section_limit": 0}, {"section_limit": 101}]
)
@pytest.mark.anyio
async def test_rutgers_invalid_pagination_is_a_tool_error(arguments) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "SoC_Get_Course", {"course_code": "14:332:331", **arguments}
        )
    assert result.is_error


@pytest.mark.anyio
async def test_transfer_detail_failure_does_not_claim_expansion(monkeypatch) -> None:
    from nj_transfer_mcp import server as transfer_server
    from nj_transfer_mcp.client import CacheInfo as TransferCacheInfo
    from nj_transfer_mcp.client import NJTransferError

    class FakeClient:
        def get_equivalencies(self):
            return parse_export(SAMPLE_EXPORT), TransferCacheInfo("cache", "2026-10-03", False)

        def get_equivalency_details(self, _record):
            raise NJTransferError("details unavailable")

    monkeypatch.setattr(transfer_server, "_client", FakeClient())
    async with Client(transfer_server.mcp) as client:
        result = await client.call_tool(
            "NJTransfer_Middlesex_Course", {"middlesex_course_code": "MAT132"}
        )
    assert result.structured_content["details_expanded"] is False
    assert result.structured_content["detail_errors"] == ["MAT132: details unavailable"]
    assert result.structured_content["matches"][0]["detail_data"] is None


@pytest.mark.anyio
async def test_transfer_history_keeps_distinct_sending_term_details(monkeypatch) -> None:
    from nj_transfer_mcp import server as transfer_server
    from nj_transfer_mcp.client import CacheInfo as TransferCacheInfo

    record = current_equivalencies(parse_export(SAMPLE_EXPORT))[-1]

    class FakeClient:
        def get_equivalencies(self):
            return [record, replace(record, middlesex_effective_term="202109")], TransferCacheInfo(
                "cache", "2026-10-03", False
            )

        def get_equivalency_details(self, evaluation):
            return {
                "minimum_grade": "B" if evaluation.middlesex_effective_term == "202109" else "C"
            }

    monkeypatch.setattr(transfer_server, "_client", FakeClient())
    async with Client(transfer_server.mcp) as client:
        result = await client.call_tool(
            "NJTransfer_Middlesex_Course",
            {"middlesex_course_code": "MAT132", "include_history": True},
        )
    assert [item["minimum_grade"] for item in result.structured_content["matches"]] == ["B", "C"]


def test_transfer_cache_distinguishes_sending_term(tmp_path: Path, monkeypatch) -> None:
    record = current_equivalencies(parse_export(SAMPLE_EXPORT))[-1]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="MAT132 <strong>Minimum Grade:</strong> C<br/>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(client, "_start_session", lambda: "123")
        client.get_equivalency_details(record)
        client.get_equivalency_details(replace(record, middlesex_effective_term="202109"))
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_transfer_export_refresh_invalidates_details(tmp_path: Path, monkeypatch) -> None:
    record = current_equivalencies(parse_export(SAMPLE_EXPORT))[-1]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="MAT132 <strong>Minimum Grade:</strong> C<br/>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(client, "_start_session", lambda: "123")
        monkeypatch.setattr(
            client, "_download_export", lambda: (SAMPLE_EXPORT, "https://www.njtransfer.org/export")
        )
        client.get_equivalency_details(record)
        client.get_equivalencies()
        client.get_equivalency_details(record)
    assert len(calls) == 2


@pytest.mark.anyio
async def test_transfer_search_punctuation_only_query_returns_no_matches(monkeypatch) -> None:
    from nj_transfer_mcp import server as transfer_server
    from nj_transfer_mcp.client import CacheInfo as TransferCacheInfo

    class FakeClient:
        def get_equivalencies(self):
            return parse_export(SAMPLE_EXPORT), TransferCacheInfo("cache", "2026-10-03", False)

    monkeypatch.setattr(transfer_server, "_client", FakeClient())
    async with Client(transfer_server.mcp) as client:
        result = await client.call_tool("NJTransfer_Search", {"query": "--"})
    assert not result.is_error
    assert result.structured_content["match_count"] == 0


@pytest.mark.anyio
async def test_transfer_detail_expansion_is_capped(monkeypatch) -> None:
    from nj_transfer_mcp import server as transfer_server
    from nj_transfer_mcp.client import CacheInfo as TransferCacheInfo

    base = current_equivalencies(parse_export(SAMPLE_EXPORT))[-1]
    many = [
        replace(base, middlesex_course=f"MAT{100 + i}", rutgers_course_codes=("01:640:152",))
        for i in range(transfer_server.MAX_DETAIL_EXPANSIONS + 3)
    ]
    detail_calls = 0

    class FakeClient:
        def get_equivalencies(self):
            return many, TransferCacheInfo("cache", "2026-10-03", False)

        def get_equivalency_details(self, _record):
            nonlocal detail_calls
            detail_calls += 1
            return {"minimum_grade": "C"}

    monkeypatch.setattr(transfer_server, "_client", FakeClient())
    async with Client(transfer_server.mcp) as client:
        result = await client.call_tool(
            "NJTransfer_Find_Middlesex_Equivalents", {"rutgers_course_code": "01:640:152"}
        )
    data = result.structured_content
    assert detail_calls == transfer_server.MAX_DETAIL_EXPANSIONS
    assert data["details_truncated"] is True
    assert data["details_expanded"] is False


def test_transfer_details_reuse_active_session(tmp_path: Path, monkeypatch) -> None:
    record = current_equivalencies(parse_export(SAMPLE_EXPORT))[-1]
    sessions = 0

    def start_session():
        nonlocal sessions
        sessions += 1
        return "999888"

    def handler(_request):
        return httpx.Response(200, text="MAT132 <strong>Minimum Grade:</strong> C<br/>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(client, "_start_session", start_session)
        client.get_equivalency_details(record)
        client.get_equivalency_details(replace(record, middlesex_effective_term="202109"))
    assert sessions == 1


def test_transfer_corrupt_fresh_cache_heals_and_redacts_metadata(tmp_path: Path) -> None:
    (tmp_path / "middlesex-rutgers-soe-active.txt").write_bytes(b"\xff\xfe corrupt export")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/precs.cgi"):
            return httpx.Response(200, text='<meta content="URL=chgri.cgi?77777">')
        if request.method == "POST" and request.url.path.endswith("/precs.cgi"):
            return httpx.Response(
                200,
                text=(
                    "Middlesex College Rutgers-School of Engineering "
                    '<input name="ArtId" value="88888">'
                ),
            )
        if request.method == "POST" and request.url.path.endswith("/listeqs.cgi"):
            return httpx.Response(
                200,
                text=(
                    '<a href="https://www.njtransfer.org/artweb/tempfiles/'
                    'EN.MI.88888.listeq.txt">Download</a>'
                ),
            )
        if request.method == "GET" and request.url.path.endswith(".listeq.txt"):
            return httpx.Response(200, text=SAMPLE_EXPORT)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        records, info = NJTransferClient(cache_dir=tmp_path, http_client=http).get_equivalencies()
    assert len(records) == 5
    assert info.source == "network"
    meta = json.loads((tmp_path / "middlesex-rutgers-soe-active.meta.json").read_text())
    assert "88888" not in meta["url"]
    assert meta["url"] == transfer_client.NJ_TRANSFER_LOOKUP


def test_transfer_error_sanitization_redacts_session_ids() -> None:
    raw = (
        "Client error for url "
        "'https://www.njtransfer.org/artweb/crs-srch.cgi?Z~987654~MI~EN~MAT132' "
        "and 'https://www.njtransfer.org/artweb/tempfiles/EN.MI.987654.listeq.txt'"
    )
    sanitized = transfer_client.sanitize_error(raw)
    assert "987654" not in sanitized
    assert "<redacted>" in sanitized


def test_soc_honors_no_store_and_invalidates_on_304(tmp_path: Path) -> None:
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": "etag-v1", "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331", "sections": []}],
            }
        )
    )
    os.utime(cache_file, (1, 1))

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(304, headers={"Cache-Control": "no-store"})
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        courses, info = client.get_courses(Term(2026, "fall"), "NB")
        assert courses[0]["courseString"] == "14:332:331"
        assert info.source == "validated_cache"
        assert not cache_file.exists()
        with pytest.raises(SocApiError):
            client.get_courses(Term(2026, "fall"), "NB")

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[{"courseString": "14:332:331", "sections": []}],
                headers={"Cache-Control": "no-store, max-age=900"},
            )
        )
    ) as http:
        courses, info = SocClient(cache_dir=tmp_path, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert courses[0]["courseString"] == "14:332:331"
    assert info.source == "network"
    assert not cache_file.exists()


def test_soc_atomic_bundle_preserves_matching_etag_on_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": "etag-v1", "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(cache_file, (1, 1))

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=[{"courseString": "14:332:452"}],
                headers={"ETag": "etag-v2", "Cache-Control": "max-age=900"},
            )
        )
    ) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(
            client,
            "_atomic_write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            client.get_courses(Term(2026, "fall"), "NB")

    stored = json.loads(cache_file.read_text())
    assert stored["meta"]["etag"] == "etag-v1"
    assert stored["payload"] == [{"courseString": "14:332:331"}]


def test_transfer_detail_failure_redacts_exception_traceback(tmp_path: Path, monkeypatch) -> None:
    import traceback

    record = current_equivalencies(parse_export(SAMPLE_EXPORT))[-1]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(client, "_start_session", lambda: "555444333")
        with pytest.raises(transfer_client.NJTransferError) as exc_info:
            client.get_equivalency_details(record)

    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "555444333" not in formatted
    assert exc_info.value.__cause__ is None


def test_transfer_migrates_legacy_metadata_and_recovers_from_corrupt_meta_and_csv(
    tmp_path: Path, monkeypatch
) -> None:
    import csv

    data_path = tmp_path / "middlesex-rutgers-soe-active.txt"
    meta_path = tmp_path / "middlesex-rutgers-soe-active.meta.json"
    data_path.write_text(SAMPLE_EXPORT, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "fetched_at": "2026-10-03T00:00:00+00:00",
                "url": "https://www.njtransfer.org/artweb/tempfiles/EN.MI.444555.listeq.txt",
            }
        )
    )

    with httpx.Client() as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        records, info = client.get_equivalencies()
    assert len(records) == 5
    assert info.source == "cache"
    migrated = json.loads(meta_path.read_text())
    assert "444555" not in migrated["url"]
    assert migrated["url"] == transfer_client.NJ_TRANSFER_LOOKUP

    # Invalid UTF-8 metadata + oversized CSV field in cache both self-heal from network.
    meta_path.write_bytes(b"\xff\xfe\xfd")
    oversized = "A" * (csv.field_size_limit() + 10)
    data_path.write_text(f"SI~Course~RI~Receiver Term~Equivalency~Transferable\n{oversized}\n")
    with httpx.Client() as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(
            client, "_download_export", lambda: (SAMPLE_EXPORT, transfer_client.NJ_TRANSFER_LOOKUP)
        )
        healed, healed_info = client.get_equivalencies()
    assert len(healed) == 5
    assert healed_info.source == "network"


@pytest.mark.anyio
async def test_transfer_history_excludes_inactive_courses(monkeypatch) -> None:
    from nj_transfer_mcp import server as transfer_server
    from nj_transfer_mcp.client import CacheInfo as TransferCacheInfo

    class FakeClient:
        def get_equivalencies(self):
            return parse_export(SAMPLE_EXPORT), TransferCacheInfo("cache", "2026-10-03", False)

    monkeypatch.setattr(transfer_server, "_client", FakeClient())
    async with Client(transfer_server.mcp) as client:
        result = await client.call_tool(
            "NJTransfer_Middlesex_Course",
            {"middlesex_course_code": "OLD100", "include_history": True},
        )
    assert not result.is_error
    assert result.structured_content["match_count"] == 0


@pytest.mark.anyio
async def test_middlesex_search_options_includes_retrieval_timestamp(monkeypatch) -> None:
    from middlesex_courses_mcp import server as middlesex_server
    from tests.test_middlesex_courses_server import FakeMiddlesexClient

    monkeypatch.setattr(middlesex_server, "_client", FakeMiddlesexClient())
    async with Client(middlesex_server.mcp) as client:
        result = await client.call_tool("Middlesex_Search_Options", {})
    assert not result.is_error
    assert result.structured_content["retrieved_at"]


@pytest.mark.parametrize(
    "bad_entry",
    [
        "rutgers_soc_mcp/.claude.json",
        "rutgers_soc_mcp/.CLAUDE.JSON",
        "rutgers_soc_mcp/1-.claude.json",
        "rutgers_soc_mcp/0-config.toml",
    ],
)
def test_verify_package_rejects_nested_private_client_configs(
    tmp_path: Path, bad_entry: str
) -> None:
    import io
    import runpy
    import tarfile
    import zipfile

    script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_package.py"))
    check_archives = script["check_archives"]

    clean_sdist = tmp_path / "clean.tar.gz"
    with tarfile.open(clean_sdist, "w:gz") as tar:
        info = tarfile.TarInfo("pkg-0.1.0/README.md")
        data = b"ok"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    bad_wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(bad_wheel, "w") as zf:
        zf.writestr(bad_entry, "{}")

    with pytest.raises(ValueError, match="Private state or client config"):
        check_archives(bad_wheel, clean_sdist)

    bad_sdist = tmp_path / "bad.tar.gz"
    with tarfile.open(bad_sdist, "w:gz") as tar:
        info = tarfile.TarInfo(f"pkg-0.1.0/src/{bad_entry}")
        data = b"model = 'x'"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    clean_wheel = tmp_path / "clean.whl"
    with zipfile.ZipFile(clean_wheel, "w") as zf:
        zf.writestr("rutgers_soc_mcp/__init__.py", "")

    with pytest.raises(ValueError, match="Private state or client config"):
        check_archives(clean_wheel, bad_sdist)


def test_soc_mismatched_304_etag_refetches_unconditionally(tmp_path: Path) -> None:
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": '"v1"', "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(cache_file, (1, 1))
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.headers))
        if len(requests) == 1:
            return httpx.Response(304, headers={"ETag": '"v2"'})
        assert "if-none-match" not in {k.lower() for k in request.headers}
        return httpx.Response(
            200,
            json=[{"courseString": "14:332:452"}],
            headers={"ETag": '"v2"', "Cache-Control": "max-age=900"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        courses, info = SocClient(cache_dir=tmp_path, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert len(requests) == 2
    assert courses == [{"courseString": "14:332:452"}]
    assert info.source == "network"
    stored = json.loads(cache_file.read_text())
    assert stored["meta"]["etag"] == '"v2"'
    assert stored["payload"] == [{"courseString": "14:332:452"}]


def test_soc_legacy_split_cache_falls_back_offline_without_trusting_old_etag(
    tmp_path: Path,
) -> None:
    (tmp_path / "courses-2026-9-NB.json").write_text('[{"courseString":"14:332:331"}]')
    (tmp_path / "courses-2026-9-NB.meta.json").write_text(
        '{"etag":"legacy-etag","max_age":-1,"fetched_at":"2026-08-01"}'
    )
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        courses, info = SocClient(cache_dir=tmp_path, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert courses == [{"courseString": "14:332:331"}]
    assert info.source == "stale_cache"
    assert info.stale is True
    assert "if-none-match" not in {k.lower() for k in seen_headers[0]}


def test_soc_malformed_200_no_store_evicts_previous_bundle(tmp_path: Path) -> None:
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": '"v1"', "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(cache_file, (1, 1))

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"error": "malformed"},
                headers={"Cache-Control": "no-store"},
            )
        )
    ) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        with pytest.raises(SocApiError):
            client.get_courses(Term(2026, "fall"), "NB")
    assert not cache_file.exists()


def test_transfer_metadata_migration_failure_does_not_block_fresh_cache_read(
    tmp_path: Path, monkeypatch
) -> None:
    data_path = tmp_path / "middlesex-rutgers-soe-active.txt"
    meta_path = tmp_path / "middlesex-rutgers-soe-active.meta.json"
    data_path.write_text(SAMPLE_EXPORT, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "fetched_at": "2026-10-03T00:00:00+00:00",
                "url": "https://www.njtransfer.org/artweb/tempfiles/EN.MI.333222.listeq.txt",
            }
        )
    )

    with httpx.Client() as http:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http)
        monkeypatch.setattr(
            client,
            "_atomic_write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        records, info = client.get_equivalencies()
    assert len(records) == 5
    assert info.source == "cache"


def test_soc_unsolicited_304_and_retry_304_do_not_mark_stale_data_fresh(tmp_path: Path) -> None:
    # Legacy split cache sends no If-None-Match; an unsolicited 304 must NOT refresh it.
    (tmp_path / "courses-2026-9-NB.json").write_text('[{"courseString":"14:332:331"}]')
    (tmp_path / "courses-2026-9-NB.meta.json").write_text(
        '{"etag":"legacy-etag","max_age":-1,"fetched_at":"2026-08-01"}'
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(304))) as http:
        courses, info = SocClient(cache_dir=tmp_path, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert courses == [{"courseString": "14:332:331"}]
    assert info.source == "stale_cache"
    assert info.stale is True
    assert not (tmp_path / "courses-2026-9-NB.cache.json").exists()

    # On mismatch-and-retry, a second 304 is a protocol error that must reach the caller.
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": '"v1"', "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(cache_file, (1, 1))
    calls = 0

    def mismatch_then_304(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(304, headers={"ETag": '"v2"'})
        return httpx.Response(304)

    with httpx.Client(transport=httpx.MockTransport(mismatch_then_304)) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        with pytest.raises(SocApiError, match="unconditional GET"):
            client.get_courses(Term(2026, "fall"), "NB")
    assert calls == 2


def test_soc_mismatched_304_with_no_store_evicts_before_retry(tmp_path: Path) -> None:
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": '"v1"', "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(cache_file, (1, 1))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(304, headers={"ETag": '"v2"', "Cache-Control": "no-store"})
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        with pytest.raises(SocApiError):
            client.get_courses(Term(2026, "fall"), "NB")
    assert not cache_file.exists()


def test_transfer_read_only_cache_directory_still_serves_fresh_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "ro-cache"
    cache_dir.mkdir()
    (cache_dir / "middlesex-rutgers-soe-active.txt").write_text(SAMPLE_EXPORT, encoding="utf-8")
    (cache_dir / "middlesex-rutgers-soe-active.meta.json").write_text(
        json.dumps(
            {
                "fetched_at": "2026-10-03T00:00:00+00:00",
                "url": "https://www.njtransfer.org/artweb/tempfiles/EN.MI.111222.listeq.txt",
            }
        )
    )
    if os.name != "nt":
        cache_dir.chmod(0o555)
    try:
        with httpx.Client() as http:
            records, info = NJTransferClient(
                cache_dir=cache_dir, http_client=http
            ).get_equivalencies()
        assert len(records) == 5
        assert info.source == "cache"
    finally:
        if os.name != "nt":
            cache_dir.chmod(0o755)


def test_soc_retry_304_with_no_store_evicts_cached_bundle(tmp_path: Path) -> None:
    cache_file = tmp_path / "courses-2026-9-NB.cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "meta": {"etag": '"v1"', "max_age": 60, "fetched_at": "2026-08-01"},
                "payload": [{"courseString": "14:332:331"}],
            }
        )
    )
    os.utime(cache_file, (1, 1))
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(304, headers={"ETag": '"v2"'})
        return httpx.Response(304, headers={"Cache-Control": "no-store"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = SocClient(cache_dir=tmp_path, http_client=http)
        with pytest.raises(SocApiError, match="unconditional GET"):
            client.get_courses(Term(2026, "fall"), "NB")
    assert not cache_file.exists()


def test_unlocked_clients_do_not_write_and_return_upstream_on_miss(
    tmp_path: Path, monkeypatch
) -> None:
    # Occupying the lock path with a directory forces unlocked mode even in a writable dir.
    soc_dir = tmp_path / "soc"
    soc_dir.mkdir()
    (soc_dir / ".courses-2026-9-NB.lock").mkdir()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=[{"courseString": "14:332:331"}])
        )
    ) as http:
        courses, info = SocClient(cache_dir=soc_dir, http_client=http).get_courses(
            Term(2026, "fall"), "NB"
        )
    assert courses == [{"courseString": "14:332:331"}]
    assert info.source == "network"
    assert not (soc_dir / "courses-2026-9-NB.cache.json").exists()

    nj_dir = tmp_path / "nj"
    nj_dir.mkdir()
    (nj_dir / ".middlesex-rutgers-soe-active.lock").mkdir()
    with httpx.Client() as http:
        client = NJTransferClient(cache_dir=nj_dir, http_client=http)
        monkeypatch.setattr(
            client, "_download_export", lambda: (SAMPLE_EXPORT, transfer_client.NJ_TRANSFER_LOOKUP)
        )
        records, nj_info = client.get_equivalencies()
    assert len(records) == 5
    assert nj_info.source == "network"
    assert not (nj_dir / "middlesex-rutgers-soe-active.txt").exists()
