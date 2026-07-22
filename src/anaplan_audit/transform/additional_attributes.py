"""Extract typed fields out of an audit event's ``additionalAttributes``.

Spec assumption vs. reality
---------------------------
The v3 spec (``v3-additionalAttributes-spec.md``) describes a "naive CEF
extension parser" that space-truncates JSON values with embedded spaces
(``"Xperience 2025"``, ``"13 | G&A Expenses"``). That parser does not
exist in this codebase — v3 uses the Anaplan Audit API v1, which returns
each event as a JSON object with ``additionalAttributes`` already a
nested dict. :func:`pandas.json_normalize` flattens that into dotted
columns (``additionalAttributes.appId``…) at the transform boundary.

The spec's brace-depth guidance therefore doesn't apply, but the
*symptom* it targets — blank UX / integration / action / process / role
/ target-user columns downstream — is still real, because the raw dict
was never projected into named, category-level columns the reporting
model can pivot on. This module fills that gap:

* :func:`parse_additional_attributes` coerces the incoming value to a
  dict — accepting a real dict, a JSON string (belt-and-suspenders in
  case the API contract ever drifts), or ``None``.
* :func:`extract_from_dict` projects the parsed dict into the named
  column set from Section 4 of the spec, always returning every column
  key (``None`` for missing sub-fields).
* :func:`enrich_event_dicts` runs the pair over a list of event dumps,
  emits a structlog summary event, and returns the enriched list ready
  for :func:`pandas.json_normalize`.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


# Section 4 field map: source key (Anaplan additionalAttributes) → target
# SQLite column. Column names use snake_case per the tool's existing
# convention (see model history tables in loader.py); the spec's table
# uses this exact spelling too.
_EXTRACTION_MAP: dict[str, str] = {
    # UX App / Page
    "appId": "app_id",
    "appName": "app_name",
    "pageId": "page_id",
    "pageName": "page_name",
    # CloudWorks integration
    "integrationId": "integration_id",
    "integrationName": "integration_name",
    "integrationFlowId": "integration_flow_id",
    # Action
    "actionId": "action_id",
    "actionName": "action_name",
    "actionType": "action_type",
    # Process
    "processId": "process_id",
    "processName": "process_name",
    # Role
    "roleId": "role_id",
    "roleName": "role_name",
    # Target user (admin events)
    "targetUserId": "target_user_id",
    "targetUserName": "target_user_name",
}

# Raw JSON archive column. Kept independent of _EXTRACTION_MAP because
# it isn't a projection of a single source key — it's the full parsed
# dict serialized back to JSON. Serves as the forward-compatibility
# hedge described in Section 4 of the spec.
_RAW_COLUMN: str = "additional_attributes_raw"

# Full list of columns this module owns on the events table. Consumers
# (schema migration, backfill, views) import this so the set of managed
# columns stays a single source of truth.
ADDITIONAL_ATTRIBUTES_COLUMNS: list[str] = [*_EXTRACTION_MAP.values(), _RAW_COLUMN]

# Category → owned columns. The settings block gates population per
# category (spec Milestone 5). This mapping powers the "clear disabled
# categories" step in :func:`extract_from_dict`.
CATEGORY_TO_COLUMNS: dict[str, list[str]] = {
    "uxAppPage": ["app_id", "app_name", "page_id", "page_name"],
    "cwIntegration": ["integration_id", "integration_name", "integration_flow_id"],
    "action": ["action_id", "action_name", "action_type"],
    "process": ["process_id", "process_name"],
    "role": ["role_id", "role_name"],
    "targetUser": ["target_user_id", "target_user_name"],
}


def parse_additional_attributes(
    value: Any,
    *,
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """Coerce an ``additionalAttributes`` field value to a plain dict.

    Handles three real-world inputs:

    * ``dict`` — passed through as-is (the current v1 API contract).
    * ``str`` — decoded via :func:`json.loads`. Belt-and-suspenders for
      the case Anaplan starts sending the value as a JSON string, or a
      backfill reads a column stored as text. Malformed JSON is caught
      and logged at DEBUG rather than raised — non-JSON strings are
      routine for the ``FRCST-*`` event family, where the field is
      sometimes a bare identifier.
    * anything else (``None``, list, scalar) → returns ``None``.

    Args:
        value: The raw ``additionalAttributes`` value from an event.
        correlation_id: Optional per-event run identifier the caller
            wants stitched into DEBUG log lines when parsing fails.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug(
                "additional_attributes_parse_failed",
                correlation_id=correlation_id,
                error=str(exc),
                preview=stripped[:64],
            )
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def extract_from_dict(
    attrs: dict[str, Any] | None,
    *,
    enabled_categories: set[str] | None = None,
    retain_raw: bool = True,
) -> dict[str, str | None]:
    """Project a parsed attributes dict onto the named column set.

    Every column in :data:`ADDITIONAL_ATTRIBUTES_COLUMNS` appears in the
    returned dict — as the extracted value when present in *attrs* and
    the corresponding category is enabled, or ``None`` otherwise. That
    uniform shape makes downstream loading (schema-migration, backfill,
    :func:`pandas.DataFrame`) work the same on every event, whether or
    not any given sub-field was populated by Anaplan.

    Args:
        attrs: Parsed ``additionalAttributes`` dict, or ``None`` for
            events that had no such payload.
        enabled_categories: When set, only columns owned by these
            categories are populated; the rest are forced to ``None``.
            ``None`` (the default) enables every category, matching the
            "parse everything, let the schema keep it" behavior spec
            Milestone 5 describes.
        retain_raw: When ``False``, ``additional_attributes_raw`` is
            emitted as ``None`` regardless of the input. Wired to the
            ``retainRawJson`` setting.

    Returns:
        A dict keyed by :data:`ADDITIONAL_ATTRIBUTES_COLUMNS`, values
        are the extracted strings or ``None``.
    """
    result: dict[str, str | None] = {col: None for col in ADDITIONAL_ATTRIBUTES_COLUMNS}
    if not isinstance(attrs, dict) or not attrs:
        return result

    allowed_columns: set[str] | None
    if enabled_categories is None:
        allowed_columns = None
    else:
        allowed_columns = set()
        for cat in enabled_categories:
            allowed_columns.update(CATEGORY_TO_COLUMNS.get(cat, []))

    for src_key, target_col in _EXTRACTION_MAP.items():
        if allowed_columns is not None and target_col not in allowed_columns:
            continue
        raw_val = attrs.get(src_key)
        if raw_val is None:
            continue
        # Coerce every scalar to str so SQLite columns remain uniformly
        # typed. Nested dicts / lists inside additionalAttributes (rare)
        # are JSON-serialized so the row still lands.
        if isinstance(raw_val, str):
            result[target_col] = raw_val
        elif isinstance(raw_val, dict | list):
            result[target_col] = json.dumps(raw_val, ensure_ascii=False)
        else:
            result[target_col] = str(raw_val)

    if retain_raw:
        # Serialize with sort_keys so backfill re-runs produce identical
        # archive strings even if Python dict insertion order shifts.
        result[_RAW_COLUMN] = json.dumps(attrs, ensure_ascii=False, sort_keys=True)

    return result


