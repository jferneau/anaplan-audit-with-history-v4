"""Anaplan Audit API client.

v4 performance note
-------------------
v3 validated events one at a time inside the pagination loop
(``yield AuditEvent.model_validate(raw)``) and the orchestrator then ran a
second per-object pass (``[e.model_dump() for e in events]``).  Both loops
crossed the Python/pydantic-core boundary once per event — measurable
overhead at tens of thousands of events per run.

v4 accumulates raw dicts across pages and validates the whole batch in one
:class:`pydantic.TypeAdapter` call, staying inside pydantic-core for the
entire list.  :func:`dump_audit_events` is the batch counterpart for the
serialize-back-to-dicts step.  (``model_construct()`` was considered and
rejected: it skips ``BeforeValidator``s entirely, so ``StrCoerce`` wouldn't
run and an un-stringified integer ``id`` would flow into SQL join logic
that assumes string IDs.)
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import TypeAdapter

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import AuditEvent

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# Module-level and reused — constructing a TypeAdapter builds a validator +
# serializer, which is exactly the overhead the batch approach avoids paying
# per call.
_AUDIT_EVENTS_ADAPTER: TypeAdapter[list[AuditEvent]] = TypeAdapter(list[AuditEvent])


def fetch_audit_events(
    client: APIClient,
    audit_uri: str,
    *,
    since_epoch: int,
    batch_size: int,
    max_events: int | None = None,
) -> list[AuditEvent]:
    """Fetch audit events from the Anaplan Audit API.

    Handles pagination automatically; raw payloads are accumulated across
    pages and validated in a single batch (see module docstring).

    Args:
        client: An authenticated :class:`APIClient`.
        audit_uri: Base URI for the Audit API.
        since_epoch: Fetch events since this Unix epoch (seconds).
        batch_size: Number of events to request per page.
        max_events: Stop after collecting this many events.  ``None`` (the
            default) fetches everything.  Used by ``run --limit`` so
            first-time customers can pull a bounded sample.

    Returns:
        The validated :class:`AuditEvent` list.

    Notes:
        The Anaplan Audit API contract (verified against the tenant):

        - ``POST {audit_uri}/events/search?limit=N`` with a JSON body
          ``{"from": <epoch milliseconds>}``.
        - Events are returned under the ``"response"`` key.
        - Pages are followed via ``meta.paging.nextUrl`` (POST to it with
          the same body) until that key is absent.

        ``since_epoch`` is stored in **seconds** (the ``lastRun`` setting),
        but the API's ``from`` filter is **milliseconds**, so it is
        converted here. ``from = 0`` (first run) returns everything within
        Anaplan's ~30-day retention window.
    """
    from_ms = since_epoch * 1000
    body = {"from": from_ms}

    raw_events: list[Any] = []
    capped = False

    # First page carries the limit; subsequent pages come from nextUrl,
    # which embeds its own paging state.
    url: str | None = f"{audit_uri}/events/search?limit={batch_size}"

    while url:
        resp = client.post(url, json=body)
        payload = resp.json()
        events = payload.get("response") or []

        remaining = None if max_events is None else max_events - len(raw_events)
        if remaining is not None and len(events) >= remaining:
            raw_events.extend(events[:remaining])
            capped = True
            break
        raw_events.extend(events)

        # Stop if this page was empty (guards against a misbehaving API that
        # keeps returning a nextUrl with no data).
        if not events:
            break

        url = payload.get("meta", {}).get("paging", {}).get("nextUrl")

    if capped:
        logger.info(
            "audit_events_fetch_capped",
            total_count=len(raw_events),
            max_events=max_events,
        )

    validated = _AUDIT_EVENTS_ADAPTER.validate_python(raw_events)
    logger.info("audit_events_fetched", total_count=len(validated), since_epoch=since_epoch)
    return validated


def dump_audit_events(events: list[AuditEvent]) -> list[dict[str, Any]]:
    """Serialize a validated event list back to plain dicts in one batch.

    The batch counterpart of per-object ``model_dump()`` — one
    pydantic-core call for the whole list.
    """
    dumped: list[dict[str, Any]] = _AUDIT_EVENTS_ADAPTER.dump_python(events)
    return dumped
