# ruff: noqa: E501

from pathlib import Path

import httpx

from nj_transfer_mcp.client import (
    CacheInfo,
    NJTransferClient,
    current_equivalencies,
    normalize_middlesex_course_code,
    normalize_rutgers_course_code,
    parse_export,
)

SAMPLE_EXPORT = """SI~Course~RI~Sender Term~Receiver Term~RI GEd~Min Grade~Equivalency~RI Min Cr~RI Max Cr~PMAP~Transferable~SI Min Cr~SI Max Cr~SI Active~SI Repeatable~Course Title~SI GEd~Notes~
MI~MAT132~EN~200109~200109~~C~01640152\"AS2,R43\"~04.00~4~~Y~4~04.00~Y~N~ANALYTIC GEOMETRY AND CALCULUS II~,GMAT,~~
MI~MAT132~EN~200109~201906~~~01640152\"R44\"~04.00~4~~Y~4~04.00~Y~N~ANALYTIC GEOMETRY AND CALCULUS II~,GMAT,~~
MI~CSC235~EN~200109~200109~,TE,~~14332252 \"R44\"~04.00~4~~Y~4~04.00~Y~N~DATA STRUCTURES~~~
MI~ACC101~EN~200109~200109~~~Not Transferable~00.00~0~~N~4~04.00~Y~N~FINANCIAL ACCOUNTING~~~
MI~OLD100~EN~200109~200109~~~01000100~03.00~3~~Y~3~03.00~N~N~OLD COURSE~~~
"""


def test_normalize_course_codes() -> None:
    assert normalize_rutgers_course_code("1-640-152") == "01:640:152"
    assert normalize_rutgers_course_code("01640152") == "01:640:152"
    assert normalize_rutgers_course_code("calculus") is None
    assert normalize_middlesex_course_code("mat-132") == "MAT132"
    assert normalize_middlesex_course_code("not a course") is None


def test_parse_and_select_current_equivalencies() -> None:
    records = parse_export(SAMPLE_EXPORT)
    current = current_equivalencies(records)

    assert len(records) == 5
    assert [item.middlesex_course for item in current] == ["ACC101", "CSC235", "MAT132"]
    mat132 = next(item for item in current if item.middlesex_course == "MAT132")
    assert mat132.rutgers_course_codes == ("01:640:152",)
    assert mat132.footnotes == ("R44",)
    assert mat132.rutgers_evaluation_effective_term == "201906"


def test_client_fetches_export_then_uses_cache(tmp_path: Path) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/precs.cgi"):
            return httpx.Response(
                200, text='<meta http-equiv="Refresh" content="0; URL=chgri.cgi?123">'
            )
        if request.method == "POST" and request.url.path.endswith("/precs.cgi"):
            return httpx.Response(
                200,
                text=(
                    "Middlesex College to Rutgers-School of Engineering "
                    '<input name="ArtId" value="456">'
                ),
            )
        if request.method == "POST" and request.url.path.endswith("/listeqs.cgi"):
            return httpx.Response(
                200,
                text=(
                    '<a href="https://www.njtransfer.org/artweb/tempfiles/'
                    'EN.MI.456.listeq.txt">Download</a>'
                ),
            )
        if request.method == "GET" and request.url.path.endswith(".listeq.txt"):
            return httpx.Response(200, text=SAMPLE_EXPORT)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http_client)
        first, first_info = client.get_equivalencies()
        second, second_info = client.get_equivalencies()

    assert len(first) == len(second) == 5
    assert first_info.source == "network"
    assert second_info.source == "cache"
    assert len(requests) == 4


def test_client_reads_detail_grade_footnotes_and_link(tmp_path: Path) -> None:
    records = parse_export(SAMPLE_EXPORT)
    record = next(
        item for item in current_equivalencies(records) if item.middlesex_course == "MAT132"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/precs.cgi") and request.method == "GET":
            return httpx.Response(200, text='<meta content="URL=chgri.cgi?123">')
        if request.url.path.endswith("/precs.cgi") and request.method == "POST":
            return httpx.Response(
                200,
                text=(
                    "Middlesex College Rutgers-School of Engineering "
                    '<input name="ArtId" value="456">'
                ),
            )
        if request.url.path.endswith("/crs-srch.cgi"):
            return httpx.Response(
                200,
                text=(
                    "MAT132 <strong>Minimum Grade: </strong> C "
                    "<strong>Foot Note:</strong> <em>R44</em>&#160; Dual enrollment policy"
                    "<br/><strong>LINK</strong>: MAT131 and MAT132 = 01640152<br/>"
                ),
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = NJTransferClient(cache_dir=tmp_path, http_client=http_client)
        details = client.get_equivalency_details(record)

    assert details["minimum_grade"] == "C"
    assert details["footnote_details"] == [{"code": "R44", "text": "Dual enrollment policy"}]
    assert details["linked_course_requirement"] == "MAT131 and MAT132 = 01640152"


def test_cache_info_shape() -> None:
    assert CacheInfo("test", "2026-09-01T00:00:00+00:00", False).as_dict()["source"] == "test"
