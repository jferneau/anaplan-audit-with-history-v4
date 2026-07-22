"""Authentication token model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class AuthToken:
    """Immutable authentication token with expiry tracking.

    Anaplan auth tokens are valid for 35 minutes regardless of auth mode
    (Basic, CA Cert, or OAuth).  The client proactively refreshes when fewer
    than ``REFRESH_MARGIN_MINUTES`` remain to avoid mid-request expiry.

    Attributes:
        access_token: The Anaplan access token string.
        expires_at: UTC expiry timestamp.
        refresh_token: OAuth refresh token, if applicable.
    """

    #: Anaplan tokens are valid for this long, in every auth mode.
    TOKEN_LIFETIME_MINUTES: ClassVar[int] = 35

    #: Refresh this many minutes before the token actually expires.
    REFRESH_MARGIN_MINUTES: ClassVar[int] = 5

    access_token: str
    expires_at: datetime
    refresh_token: str | None = field(default=None)

    @property
    def is_expired(self) -> bool:
        """Return *True* if the token has already expired."""
        return datetime.now(tz=UTC) >= self.expires_at

    def is_near_expiry(self, margin_minutes: int | None = None) -> bool:
        """Return *True* if the token expires within *margin_minutes*.

        Args:
            margin_minutes: Minutes before expiry to treat as near-expiry.
                Defaults to :attr:`REFRESH_MARGIN_MINUTES`.
        """
        margin = margin_minutes if margin_minutes is not None else self.REFRESH_MARGIN_MINUTES
        return datetime.now(tz=UTC) >= (self.expires_at - timedelta(minutes=margin))

    def auth_header(self) -> dict[str, str]:
        """Return an Authorization header dict for Anaplan APIs."""
        return {"Authorization": f"AnaplanAuthToken {self.access_token}"}
