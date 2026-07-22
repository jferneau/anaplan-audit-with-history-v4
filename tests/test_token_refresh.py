"""Tests for proactive token refresh in AuthToken and APIClient."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import respx

from anaplan_audit.api.client import APIClient
from anaplan_audit.auth.models import AuthToken

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token(*, minutes_until_expiry: float = 60) -> AuthToken:
    """Return an AuthToken expiring in the given number of minutes."""
    return AuthToken(
        access_token="test-token",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=minutes_until_expiry),
    )


def _fresh_token() -> AuthToken:
    """Return a new token representing a refreshed credential."""
    return AuthToken(
        access_token="refreshed-token",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=35),
    )


# ---------------------------------------------------------------------------
# AuthToken.is_near_expiry
# ---------------------------------------------------------------------------


class TestIsNearExpiry:
    def test_far_future_not_near_expiry(self) -> None:
        token = _token(minutes_until_expiry=60)
        assert not token.is_near_expiry()

    def test_exactly_at_margin_is_near_expiry(self) -> None:
        # Token expires in exactly REFRESH_MARGIN_MINUTES — should be near expiry.
        token = _token(minutes_until_expiry=AuthToken.REFRESH_MARGIN_MINUTES)
        assert token.is_near_expiry()

    def test_inside_margin_is_near_expiry(self) -> None:
        token = _token(minutes_until_expiry=2)
        assert token.is_near_expiry()

    def test_already_expired_is_near_expiry(self) -> None:
        token = _token(minutes_until_expiry=-1)
        assert token.is_near_expiry()

    def test_custom_margin_respected(self) -> None:
        # Token expires in 10 minutes; with a 15-minute custom margin it's near expiry.
        token = _token(minutes_until_expiry=10)
        assert token.is_near_expiry(margin_minutes=15)
        assert not token.is_near_expiry(margin_minutes=5)

    def test_default_margin_is_five_minutes(self) -> None:
        assert AuthToken.REFRESH_MARGIN_MINUTES == 5


# ---------------------------------------------------------------------------
# APIClient._maybe_refresh_token
# ---------------------------------------------------------------------------


class TestMaybeRefreshToken:
    def test_no_factory_skips_refresh(self) -> None:
        """When no token_factory is provided, _maybe_refresh_token is a no-op."""
        near_expiry_token = _token(minutes_until_expiry=1)
        client = APIClient(near_expiry_token)
        original_token = client._token

        client._maybe_refresh_token()

        assert client._token is original_token

    def test_healthy_token_skips_factory(self) -> None:
        """Factory must NOT be called when the token is not near expiry."""
        factory = MagicMock(return_value=_fresh_token())
        client = APIClient(_token(minutes_until_expiry=30), token_factory=factory)

        client._maybe_refresh_token()

        factory.assert_not_called()
        assert client._token.access_token == "test-token"

    def test_near_expiry_calls_factory(self) -> None:
        """Factory IS called when token is within the refresh margin."""
        new_token = _fresh_token()
        factory = MagicMock(return_value=new_token)
        client = APIClient(_token(minutes_until_expiry=1), token_factory=factory)

        client._maybe_refresh_token()

        factory.assert_called_once()
        assert client._token is new_token
        assert client._client.headers["Authorization"] == "AnaplanAuthToken refreshed-token"

    def test_factory_failure_keeps_original_token(self) -> None:
        """If the factory raises, the existing token is kept and no exception propagates."""
        original = _token(minutes_until_expiry=1)
        factory = MagicMock(side_effect=RuntimeError("auth service down"))
        client = APIClient(original, token_factory=factory)

        # Must not raise.
        client._maybe_refresh_token()

        assert client._token is original

    def test_refresh_called_before_request(self) -> None:
        """_maybe_refresh_token must be invoked on every request()."""
        new_token = _fresh_token()
        factory = MagicMock(return_value=new_token)
        near_expiry = _token(minutes_until_expiry=1)

        with respx.mock:
            respx.get("https://api.test.com/data").mock(return_value=httpx.Response(200, json={}))
            with APIClient(near_expiry, token_factory=factory) as client:
                client.get("https://api.test.com/data")

        factory.assert_called_once()
        assert client._token is new_token


# ---------------------------------------------------------------------------
# Double-checked locking — concurrent refresh
# ---------------------------------------------------------------------------


class TestDoubleCheckedLock:
    def test_only_one_refresh_when_threads_race(self) -> None:
        """When many threads call _maybe_refresh_token simultaneously, the
        factory should be invoked exactly once."""
        call_count = 0
        barrier = threading.Barrier(10)

        def factory() -> AuthToken:
            nonlocal call_count
            call_count += 1
            return _fresh_token()

        client = APIClient(_token(minutes_until_expiry=1), token_factory=factory)

        def worker() -> None:
            barrier.wait()  # Synchronize all threads to maximise race.
            client._maybe_refresh_token()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert call_count == 1
        assert client._token.access_token == "refreshed-token"
