from datetime import datetime
from pathlib import Path

import httpx
import pytest

from rutgers_soc_mcp.client import (
    SocClient,
    Term,
    clean_text,
    current_term,
    normalize_course_code,
    normalize_subject_course,
    resolve_semester,
    resolve_term,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("14:332:331", "14:332:331"),
        ("14-332-331", "14:332:331"),
        ("14 332 331", "14:332:331"),
        ("14332331", "14:332:331"),
        ("1:198:111", "01:198:111"),
        ("computer architecture", None),
    ],
)
def test_normalize_course_code(value: str, expected: str | None) -> None:
    assert normalize_course_code(value) == expected


def test_normalize_subject_course() -> None:
    assert normalize_subject_course("332:331") == "332:331"
    assert normalize_subject_course("14:332:331") is None


@pytest.mark.parametrize(
    ("month", "expected"),
    [(1, "spring"), (5, "spring"), (6, "summer"), (7, "summer"), (8, "fall"), (12, "fall")],
)
def test_current_term(month: int, expected: str) -> None:
    assert current_term(datetime(2026, month, 1)).name == expected


def test_resolve_term_accepts_numeric_alias() -> None:
    assert resolve_term(2026, "9") == Term(year=2026, name="fall")


def test_clean_text_removes_soc_markup() -> None:
    assert clean_text("A <em> OR </em> B<br>C &amp; D") == "A OR B C & D"


def test_client_fetches_then_uses_fresh_cache(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.params["year"] == "2026"
        assert request.url.params["term"] == "9"
        assert request.url.params["campus"] == "NB"
        return httpx.Response(
            200,
            json=[{"courseString": "14:332:331"}],
            headers={"Cache-Control": "max-age=900", "ETag": "sample-etag"},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = SocClient(cache_dir=tmp_path, http_client=http_client)
        first, first_info = client.get_courses(Term(2026, "fall"), "NB")
        second, second_info = client.get_courses(Term(2026, "fall"), "NB")

    assert first == second == [{"courseString": "14:332:331"}]
    assert first_info.source == "network"
    assert second_info.source == "cache"
    assert requests == 1


def test_client_falls_back_to_stale_cache(tmp_path: Path) -> None:
    bundle_path = tmp_path / "courses-2026-9-NB.cache.json"
    bundle_path.write_text(
        '{"meta":{"max_age":-1,"fetched_at":"2026-01-01T00:00:00+00:00"},'
        '"payload":[{"courseString":"14:332:331"}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = SocClient(cache_dir=tmp_path, http_client=http_client)
        courses, info = client.get_courses(Term(2026, "fall"), "NB")

    assert courses[0]["courseString"] == "14:332:331"
    assert info.source == "stale_cache"
    assert info.stale is True


@pytest.mark.parametrize(
    ("semester", "expected"),
    [
        ("Spring 2027", Term(year=2027, name="spring")),
        ("winter 2027", Term(year=2027, name="winter")),
        ("02027", Term(year=2027, name="winter")),
        ("Fall  2026", Term(year=2026, name="fall")),
    ],
)
def test_resolve_semester(semester, expected) -> None:
    assert resolve_semester(semester) == expected
    assert resolve_semester(semester).as_dict("NB")["semester"] == (
        f"{expected.code}{expected.year}"
    )


@pytest.mark.parametrize("semester", ["Spring 27", "Sp 2027", "spring", "12127", ""])
def test_resolve_semester_refuses_unclear_input(semester) -> None:
    with pytest.raises(ValueError, match="Spring 2027"):
        resolve_semester(semester)


def test_resolve_semester_defaults_to_current_term() -> None:
    assert resolve_semester(None) == current_term()
