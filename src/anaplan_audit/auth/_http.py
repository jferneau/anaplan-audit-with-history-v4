"""Shared HTTP plumbing for the authentication flows.

Each auth mode makes one or two unauthenticated POSTs before an
:class:`~anaplan_audit.api.client.APIClient` exists.  This helper owns the
client construction and the httpx-error → typed-exception translation so
the per-mode modules only describe their payloads.
"""

from __future__ import annotations

from typing import Any

import httpx

from anaplan_audit.exceptions import AuthError


def auth_post(
    url: str,
    *,
    error_cls: type[AuthError],
    error_label: str,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    json: Any | None = None,
) -> httpx.Response:
    """POST to an auth endpoint, translating failures to a typed error.

    Args:
        url: The auth endpoint URL.
        error_cls: The :class:`AuthError` subclass to raise on failure.
        error_label: Human-readable flow name used in error messages
            (e.g. ``"Basic auth"``).
        headers: Request headers.
        data: Form-encoded body.
        json: JSON body.

    Returns:
        The successful (2xx) response.

    Raises:
        error_cls: On HTTP status or transport errors.
    """
    try:
        with httpx.Client(http2=True, timeout=60.0) as client:
            resp = client.post(url, headers=headers, data=data, json=json)
            resp.raise_for_status()
            return resp
    except httpx.HTTPStatusError as exc:
        raise error_cls(
            f"{error_label} failed: HTTP {exc.response.status_code}",
            context={"status_code": exc.response.status_code},
        ) from exc
    except httpx.HTTPError as exc:
        raise error_cls(
            f"{error_label} request error: {exc}",
            context={"error": str(exc)},
        ) from exc
