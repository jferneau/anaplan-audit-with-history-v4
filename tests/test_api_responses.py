"""Tests for API client functions using respx mocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from anaplan_audit.api.audit import fetch_audit_events
from anaplan_audit.api.client import APIClient
from anaplan_audit.api.cloudworks import list_integrations
from anaplan_audit.api.scim import list_users
from anaplan_audit.auth.models import AuthToken


def _make_token() -> AuthToken:
    """Return a non-expired test token."""
    return AuthToken(
        access_token="test-token",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


class TestFetchAuditEvents:
    """Tests for fetch_audit_events against the real Anaplan Audit API contract.

    The API is ``POST {uri}/events/search?limit=N`` with body ``{"from": ms}``;
    events live under the ``response`` key; pages are followed via
    ``meta.paging.nextUrl``.
    """

    SEARCH_URL = "https://audit.test.com/audit/api/1/events/search"

    def test_single_page(self) -> None:
        """Single page of results is returned without extra requests."""
        with respx.mock:
            respx.post(url__startswith=self.SEARCH_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": [
                            {"id": "evt-001", "eventTypeId": "CONN-1"},
                            {"id": "evt-002", "eventTypeId": "CONN-2"},
                        ],
                        "meta": {"paging": {"totalSize": 2}},
                    },
                )
            )
            with APIClient(_make_token()) as client:
                events = list(
                    fetch_audit_events(
                        client,
                        "https://audit.test.com/audit/api/1",
                        since_epoch=0,
                        batch_size=100,
                    )
                )
        assert len(events) == 2
        assert events[0].id == "evt-001"

    def test_uses_post_search_with_from_body(self) -> None:
        """The request is POST /events/search with a JSON `from` body (ms)."""
        with respx.mock:
            route = respx.post(url__startswith=self.SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"response": [], "meta": {"paging": {}}})
            )
            with APIClient(_make_token()) as client:
                list(
                    fetch_audit_events(
                        client,
                        "https://audit.test.com/audit/api/1",
                        since_epoch=5,  # seconds
                        batch_size=100,
                    )
                )
        assert route.called
        request = route.calls[0].request
        assert request.method == "POST"
        assert str(request.url).endswith("/events/search?limit=100")
        assert b'"from"' in request.content
        # 5 seconds -> 5000 ms
        assert b"5000" in request.content

    def test_empty_response_returns_no_events(self) -> None:
        """Empty API response yields nothing."""
        with respx.mock:
            respx.post(url__startswith=self.SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"response": [], "meta": {"paging": {}}})
            )
            with APIClient(_make_token()) as client:
                events = list(
                    fetch_audit_events(
                        client,
                        "https://audit.test.com/audit/api/1",
                        since_epoch=0,
                        batch_size=100,
                    )
                )
        assert events == []

    def test_pagination_follows_next_url(self) -> None:
        """Paginator follows meta.paging.nextUrl until it is absent."""
        next_url = "https://audit.test.com/audit/api/1/events/search?offset=2&limit=2"
        page1 = {
            "response": [{"id": "evt-000"}, {"id": "evt-001"}],
            "meta": {"paging": {"nextUrl": next_url}},
        }
        page2 = {"response": [{"id": "evt-002"}], "meta": {"paging": {}}}

        with respx.mock:
            # One route matches the search path for both the initial POST and
            # the follow-up POST to nextUrl; side_effect serves them in order.
            route = respx.post(url__startswith=self.SEARCH_URL).mock(
                side_effect=[
                    httpx.Response(200, json=page1),
                    httpx.Response(200, json=page2),
                ]
            )
            with APIClient(_make_token()) as client:
                events = list(
                    fetch_audit_events(
                        client,
                        "https://audit.test.com/audit/api/1",
                        since_epoch=0,
                        batch_size=2,
                    )
                )
        assert route.call_count == 2
        assert str(route.calls[1].request.url) == next_url
        assert len(events) == 3
        assert events[-1].id == "evt-002"


class TestListUsers:
    """Tests for SCIM list_users pagination."""

    def test_single_page(self) -> None:
        """All users are returned from a single-page response."""
        with respx.mock:
            respx.get("https://scim.test.com/Users").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "totalResults": 2,
                        "Resources": [
                            {"id": "u-001", "userName": "alice@test.com"},
                            {"id": "u-002", "userName": "bob@test.com"},
                        ],
                    },
                )
            )
            with APIClient(_make_token()) as client:
                users = list_users(client, "https://scim.test.com")

        assert len(users) == 2
        assert users[0].userName == "alice@test.com"

    def test_extracts_first_last_name_from_nested_name(self) -> None:
        """firstName/lastName are lifted from SCIM's nested name object;
        a user with no name object degrades to blank rather than raising."""
        with respx.mock:
            respx.get("https://scim.test.com/Users").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "totalResults": 2,
                        "Resources": [
                            {
                                "id": "u-001",
                                "userName": "bill.dowling@anaplan.com",
                                "displayName": "Bill Dowling",
                                "name": {"givenName": "Bill", "familyName": "Dowling"},
                            },
                            {
                                "id": "u-002",
                                "userName": "svc.apikey@anaplan.com",
                                "displayName": "",
                            },
                        ],
                    },
                )
            )
            with APIClient(_make_token()) as client:
                users = list_users(client, "https://scim.test.com")

        assert (users[0].firstName, users[0].lastName) == ("Bill", "Dowling")
        assert (users[1].firstName, users[1].lastName) == ("", "")

    def test_empty_response(self) -> None:
        """Empty SCIM response returns an empty list."""
        with respx.mock:
            respx.get("https://scim.test.com/Users").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "totalResults": 0,
                        "Resources": [],
                    },
                )
            )
            with APIClient(_make_token()) as client:
                users = list_users(client, "https://scim.test.com")

        assert users == []


class TestListIntegrations:
    """Tests for CloudWorks list_integrations."""

    def test_returns_integrations(self) -> None:
        """CloudWorks integrations are parsed correctly."""
        with respx.mock:
            respx.get("https://cw.test.com/integrations").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "integrations": [
                            {
                                "integrationId": "cw-001",
                                "name": "Daily Sync",
                                "type": "S3",
                                "workspaceId": "ws-001",
                                "modelId": "model-001",
                            }
                        ]
                    },
                )
            )
            with APIClient(_make_token()) as client:
                integrations = list_integrations(client, "https://cw.test.com")

        assert len(integrations) == 1
        assert integrations[0].integrationId == "cw-001"

    def test_empty_response(self) -> None:
        """Empty CloudWorks response returns an empty list."""
        with respx.mock:
            respx.get("https://cw.test.com/integrations").mock(
                return_value=httpx.Response(200, json={"integrations": []})
            )
            with APIClient(_make_token()) as client:
                integrations = list_integrations(client, "https://cw.test.com")

        assert integrations == []
