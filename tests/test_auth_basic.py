"""Tests for basic authentication."""

from __future__ import annotations

import httpx
import pytest
import respx

from anaplan_audit.auth.basic import authenticate_basic
from anaplan_audit.config import AnaplanUris
from anaplan_audit.exceptions import BasicAuthError


class TestBasicAuth:
    """Test basic (username/password) authentication."""

    def test_success(self) -> None:
        """Successful basic auth returns a valid token."""
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")
        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "tokenInfo": {
                            "tokenValue": "test-token-123",
                            "expiresAt": 1700000000,
                        }
                    },
                )
            )
            token = authenticate_basic("user@test.com", "password", uris)
            assert token.access_token == "test-token-123"
            assert not token.is_expired

    def test_401_raises_basic_auth_error(self) -> None:
        """HTTP 401 raises BasicAuthError."""
        uris = AnaplanUris(authServiceUri="https://mock.anaplan.com/auth")
        with respx.mock:
            respx.post("https://mock.anaplan.com/auth").mock(
                return_value=httpx.Response(401, json={"error": "Unauthorized"})
            )
            with pytest.raises(BasicAuthError):
                authenticate_basic("user@test.com", "wrong", uris)
