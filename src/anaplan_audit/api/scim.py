"""Anaplan SCIM API client — user metadata."""

from __future__ import annotations

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import User

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def list_users(client: APIClient, scim_uri: str) -> list[User]:
    """List all Anaplan users via the SCIM API.

    Handles SCIM pagination automatically.

    Args:
        client: An authenticated :class:`APIClient`.
        scim_uri: Base URI for the SCIM API.

    Returns:
        A list of :class:`User` instances.
    """
    users: list[User] = []
    start_index = 1
    page_size = 100

    while True:
        resp = client.get(
            f"{scim_uri}/Users",
            params={"startIndex": start_index, "count": page_size},
        )
        data = resp.json()
        resources = data.get("Resources", [])

        for raw in resources:
            users.append(User.model_validate(raw))

        total = data.get("totalResults", len(users))
        start_index += len(resources)
        if start_index > total or not resources:
            break

    logger.info("scim_users_fetched", count=len(users))
    return users
