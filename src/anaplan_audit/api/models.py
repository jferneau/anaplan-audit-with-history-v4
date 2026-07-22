"""Pydantic response models for Anaplan API payloads.

All models use ``extra="allow"`` so new fields from Anaplan don't break
existing runs — but known fields are typed.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _to_str(value: object) -> str:
    """Coerce any scalar (or None) to a string.

    The Anaplan Audit API is loosely typed: fields we treat as strings can
    come back as integers (e.g. the event ``id`` is a number like
    ``2529918698``) or ``null``. We store everything as text locally and
    resolve/join on it downstream, so normalise to ``str`` at the edge
    rather than let a numeric ``id`` raise a ValidationError mid-fetch.
    """
    return "" if value is None else str(value)


# A string field that tolerates ints / None from the API.
StrCoerce = Annotated[str, BeforeValidator(_to_str)]


class AuditEvent(BaseModel):
    """A single audit event from the Anaplan Audit API.

    The declared fields are exactly the top-level attributes that
    ``audit_query.sql`` references (``e.<field>``), so those columns are
    guaranteed to exist in the events table regardless of what any given
    batch contains.  Everything else — including the nested
    ``additionalAttributes`` dict — flows through ``extra="allow"`` and is
    flattened into dotted column names by ``pd.json_normalize``.

    String fields use :data:`StrCoerce` because the Audit API returns some
    of them (notably ``id``) as integers or ``null``.
    """

    model_config = ConfigDict(extra="allow")

    id: StrCoerce = ""
    eventDate: int = 0
    index: int = 0
    eventTimeZone: StrCoerce = ""
    createdDate: int = 0
    createdTimeZone: StrCoerce = ""
    eventTypeId: StrCoerce = ""
    userId: StrCoerce = ""
    tenantId: StrCoerce = ""
    objectId: StrCoerce = ""
    objectTypeId: StrCoerce = ""
    objectTenantId: StrCoerce = ""
    message: StrCoerce = ""
    success: bool = True
    errorNumber: StrCoerce | None = None
    ipAddress: StrCoerce = ""
    userAgent: StrCoerce = ""
    sessionId: StrCoerce = ""
    hostName: StrCoerce = ""
    serviceVersion: StrCoerce = ""
    checksum: StrCoerce = ""


class User(BaseModel):
    """An Anaplan user from the SCIM API.

    Emits ``id / userName / displayName / firstName / lastName``. The first
    three are top-level SCIM fields; ``firstName`` / ``lastName`` are lifted
    from SCIM's nested ``name`` object (``name.givenName`` /
    ``name.familyName``) by :meth:`_flatten_scim_name` so they land as flat
    CSV columns the reporting model's ``SYS Users`` First Name / Last Name
    line items import directly — rather than being reverse-engineered from
    ``displayName`` in an Anaplan formula (Jon, 2026-07-21: fetch the real
    values SCIM already provides).

    ``extra="ignore"`` still drops every other SCIM key (``schemas``,
    ``meta``, ``emails``, ``entitlements``, ``active``, and the ``name``
    object itself once its two sub-fields are lifted out) so nothing else
    reaches the DataFrame / CSV. Do not add further fields without a
    documented downstream requirement.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    userName: str = ""
    displayName: str = ""
    firstName: str = ""
    lastName: str = ""

    @model_validator(mode="before")
    @classmethod
    def _flatten_scim_name(cls, data: Any) -> Any:
        """Lift ``name.givenName`` / ``name.familyName`` to flat columns.

        SCIM returns the structured name as
        ``{"givenName": ..., "familyName": ..., "formatted": ...}``. Any
        sub-field may be missing or ``null`` (API/test accounts often have
        no name set), so each defaults to ``""``. An explicit top-level
        ``firstName`` / ``lastName`` (used in tests) is left untouched.
        """
        if isinstance(data, dict):
            name = data.get("name")
            if isinstance(name, dict):
                merged = dict(data)
                merged.setdefault("firstName", name.get("givenName") or "")
                merged.setdefault("lastName", name.get("familyName") or "")
                return merged
        return data


