"""Tests for OAuth device-grant flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from anaplan_audit.auth.oauth import refresh_access_token, register_device
from anaplan_audit.auth.token_store import TokenStore
from anaplan_audit.config import AnaplanUris
from anaplan_audit.exceptions import RefreshTokenError


@pytest.fixture()
def token_store(tmp_path: Path) -> TokenStore:
    """Create a temporary token store."""
    return TokenStore(
        db_path=tmp_path / "tokens.db",
        key_path=tmp_path / "token.key",
    )


class TestOAuthRegister:
    """Test OAuth device registration."""

    @patch("time.sleep", return_value=None)
    def test_register_happy_path(self, _mock_sleep: object, token_store: TokenStore) -> None:
        """Device registration completes and stores refresh token."""
        uris = AnaplanUris(oauthServiceUri="https://mock.anaplan.com/oauth")
        with respx.mock:
            respx.post("https://mock.anaplan.com/oauth/device/code").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "user_code": "ABCD-1234",
                        "verification_uri": "https://anaplan.com/verify",
                        "device_code": "device-123",
                        "interval": 1,
                    },
                )
            )
            respx.post("https://mock.anaplan.com/oauth/token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "at-123",
                        "refresh_token": "rt-456",
                        "expires_in": 1800,
                    },
                )
            )
            register_device("client-id", uris, token_store)

        assert token_store.get("client-id") == "rt-456"


class TestOAuthRefresh:
    """Test OAuth token refresh."""

    def test_refresh_success(self, token_store: TokenStore) -> None:
        """Token refresh returns a valid access token."""
        token_store.put("client-id", "stored-rt")
        uris = AnaplanUris(oauthServiceUri="https://mock.anaplan.com/oauth")
        with respx.mock:
            respx.post("https://mock.anaplan.com/oauth/token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "new-at",
                        "refresh_token": "new-rt",
                        "expires_in": 1800,
                    },
                )
            )
            token = refresh_access_token("client-id", uris, token_store, rotatable=True)
            assert token.access_token == "new-at"
            assert token_store.get("client-id") == "new-rt"

    def test_no_stored_token_raises(self, token_store: TokenStore) -> None:
        """Missing stored token raises RefreshTokenError."""
        uris = AnaplanUris()
        with pytest.raises(RefreshTokenError):
            refresh_access_token("no-such-client", uris, token_store, rotatable=False)
