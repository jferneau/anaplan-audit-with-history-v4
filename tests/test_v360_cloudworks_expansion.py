"""v3.6.0 regression tests — CloudWorks blueprint expansion.

The reporting model's ``SYS Cloudworks`` module has ~25 line items
covering integration detail, latest run stats, and schedule config
(``latestRun.triggeredBy``, ``schedule.name``, ``Created By``,
``Modified By``, ``creationDate``, ``modificationDate``, etc.).

Before v3.6.0 the ``CloudWorksIntegration`` Pydantic class declared
only five fields, so ``CLOUDWORKS_LIST.csv`` shipped only five columns
and most of the ``SYS Cloudworks`` module was blank.

Fix: declare every top-level field on the Pydantic class; flatten the
nested ``latestRun`` / ``schedule`` dicts via ``pd.json_normalize`` in
the orchestrator so their sub-fields become dotted columns matching
the Anaplan line-item names.
"""

from __future__ import annotations

import pandas as pd

from anaplan_audit.api.models import CloudWorksIntegration


class TestCloudWorksIntegrationDeclaresExpectedFields:
    def test_top_level_fields_are_all_declared(self) -> None:
        # Everything SYS Cloudworks needs beyond the original five.
        # ``_metadata_frame`` guarantees these columns exist on a
        # zero-row response by reading ``model_fields``.
        declared = set(CloudWorksIntegration.model_fields.keys())
        expected = {
            "integrationId",
            "name",
            "type",
            "workspaceId",
            "modelId",
            "createdBy",
            "creationDate",
            "modifiedBy",
            "modificationDate",
            "uxVisible",
            "notificationId",
            "processId",
            "latestRun",
            "schedule",
        }
        assert expected.issubset(declared)

    def test_extra_allow_remains(self) -> None:
        # Future Anaplan additions still flow through so a new field
        # doesn't require a code release.
        assert CloudWorksIntegration.model_config.get("extra") == "allow"

    def test_parses_real_shaped_response(self) -> None:
        payload = {
            "integrationId": "int-1",
            "name": "Salesforce to Anaplan",
            "type": "AnaplanExportToFile",
            "workspaceId": "ws-1",
            "modelId": "mod-1",
            "createdBy": "alice@example.com",
            "creationDate": "2026-06-01T12:00:00.000+0000",
            "modifiedBy": "bob@example.com",
            "modificationDate": "2026-07-14T09:30:00.000+0000",
            "uxVisible": "true",
            "notificationId": "notif-42",
            "processId": "proc-7",
            "latestRun": {
                "triggeredBy": "scheduler",
                "startDate": "2026-07-15T02:00:00.000+0000",
                "endDate": "2026-07-15T02:05:00.000+0000",
                "success": "true",
                "message": "OK",
                "executionErrorCode": "",
            },
            "schedule": {
                "name": "Nightly",
                "type": "recurring",
                "status": "enabled",
            },
        }
        c = CloudWorksIntegration.model_validate(payload)
        assert c.integrationId == "int-1"
        assert c.createdBy == "alice@example.com"
        assert c.latestRun["triggeredBy"] == "scheduler"
        assert c.schedule["status"] == "enabled"


class TestNestedFieldsFlattenToDottedColumns:
    """Anaplan's SYS Cloudworks blueprint has line items named literally
    ``latestRun.triggeredBy`` and ``schedule.name``. The property-based
    import matches those against dotted CSV column names, so the tool
    must flatten the nested dicts before ``to_csv``.
    """

    def test_json_normalize_produces_dotted_column_names(self) -> None:
        # Reproduces the orchestrator's flatten step in isolation.
        dumped = [
            CloudWorksIntegration.model_validate(
                {
                    "integrationId": "int-1",
                    "name": "N",
                    "type": "T",
                    "workspaceId": "ws-1",
                    "modelId": "mod-1",
                    "latestRun": {"triggeredBy": "user", "success": "true"},
                    "schedule": {"name": "Nightly", "status": "enabled"},
                }
            ).model_dump()
        ]
        flat = pd.json_normalize(dumped)
        cols = set(flat.columns)
        assert "latestRun.triggeredBy" in cols
        assert "latestRun.success" in cols
        assert "schedule.name" in cols
        assert "schedule.status" in cols
        # And the original nested keys should NOT survive.
        assert "latestRun" not in cols
        assert "schedule" not in cols

    def test_empty_nested_dict_flattens_cleanly(self) -> None:
        # A tenant with an integration that has never run should still
        # produce well-shaped rows.
        dumped = [
            CloudWorksIntegration.model_validate(
                {
                    "integrationId": "int-1",
                    "name": "N",
                    "type": "T",
                    "workspaceId": "ws-1",
                    "modelId": "mod-1",
                }
            ).model_dump()
        ]
        flat = pd.json_normalize(dumped)
        # No sub-columns from latestRun / schedule, but the record
        # is present and other columns land.
        assert flat.iloc[0]["integrationId"] == "int-1"


class TestCloudWorksAcceptsLooselyTypedApiResponses:
    """v3.7.1 regression — the CloudWorks API is loosely typed. Verified
    against a live tenant:

      * ``modifiedBy`` came back as ``None`` (never edited)
      * ``latestRun.success`` as ``True`` (bool, not string)
      * ``latestRun.executionErrorCode`` as ``12`` (int, not string)

    v3.6.0 typed every field as ``str``, which failed hard on all three
    with ``ValidationError``. Every string field now uses ``StrCoerce``
    (matches the pattern used for ``Model.lastModified`` in v3.3.3);
    nested dicts use ``dict[str, Any]`` so bool/int/None sub-values
    pass through and land as their string form after ``to_csv``.
    """

    def test_none_top_level_string_field_is_coerced_to_empty(self) -> None:
        # The exact input shape that crashed the live tenant.
        c = CloudWorksIntegration.model_validate(
            {
                "integrationId": "int-1",
                "name": "N",
                "type": "T",
                "workspaceId": "ws-1",
                "modelId": "mod-1",
                "modifiedBy": None,
            }
        )
        # StrCoerce turns None into "" (see _to_str).
        assert c.modifiedBy == ""

    def test_bool_nested_value_is_accepted(self) -> None:
        # latestRun.success comes back as True from the API.
        c = CloudWorksIntegration.model_validate(
            {
                "integrationId": "int-1",
                "name": "N",
                "type": "T",
                "workspaceId": "ws-1",
                "modelId": "mod-1",
                "latestRun": {
                    "triggeredBy": "scheduler",
                    "success": True,
                    "executionErrorCode": 12,
                },
            }
        )
        # Values preserved as their native Python type — pandas /
        # to_csv handles the string rendering downstream.
        assert c.latestRun["success"] is True
        assert c.latestRun["executionErrorCode"] == 12

    def test_int_nested_value_flattens_to_string_via_to_csv(self) -> None:
        dumped = [
            CloudWorksIntegration.model_validate(
                {
                    "integrationId": "int-1",
                    "name": "N",
                    "type": "T",
                    "workspaceId": "ws-1",
                    "modelId": "mod-1",
                    "latestRun": {"executionErrorCode": 12, "success": True},
                }
            ).model_dump()
        ]
        flat = pd.json_normalize(dumped)
        # The CSV form of what the reporting model actually consumes.
        csv_text = flat.to_csv(index=False)
        # Sanity: dotted columns present, values serialized to string.
        assert "latestRun.executionErrorCode" in csv_text
        assert ",12," in csv_text or csv_text.endswith(",12\n")
        assert "True" in csv_text
