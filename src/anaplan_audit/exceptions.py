"""Typed exception hierarchy with distinct exit codes.

Every leaf exception maps to a process exit code used by the CLI.
All exceptions accept an optional ``context`` dict that gets merged
into the structlog event when logged.
"""

from __future__ import annotations

from typing import Any


class AnaplanAuditError(Exception):
    """Base exception for the anaplan-audit package. Never raised directly."""

    exit_code: int = 1

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = context or {}


# --- Config ---


class ConfigError(AnaplanAuditError):
    """Invalid or missing configuration. Exit code 2."""

    exit_code = 2


# --- Auth ---


class AuthError(AnaplanAuditError):
    """Authentication failure. Exit code 3."""

    exit_code = 3


class BasicAuthError(AuthError):
    """Basic (username/password) authentication failed."""


class CertAuthError(AuthError):
    """Certificate-based authentication failed."""


class OAuthError(AuthError):
    """OAuth flow failure."""


class DeviceRegistrationError(OAuthError):
    """OAuth device registration flow failed."""


class RefreshTokenError(OAuthError):
    """OAuth refresh-token exchange failed."""


# --- API ---


class APIError(AnaplanAuditError):
    """API call failure. Exit code 4."""

    exit_code = 4


class RateLimitError(APIError):
    """HTTP 429 — rate limited.

    Attributes:
        retry_after: Value of the Retry-After header, if present.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.retry_after = retry_after


class UpstreamError(APIError):
    """HTTP 5xx from upstream Anaplan service."""


class UnexpectedResponseError(APIError):
    """Response did not match expected schema."""


# --- Transform ---


class TransformError(AnaplanAuditError):
    """Data transformation failure. Exit code 5."""

    exit_code = 5


class StorageLoadError(TransformError):
    """Failed to load data into the local database (DuckDB)."""


class QueryExecutionError(TransformError):
    """audit_query.sql execution failed."""


# --- Model History ---


class ModelHistoryError(AnaplanAuditError):
    """Model history feature failure. Exit code 6.

    Raised internally but always caught at the orchestrator boundary so that
    model history failures never crash the audit run.
    """

    exit_code = 6


# --- Concurrency ---


class RunLockError(AnaplanAuditError):
    """Another instance of the tool is already running. Exit code 7.

    The tool writes an exclusive lock file next to the database at
    startup.  If the lock cannot be acquired the process exits immediately
    rather than corrupting shared state.
    """

    exit_code = 7
