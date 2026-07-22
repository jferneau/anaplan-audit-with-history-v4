"""Anaplan CloudWorks API client — integration metadata."""

from __future__ import annotations

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import CloudWorksIntegration

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def list_integrations(
    client: APIClient,
    cloudworks_uri: str,
) -> list[CloudWorksIntegration]:
    """List all CloudWorks integrations.

    Args:
        client: An authenticated :class:`APIClient`.
        cloudworks_uri: Base URI for the CloudWorks API.

    Returns:
        A list of :class:`CloudWorksIntegration` instances.
    """
    resp = client.get(f"{cloudworks_uri}/integrations")
    data = resp.json()
    items = data.get("integrations", [])
    result = [CloudWorksIntegration.model_validate(i) for i in items]
    logger.info("cloudworks_integrations_fetched", count=len(result))
    return result