class Workspace(BaseModel):
    """An Anaplan workspace from the Integration API.

    ``sizeAllowance`` and ``currentSize`` are populated only when the
    caller passes ``?tenantDetails=true`` (see :func:`list_workspaces`);
    otherwise they default to ``0``.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    active: bool = True
    sizeAllowance: int = 0
    currentSize: int = 0


class Model(BaseModel):
    """An Anaplan model from the Integration API (``?modelDetails=true``).

    Every field the reporting model's Tenant Detail > Models module
    consumes is declared here so that :func:`_metadata_frame` (which
    reads ``model_fields``) always guarantees the column exists on a
    zero-row result set. ``extra="allow"`` remains so any new detail
    field Anaplan adds flows through without a code change — with the
    single exception of ``categoryValues`` which the orchestrator drops
    explicitly, matching Quinn's v1 (see spec Section 2).

    ``memoryUsage`` and ``lastSavedSerialNumber`` default to ``0`` so
    pandas / to_sql infers ``INTEGER``. ``isoCreationDate`` and
    ``lastModified`` use text — Anaplan returns them as ISO 8601 strings
    (e.g. ``"2026-07-06T20:02:34.000+0000"``), and the reporting model's
    ``SYS Models`` staging line items also expect text (formulas do
    ``LEFT(<field>, 19)`` to trim to the display form). ``lastModified``
    uses :data:`StrCoerce` to tolerate the epoch-millisecond variant
    some older responses returned.

    ``currentSize`` and ``lastServerRestartDate`` are deliberately
    absent: the model-export-restoration spec listed them but Anaplan's
    ``?modelDetails=true`` response does not include them for model
    endpoints (confirmed against a 41-model live tenant — 41/41 landed
    null). Declaring them here would only ship two dead columns in
    every CSV.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    activeState: str = ""
    currentWorkspaceId: str = ""
    currentWorkspaceName: str = ""
    modelUrl: str = ""
    isoCreationDate: str = ""
    lastSavedSerialNumber: int = 0
    lastModifiedByUserGuid: str = ""
    memoryUsage: int = 0
    lastModified: StrCoerce = ""
    """Anaplan's last-modification timestamp. Returned as ISO 8601 text
    (``"YYYY-MM-DDTHH:MM:SS.mmm+ZZZZ"``); the reporting model's
    ``SYS Models > lastModified`` staging line item and its
    ``Last Modified Date`` formula (``LEFT(lastModified, 19)``) expect
    text under this exact column name."""


class Action(BaseModel):
    """An Anaplan action from the Integration API.

    The action kind arrives under the API key ``actionType`` (e.g.
    ``IMPORT`` / ``EXPORT`` / ``DELETE_BY_SELECTION`` / ``PROCESS``), *not*
    ``type`` — so a plain ``type`` field stayed empty and the real value
    leaked through ``extra="allow"`` as an ``actionType`` column. The field
    now reads from ``actionType`` first (falling back to ``type`` for older
    payloads / test fixtures) and still serializes as ``type``, so the
    reporting model's SYS Actions ``type`` import is populated.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    type: str = Field("", validation_alias=AliasChoices("actionType", "type"))


class Process(BaseModel):
    """An Anaplan process from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class ImportDataSource(BaseModel):
    """An Anaplan import data source from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class ImportAction(BaseModel):
    """An Anaplan import action from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class AnaplanList(BaseModel):
    """An Anaplan list from the Transactional API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class Module(BaseModel):
    """An Anaplan module from the Transactional API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class LineItem(BaseModel):
    """A line item inside an Anaplan module."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    moduleId: str = ""


class CloudWorksIntegration(BaseModel):
    """A CloudWorks integration.

    Declares every top-level field the reporting model's ``SYS Cloudworks``
    module consumes so ``_metadata_frame`` guarantees the column exists
    on a zero-row response. ``latestRun`` and ``schedule`` are nested
    dicts on the raw API response; the orchestrator flattens them via
    :func:`pandas.json_normalize` into dotted columns
    (``latestRun.triggeredBy``, ``schedule.name``, …) that match the
    dotted line-item names on ``SYS Cloudworks`` blueprint.

    Every string field is typed as :data:`StrCoerce` because the
    CloudWorks API is loosely typed — verified against a live tenant:
    ``modifiedBy`` came back as ``None`` when never edited,
    ``latestRun.success`` as a boolean, ``latestRun.executionErrorCode``
    as an int. StrCoerce coerces all three to strings so no field
    raises a ``ValidationError`` mid-fetch and every column lands as
    text (which the reporting model's Text line items expect anyway).

    Nested dicts use ``dict[str, Any]`` for the same reason: values
    inside ``latestRun`` / ``schedule`` are a mix of str / int / bool /
    None. ``pd.json_normalize`` flattens them to columns whose cells
    are their native Python types; ``DataFrame.to_csv`` renders each
    as its string form, which the property-based import happily
    consumes.

    ``extra="allow"`` still catches anything Anaplan adds without
    requiring a code change.
    """

    model_config = ConfigDict(extra="allow")

    integrationId: StrCoerce = ""
    name: StrCoerce = ""
    type: StrCoerce = ""
    workspaceId: StrCoerce = ""
    modelId: StrCoerce = ""
    createdBy: StrCoerce = ""
    creationDate: StrCoerce = ""
    modifiedBy: StrCoerce = ""
    modificationDate: StrCoerce = ""
    uxVisible: StrCoerce = ""
    notificationId: StrCoerce = ""
    processId: StrCoerce = ""
    latestRun: dict[str, Any] = {}
    schedule: dict[str, Any] = {}


class BulkUploadChunk(BaseModel):
    """Metadata for a bulk upload chunk."""

    model_config = ConfigDict(extra="allow")

    chunk_index: int = 0
    total_chunks: int = 0
    data: str = ""


class Export(BaseModel):
    """An Anaplan export action from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class ExportTask(BaseModel):
    """Status of an Anaplan export task.

    Attributes:
        taskId: Unique task identifier.
        taskState: One of NOT_STARTED, IN_PROGRESS, COMPLETE, FAILED, CANCELLED.
    """

    model_config = ConfigDict(extra="allow")

    taskId: str = ""
    taskState: str = ""
