"""Anaplan Transactional API client — lists, modules, line items, cell writes.

Distinct from the Bulk Integration API in ``integration.py`` — the
Transactional API operates directly on model content (list items, module
cells) instead of files/imports/processes. Used by the refresh-log path
to append a run entry to the ``BATCH_ID`` list and populate the two
Refresh Log module cells without needing an import mapping inside a
process.
"""

from __future__ import annotations

from typing import Any

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import AnaplanList, LineItem, Module
from anaplan_audit.exceptions import UnexpectedResponseError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def _extract_failure_reasons(failures: list[Any]) -> list[str]:
    """Pull human-readable reasons out of Anaplan's per-item failure dicts.

    Anaplan is inconsistent about which key carries the message —
    depending on endpoint you'll see ``reason``, ``failureReason``,
    ``failureMessageDetails``, or a nested ``status.message``. Try each
    in order and return whatever's set, so callers see *why* even when
    the response shape drifts.
    """
    reasons: list[str] = []
    for f in failures:
        if not isinstance(f, dict):
            continue
        r = (
            f.get("reason")
            or f.get("failureReason")
            or f.get("failureMessageDetails")
            or f.get("failureType")
            or (f.get("status", {}) if isinstance(f.get("status"), dict) else {}).get("message")
            or ""
        )
        if r:
            reasons.append(str(r))
    return reasons


def list_lists(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[AnaplanList]:
    """List all lists in a model."""
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/lists")
    data = resp.json()
    return [AnaplanList.model_validate(item) for item in data.get("lists", [])]


def list_modules(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Module]:
    """List all modules in a model."""
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/modules")
    data = resp.json()
    return [Module.model_validate(m) for m in data.get("modules", [])]


def list_module_line_items(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    module_id: str,
) -> list[LineItem]:
    """List all line items in a module."""
    resp = client.get(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/modules/{module_id}/lineItems"
    )
    data = resp.json()
    return [LineItem.model_validate(li) for li in data.get("items", [])]


def get_list_item_identifiers(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    list_id: str,
) -> set[str]:
    """Return the set of existing item identifiers (codes plus names).

    Anaplan enforces uniqueness on **both** the ``code`` and ``name``
    columns of a list independently, so a caller trying to add an item
    idempotently has to check both. Reporting-model imports (e.g. the
    saved-view import that populates ``EVENT_ID``) frequently produce
    items where ``code`` differs from ``name`` — an observed value
    absent from the code set may still collide with an existing name.
    Returning the union of the two lets the caller skip anything that
    would collide on either side.

    Uses ``?includeAll=true`` so a single response covers every item;
    if Anaplan returns a paged response the first page is used and a
    warning is emitted so the caller knows coverage was incomplete.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        list_id: Target list ID.

    Returns:
        A ``set`` containing every non-empty ``code`` and ``name`` from
        the list.
    """
    url = (
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/lists/{list_id}/items?includeAll=true"
    )
    resp = client.get(url)
    data = resp.json()
    items = data.get("listItems", data.get("items", []))
    identifiers: set[str] = set()
    for item in items:
        for key in ("code", "name"):
            v = item.get(key)
            if v not in (None, ""):
                identifiers.add(str(v))
    next_url = data.get("meta", {}).get("paging", {}).get("nextUrl")
    if next_url:
        logger.warning(
            "list_items_paged_response",
            list_id=list_id,
            note="Anaplan returned a paged response; only page 1 was consumed.",
        )
    logger.debug(
        "list_item_identifiers_fetched",
        list_id=list_id,
        identifier_count=len(identifiers),
    )
    return identifiers


def add_list_items(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    list_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add items to a list via the Transactional API.

    Each item is a dict with at least a ``code`` (unique key). The
    ``name`` defaults to the code when omitted — good enough for
    machine-managed lists like ``BATCH_ID``.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        list_id: Target list ID.
        items: List of item dicts, e.g. ``[{"code": "1720543095"}]``.

    Returns:
        The parsed response dict — includes ``added``, ``ignored``,
        ``total``, and any per-item ``failures``.

    Raises:
        UnexpectedResponseError: If any item failed to add.
    """
    url = (
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/lists/{list_id}/items?action=add"
    )
    resp = client.post(url, json={"items": items})
    data: dict[str, Any] = resp.json()

    failures = data.get("failures", []) or []
    if failures:
        reasons = _extract_failure_reasons(failures)
        first_reason = reasons[0] if reasons else ""
        # Log the raw failure dicts (bounded to first 5) at WARNING so
        # the operator can see the shape Anaplan returned even if the
        # exception context is dropped by the caller's error handler.
        logger.warning(
            "list_items_add_failed",
            list_id=list_id,
            item_count=len(items),
            failure_count=len(failures),
            failures=failures[:5],
            first_item_sent=items[0] if items else None,
        )
        raise UnexpectedResponseError(
            f"Anaplan add-list-items failed for {len(failures)} of {len(items)} items"
            + (f": {first_reason}" if first_reason else ""),
            context={"list_id": list_id, "failures": failures[:5]},
        )

    logger.info(
        "list_items_added",
        list_id=list_id,
        added=data.get("added", 0),
        ignored=data.get("ignored", 0),
        total=data.get("total", len(items)),
    )
    return data


def write_module_cells(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    module_id: str,
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write cell values to a module via the Transactional API.

    Each cell payload has:

    * ``lineItemId`` — the target line item ID.
    * ``dimensions`` — list of ``{"dimensionId": <listId>, "itemCode": <code>}``
      entries locating the cell across the module's dimensions.
    * ``value`` — the value to write (str/int/float/bool depending on
      the line item's data type).

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        module_id: Target module ID.
        cells: The cell payloads described above.

    Returns:
        The parsed response dict — includes per-cell ``failures``.

    Raises:
        UnexpectedResponseError: If any cell failed to write.
    """
    url = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/modules/{module_id}/data"
    resp = client.post(url, json=cells)
    data: dict[str, Any] = resp.json()

    failures = data.get("failures", []) or []
    if failures:
        reasons = _extract_failure_reasons(failures)
        first_reason = reasons[0] if reasons else ""
        logger.warning(
            "module_cells_write_failed",
            module_id=module_id,
            cell_count=len(cells),
            failure_count=len(failures),
            failures=failures[:5],
            first_cell_sent=cells[0] if cells else None,
        )
        raise UnexpectedResponseError(
            f"Anaplan module-cell write failed for {len(failures)} of {len(cells)} cells"
            + (f": {first_reason}" if first_reason else ""),
            context={"module_id": module_id, "failures": failures[:5]},
        )

    logger.info(
        "module_cells_written",
        module_id=module_id,
        cell_count=len(cells),
    )
    return data