def enrich_event_dicts(
    event_dumps: list[dict[str, Any]],
    *,
    enabled_categories: set[str] | None = None,
    retain_raw: bool = True,
    correlation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Add the extracted columns onto each event dump in place.

    Called after :meth:`AuditEvent.model_dump` and before
    :func:`pandas.json_normalize`, so the extracted keys become
    first-class columns on the DataFrame alongside the dotted-name
    columns Pandas produces from the nested ``additionalAttributes``
    dict itself. Both representations coexist:

    * ``additionalAttributes.appId`` — the flattened dotted column,
      still referenced by ``audit_query.sql``.
    * ``app_id`` — the new named column, referenced by the staging
      views and the reporting-model imports the spec introduces.

    Emits a summary structlog event ``parser.additional_attributes_extracted``
    with the count of events that yielded any extracted values.

    Args:
        event_dumps: List of ``AuditEvent.model_dump()`` dicts,
            mutated in place with the new column keys.
        enabled_categories: Passed through to :func:`extract_from_dict`.
        retain_raw: Passed through to :func:`extract_from_dict`.
        correlation_id: Run-level ID propagated into per-event DEBUG
            logs when parsing fails.

    Returns:
        The same list, mutated. Returned for call-site fluency.
    """
    extracted_count = 0
    for dump in event_dumps:
        attrs = parse_additional_attributes(
            dump.get("additionalAttributes"),
            correlation_id=correlation_id,
        )
        extraction = extract_from_dict(
            attrs,
            enabled_categories=enabled_categories,
            retain_raw=retain_raw,
        )
        # A dump "yielded" extractions if any non-raw column got set —
        # the raw archive alone (which every non-empty dict produces)
        # doesn't count as observability signal.
        yielded = any(v is not None for k, v in extraction.items() if k != _RAW_COLUMN)
        if yielded:
            extracted_count += 1
        dump.update(extraction)

    logger.info(
        "parser.additional_attributes_extracted",
        correlation_id=correlation_id,
        events_processed=len(event_dumps),
        events_with_extractions=extracted_count,
        categories_enabled=(
            sorted(enabled_categories) if enabled_categories is not None else "all"
        ),
        retain_raw=retain_raw,
    )
    return event_dumps
