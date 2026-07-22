"""Regression tests for v3.2.6 follow-ups — Anaplan's loose audit typing.

The real Audit API returns:
- ``id`` as an integer (e.g. 2529918698), not a string.
- ``additionalAttributes`` as ``null`` on events that have none (e.g. logins).

Both broke a real run. Verified here:
- AuditEvent coerces integer / null string fields instead of raising.
- The events table pre-declares the core additionalAttributes.* columns, so
  a batch with no attributes (all logins) still lets audit_query.sql join.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd

from anaplan_audit.api.models import AuditEvent
from anaplan_audit.transform.loader import _KNOWN_OPTIONAL_EVENT_COLUMNS, load_to_duckdb


class TestAuditEventCoercion:
    def test_integer_id_is_coerced_to_string(self) -> None:
        event = AuditEvent.model_validate(
            {
                "id": 2529918698,
                "eventTypeId": "USR-8",
                "userId": "u-1",
                "additionalAttributes": None,
                "message": "User login success",
                "success": True,
                "eventDate": 1783365000000,
            }
        )
        assert event.id == "2529918698"
        assert isinstance(event.id, str)

    def test_null_string_fields_become_empty(self) -> None:
        event = AuditEvent.model_validate(
            {"id": 1, "hostName": None, "serviceVersion": None, "checksum": None}
        )
        assert event.hostName == ""
        assert event.serviceVersion == ""
        assert event.checksum == ""

    def test_null_additional_attributes_preserved_as_extra(self) -> None:
        # additionalAttributes is not a declared field; None must be accepted.
        event = AuditEvent.model_validate({"id": 1, "additionalAttributes": None})
        dumped = event.model_dump()
        assert dumped["additionalAttributes"] is None

    def test_string_id_still_accepted(self) -> None:
        # Existing string ids (and test fixtures) must keep working.
        event = AuditEvent.model_validate({"id": "audit-001"})
        assert event.id == "audit-001"


class TestLoginOnlyBatchColumns:
    """A batch with no additionalAttributes must still get the dotted columns."""

    def test_events_table_predeclares_core_attribute_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Simulate an all-login batch: json_normalize would produce NO
        # additionalAttributes.* columns here.
        events = pd.DataFrame(
            [
                {"id": "1", "eventTypeId": "USR-8", "eventDate": 1783365000000, "index": 0},
                {"id": "2", "eventTypeId": "USR-8", "eventDate": 1783365001000, "index": 0},
            ]
        )
        load_to_duckdb(db, {"events": events})

        with closing(duckdb.connect(str(db))) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}

        # Every column audit_query.sql joins on must exist even though no
        # event in the batch carried additionalAttributes.
        for col in _KNOWN_OPTIONAL_EVENT_COLUMNS:
            assert col in cols, f"{col} was not pre-declared"
        # Spot-check the core ones the SQL relies on most.
        assert "additionalAttributes.workspaceId" in cols
        assert "additionalAttributes.modelId" in cols
        assert "additionalAttributes.actionId" in cols
