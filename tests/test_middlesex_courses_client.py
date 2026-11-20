from datetime import datetime

import httpx
import pytest

from middlesex_courses_mcp.client import (
    CATALOG_URL,
    OPTIONS_URL,
    SEARCH_URL,
    SECTION_DETAILS_URL,
    MiddlesexCatalogClient,
    normalize_section_code,
    resolve_days,
    resolve_option,
    resolve_term,
)

OPTIONS = {
    "Subjects": [
        {"Code": "CSC", "Description": "Computer Science", "ShowInCourseSearch": True},
        {"Code": "MAT", "Description": "Mathematics", "ShowInCourseSearch": True},
    ],
    "Terms": [
        {"Item1": "26/SP", "Item2": "Spring 2026"},
        {"Item1": "26/FA", "Item2": "Fall 2026"},
    ],
    "Locations": [
        {"Item1": "MC", "Item2": "Main Campus - Edison"},
        {"Item1": "NET", "Item2": "Online Course"},
    ],
}

SECTION = {
    "Id": "188940",
    "SectionNameDisplay": "CSC-105-01",
    "SectionTitleDisplay": "Computer Applications & System",
    "CourseName": "CSC-105",
    "Number": "01",
    "TermId": "26/FA",
    "TermDisplay": "Fall 2026",
    "AvailabilityStatus": "Open",
    "AvailabilityStatusDisplay": "Open",
    "Available": 3,
    "Capacity": 24,
    "Enrolled": 21,
    "Waitlisted": 0,
    "WaitlistAvailable": False,
    "AreSeatCountsAvailable": True,
    "AvailabilityDisplay": "3 / 24 / 0",
    "MinimumCredits": 3.0,
    "LocationCode": "MC",
    "LocationDisplay": "Main Campus - Edison",
    "FacultyDisplay": ["Teacher, A"],
    "AcademicLevel": "Undergraduate",
    "FormattedMeetingTimes": [
        {
            "Days": [1, 3],
            "DaysOfWeekDisplay": "M/W",
            "StartTime": "09:00:00",
            "EndTime": "10:15:00",
            "StartTimeDisplay": "9:00 AM",
            "EndTimeDisplay": "10:15 AM",
            "InstructionalMethodCode": "LEC",
            "InstructionalMethodDisplay": "Lecture",
            "BuildingDisplay": "Crabiel Hall",
            "RoomDisplay": "101",
            "IsOnline": False,
        }
    ],
    "Course": {
        "SubjectCode": "CSC",
        "Number": "105",
        "Description": "Introduces computer applications.",
    },
    "Term": {
        "Code": "26/FA",
        "Description": "Fall 2026",
        "StartDate": "2026-09-08T00:00:00-04:00",
        "EndDate": "2026-12-18T00:00:00-05:00",
    },
}


def test_normalize_section_code() -> None:
    assert normalize_section_code("csc-105-01") == ("CSC", "105", "01")
    assert normalize_section_code("CSC 105") == ("CSC", "105", None)
    assert normalize_section_code("computer applications") is None


def test_resolve_options_and_days() -> None:
    assert resolve_option("computer science", OPTIONS["Subjects"], "subject") == "CSC"
    assert resolve_option("NET", OPTIONS["Locations"], "location") == "NET"
    assert resolve_days(["Monday", "Wed", "Monday"]) == ["1", "3"]


def test_resolve_default_term() -> None:
    assert resolve_term(None, OPTIONS["Terms"], now=datetime(2026, 9, 2)) == (
        "26/FA",
        "Fall 2026",
    )


def test_client_bootstraps_and_searches() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == CATALOG_URL:
            return httpx.Response(
                200,
                text='<input name="__RequestVerificationToken" value="test-token">',
                headers={"Set-Cookie": ".ColleagueSelfServiceAntiforgery=test-cookie; Path=/"},
            )
        if str(request.url) == OPTIONS_URL:
            return httpx.Response(200, json=OPTIONS)
        if str(request.url) == SEARCH_URL:
            assert request.headers["__RequestVerificationToken"] == "test-token"
            assert request.headers["__IsGuestUser"] == "true"
            payload = __import__("json").loads(request.content)
            assert payload["terms"] == ["26/FA"]
            assert payload["keywordComponents"] == [
                {"synonym": "", "subject": "CSC", "courseNumber": "105", "section": ""}
            ]
            assert payload["locations"] == ["NET"]
            assert payload["days"] == ["1"]
            assert payload["openSections"] is True
            return httpx.Response(
                200,
                json={
                    "Sections": [SECTION],
                    "TotalItems": 1,
                    "TotalPages": 1,
                    "CurrentPageIndex": 1,
                    "ErrorMessage": "",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = MiddlesexCatalogClient(http_client=http_client)
        result = client.search_classes(
            term="Fall 2026",
            subject="CSC",
            course_number="105",
            locations=["Online Course"],
            days=["Monday"],
            open_only=True,
        )

    assert result["total_matches"] == 1
    assert result["classes"][0]["section_code"] == "CSC-105-01"
    assert result["classes"][0]["meetings"][0]["building"] == "Crabiel Hall"
    assert [request.url.path for request in requests] == [
        "/Student/Courses",
        "/Student/Courses/GetCatalogAdvancedSearch",
        "/Student/Courses/PostSearchCriteria",
    ]


def test_client_gets_class_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == CATALOG_URL:
            return httpx.Response(
                200, text='<input name="__RequestVerificationToken" value="test-token">'
            )
        assert str(request.url) == SECTION_DETAILS_URL
        return httpx.Response(
            200,
            json={
                "Id": "188940",
                "CourseName": "CSC-105",
                "Number": "01",
                "Title": "Computer Applications & System",
                "TermId": "26/FA",
                "TermDisplay": "Fall 2026",
                "AvailabilityStatus": "Open",
                "RequisiteItems": [
                    {
                        "DisplayText": "Placement into college-level mathematics",
                        "DisplayTextExtension": "- Must be completed prior to taking this course.",
                        "IsRequired": True,
                    }
                ],
                "Charges": [{"Description": "Course fee:", "DisplayAmount": "$25.00"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = MiddlesexCatalogClient(http_client=http_client).get_class_details("188940")

    assert result["section_code"] == "CSC-105-01"
    assert result["requisites"][0]["required"] is True
    assert result["charges"][0]["amount"] == "$25.00"


def test_get_class_details_rejects_non_numeric_id() -> None:
    with pytest.raises(ValueError, match="numeric ID"):
        MiddlesexCatalogClient(http_client=httpx.Client()).get_class_details("CSC-105-01")
