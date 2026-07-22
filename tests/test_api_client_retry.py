"""Tests for the API client retry logic — the headline reliability fix."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from anaplan_audit.api.client import APIClient
from anaplan_audit.auth.models import AuthToken
from anaplan_audit.exceptions import RateLimitError, UpstreamError


@pytest.fixture()
def auth_token() -> AuthToken:
    """A non-expired auth token for testing."""
    return AuthToken(
        access_token="test-token",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


class TestRetryPolicy:
    """Test the tenacity retry policy on the API client."""

    def test_503_then_200_succeeds(self, auth_token: AuthToken) -> None:
        """Three 503s followed by a 200 should succeed with 4 total requests."""
        with respx.mock:
            route = respx.get("https://api.test.com/data").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(503),
                    httpx.Response(503),
                    httpx.Response(200, json={"ok": True}),
                ]
            )
            with APIClient(auth_token) as client:
                resp = client.get("https://api.test.com/data")
                assert resp.status_code == 200
                assert route.call_count == 4

    def test_all_503_raises_upstream_error(self, auth_token: AuthToken) -> None:
        """Five consecutive 503s exhaust retries and raise UpstreamError."""
        with respx.mock:
            respx.get("https://api.test.com/data").mock(return_value=httpx.Response(503))
            with APIClient(auth_token) as client, pytest.raises(UpstreamError):
                client.get("https://api.test.com/data")

    def test_429_raises_rate_limit_error(self, auth_token: AuthToken) -> None:
        """HTTP 429 with Retry-After raises RateLimitError after retries."""
        with respx.mock:
            respx.get("https://api.test.com/data").mock(
                return_value=httpx.Response(429, headers={"Retry-After": "2"})
            )
            with APIClient(auth_token) as client:
                with pytest.raises(RateLimitError) as exc_info:
                    client.get("https://api.test.com/data")
                assert exc_info.value.retry_after == 2.0

    def test_401_not_retried(self, auth_token: AuthToken) -> None:
        """HTTP 401 should NOT be retried — should fail immediately."""
        with respx.mock:
            route = respx.get("https://api.test.com/data").mock(
                return_value=httpx.Response(401, json={"error": "Unauthorized"})
            )
            with APIClient(auth_token) as client:
                with pytest.raises(httpx.HTTPStatusError):
                    client.get("https://api.test.com/data")
                assert route.call_count == 1
