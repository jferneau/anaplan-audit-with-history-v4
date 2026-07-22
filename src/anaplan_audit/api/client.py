"""HTTP client wrapper with retry logic (tenacity + httpx) and token refresh.

Every API function goes through this client, inheriting:
- Exponential backoff with jitter on 429/5xx/network errors (tenacity)
- Proactive token refresh when the Anaplan auth token is near expiry

Anaplan auth tokens live for exactly 35 minutes regardless of auth mode.
The client refreshes 5 minutes early so no request ever fires with a stale token.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from anaplan_audit.auth.models import AuthToken
from anaplan_audit.exceptions import (
    RateLimitError,
    UpstreamError,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    """Determine whether an exception should trigger a retry."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, (UpstreamError, RateLimitError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return False


def _before_sleep(retry_state: RetryCallState) -> None:
    """Log a warning before each retry sleep."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "http_retry",
        attempt=retry_state.attempt_number,
        error=str(exc),
    )


_base_wait = wait_exponential_jitter(initial=1, max=16)


def _wait_honoring_retry_after(retry_state: RetryCallState) -> float:
    """Exponential backoff with jitter, floored by any Retry-After header.

    When Anaplan returns 429 with a Retry-After value, waiting less than
    that value guarantees another 429 — so the server-provided value acts
    as a lower bound on the computed wait.
    """
    wait = _base_wait(retry_state)
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RateLimitError) and exc.retry_after is not None:
        wait = max(wait, exc.retry_after)
    return wait


_retry_policy = retry(
    stop=stop_after_attempt(5),
    wait=_wait_honoring_retry_after,
    retry=retry_if_exception(_is_retryable),
    before_sleep=_before_sleep,
    reraise=True,
)


class APIClient:
    """Thin wrapper around :class:`httpx.Client` with auth, retries, and token refresh.

    Token refresh
    ~~~~~~~~~~~~~
    ``token_factory`` is an optional callable that returns a fresh
    :class:`~anaplan_audit.auth.models.AuthToken`.  Before every request the
    client checks whether the current token is within
    :attr:`~anaplan_audit.auth.models.AuthToken.REFRESH_MARGIN_MINUTES` of
    expiry.  If so it calls ``token_factory()`` under a lock (double-checked
    to avoid redundant refreshes from concurrent threads) and updates the
    Authorization header on the underlying httpx client.

    Thread safety
    ~~~~~~~~~~~~~
    :class:`httpx.Client` is safe to share across threads.  The token refresh
    uses a :class:`threading.Lock` so that at most one thread at a time
    exchanges the stale token — subsequent threads see the freshly-written
    token and skip the refresh.

    Args:
        token: An :class:`AuthToken` injected into every request.
        base_headers: Additional headers merged into every request.
        token_factory: Optional callable that returns a fresh token.
            When *None*, no proactive refresh is performed.
    """

    def __init__(
        self,
        token: AuthToken,
        *,
        base_headers: dict[str, str] | None = None,
        token_factory: Callable[[], AuthToken] | None = None,
    ) -> None:
        self._token = token
        self._token_factory = token_factory
        self._refresh_lock = threading.Lock()

        headers = {
            **token.auth_header(),
            "Content-Type": "application/json",
            **(base_headers or {}),
        }
        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=30.0),
            http2=True,
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> APIClient:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context manager."""
        self.close()

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    def _maybe_refresh_token(self) -> None:
        """Proactively refresh the access token if it is near expiry.

        Uses double-checked locking so that when multiple threads race to
        refresh, only the first one actually calls the factory — the rest
        observe the already-refreshed token and return immediately.
        """
        if self._token_factory is None:
            return
        if not self._token.is_near_expiry():
            return

        with self._refresh_lock:
            # Second check — another thread may have refreshed while we waited.
            if not self._token.is_near_expiry():
                return

            try:
                new_token = self._token_factory()
            except Exception as exc:
                # Log and continue with the existing token rather than crashing.
                logger.warning("token_refresh_failed", error=str(exc))
                return

            self._token = new_token
            # Update the default Authorization header on the shared httpx client.
            self._client.headers["Authorization"] = new_token.auth_header()["Authorization"]
            logger.info(
                "token_refreshed_proactively",
                expires_at=new_token.expires_at.isoformat(),
            )

    # ------------------------------------------------------------------
    # HTTP verbs
    # ------------------------------------------------------------------

    @_retry_policy
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request with retry logic and token refresh.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            url: Full URL.
            params: Query parameters.
            json: JSON body.
            data: Raw body data.
            headers: Per-request headers.

        Returns:
            The :class:`httpx.Response`.

        Raises:
            RateLimitError: On HTTP 429 after retries exhausted.
            UpstreamError: On HTTP 5xx after retries exhausted.
            UnexpectedResponseError: On other non-2xx status codes.
        """
        self._maybe_refresh_token()

        start = time.monotonic()
        resp = self._client.request(
            method,
            url,
            params=params,
            json=json,
            content=data,
            headers=headers,
        )
        duration_ms = (time.monotonic() - start) * 1000

        logger.debug(
            "http_response",
            method=method,
            url=url,
            status=resp.status_code,
            duration_ms=round(duration_ms, 1),
        )

        if resp.status_code >= 400:
            self._raise_for_status(resp)

        return resp

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute HTTP GET."""
        return self.request("GET", url, params=params, json=json, data=data, headers=headers)

    def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute HTTP POST."""
        return self.request("POST", url, params=params, json=json, data=data, headers=headers)

    def put(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute HTTP PUT."""
        return self.request("PUT", url, params=params, json=json, data=data, headers=headers)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Raise the appropriate typed exception for a non-2xx response.

        Args:
            resp: The HTTP response.

        Raises:
            RateLimitError: On HTTP 429.
            UpstreamError: On HTTP 5xx.
            httpx.HTTPStatusError: On other 4xx/5xx codes.
        """
        status = resp.status_code
        body = resp.text[:500]
        ctx: dict[str, Any] = {"status_code": status, "body": body}

        if status == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            retry_after = float(retry_after_raw) if retry_after_raw else None
            raise RateLimitError(
                f"Rate limited (429): {body}",
                retry_after=retry_after,
                context=ctx,
            )
        if status >= 500:
            raise UpstreamError(f"Upstream error ({status}): {body}", context=ctx)

        resp.raise_for_status()
