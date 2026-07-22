"""OAuth device-grant flow with refresh-token rotation."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from anaplan_audit.auth._http import auth_post
from anaplan_audit.auth.models import AuthToken
from anaplan_audit.auth.token_store import TokenStore
from anaplan_audit.config import AnaplanUris
from anaplan_audit.exceptions import DeviceRegistrationError, RefreshTokenError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def register_device(
    client_id: str,
    uris: AnaplanUris,
    store: TokenStore,
) -> None:
    """Run the OAuth device-grant registration flow.

    Displays a user code and polls until the user completes browser auth.
    Persists the refresh token encrypted via *store*.

    Args:
        client_id: The OAuth client ID.
        uris: API base URIs.
        store: Encrypted token storage.

    Raises:
        DeviceRegistrationError: If the device registration flow fails.
    """
    try:
        with httpx.Client(http2=True, timeout=60.0) as http:
            # Request device code
            resp = http.post(
                f"{uris.oauthServiceUri}/device/code",
                data={"client_id": client_id, "scope": "openid"},
            )
            resp.raise_for_status()
            device_data = resp.json()

            user_code = device_data["user_code"]
            verification_uri = device_data["verification_uri"]
            device_code = device_data["device_code"]
            interval = device_data.get("interval", 5)

            logger.info(
                "oauth_device_registration",
                user_code=user_code,
                verification_uri=verification_uri,
            )
            print(f"\nGo to {verification_uri} and enter code: {user_code}\n")

            # Poll for completion
            while True:
                time.sleep(interval)
                token_resp = http.post(
                    f"{uris.oauthServiceUri}/token",
                    data={
                        "client_id": client_id,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
                if token_resp.status_code == 200:
                    token_data = token_resp.json()
                    refresh_token = token_data["refresh_token"]
                    store.put(client_id, refresh_token)
                    logger.info("oauth_device_registration_complete")
                    return
                error = token_resp.json().get("error", "")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                raise DeviceRegistrationError(
                    f"Device registration failed: {error}",
                    context={"error": error},
                )
    except DeviceRegistrationError:
        raise
    except Exception as exc:
        raise DeviceRegistrationError(
            f"Device registration error: {exc}",
            context={"error": str(exc)},
        ) from exc


def refresh_access_token(
    client_id: str,
    uris: AnaplanUris,
    store: TokenStore,
    *,
    rotatable: bool,
) -> AuthToken:
    """Exchange a stored refresh token for a new access token.

    Args:
        client_id: The OAuth client ID.
        uris: API base URIs.
        store: Encrypted token storage.
        rotatable: When *True*, the refresh token is rotated and persisted.

    Returns:
        A valid :class:`AuthToken`.

    Raises:
        RefreshTokenError: If token refresh fails.
    """
    refresh_token = store.get(client_id)
    if refresh_token is None:
        raise RefreshTokenError(
            "No stored refresh token. Run 'register' first.",
            context={"client_id": client_id},
        )

    resp = auth_post(
        f"{uris.oauthServiceUri}/token",
        error_cls=RefreshTokenError,
        error_label="Token refresh",
        data={
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )

    token_data = resp.json()
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 1800)
    new_refresh = token_data.get("refresh_token", refresh_token)

    if rotatable and new_refresh != refresh_token:
        store.put(client_id, new_refresh)
        logger.info("oauth_refresh_token_rotated")

    logger.info("oauth_access_token_refreshed")

    return AuthToken(
        access_token=access_token,
        expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in),
        refresh_token=new_refresh,
    )
