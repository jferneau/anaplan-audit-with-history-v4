"""Tests for the data transformation layer."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from anaplan_audit.exceptions import StorageLoadError
from anaplan_audit.transform.loader import _sanitize_for_storage, load_to_duckdb
from anaplan_audit.transform.runner import run_audit_query

from .conftest import seed_tables


def _seed_full_schema(db_path: Path) -> None:
    """Seed all tables required by audit_query.sql with fixture data.

    Uses the shared ``seed_tables`` helper (register + CTAS) so the fixture
    write path stays independent of the custom upsert path.  The loader is
    tested separately in ``TestDuckDBLoader``.
    """
    # Column names match the v1 Anaplan Audit API schema exactly.
    # Dotted names represent the flattened additionalAttributes fields
    # that audit_query.sql references via e."additionalAttributes.xxx".
    events_df = pd.DataFrame(
        [
            {
                "id": "evt-001",
                "eventDate": 1705312200000,
                "index": 0,
                "eventTimeZone": "UTC",
                "createdDate": 1705312200000,
                "createdTimeZone": "UTC",
                "eventTypeId": "CONN-1",
                "userId": "user-001",
                "tenantId": "tenant-001",
                "additionalAttributes.workspaceId": "ws-001",
                "additionalAttributes.modelId": "model-001",
                "additionalAttributes.actionId": None,
                "additionalAttributes.name": None,
                "additionalAttributes.type": None,
                "additionalAttributes.auth_id": None,
                "additionalAttributes.modelRoleName": None,
                "additionalAttributes.modelRoleId": None,
                "additionalAttributes.objectTypeId": None,
                "additionalAttributes.roleId": None,
                "additionalAttributes.roleName": None,
                "additionalAttributes.objectTenantId": None,
                "additionalAttributes.objectId": None,
                "additionalAttributes.active": None,
                "additionalAttributes.appId": None,
                "additionalAttributes.appName": None,
                "additionalAttributes.pageId": None,
                "additionalAttributes.pageName": None,
                "additionalAttributes.pipelineId": None,
                "additionalAttributes.dataspaceId": None,
                "additionalAttributes.scheduleId": None,
                "additionalAttributes.connectionId": None,
                "additionalAttributes.taskId": None,
                "additionalAttributes.workflowTemplateId": None,
                "additionalAttributes.commentId": None,
                "objectId": "obj-001",
                "message": "Test event",
                "success": True,
                "errorNumber": None,
                "ipAddress": "192.168.1.1",
                "userAgent": "Mozilla/5.0",
                "sessionId": "sess-001",
                "hostName": "api.anaplan.com",
                "serviceVersion": "1.0",
                "objectTypeId": "1",
                "objectTenantId": "tenant-001",
                "checksum": "abc123",
            }
        ]
    )

    users_df = pd.DataFrame(
        [
            {"id": "user-001", "userName": "john@test.com", "displayName": "John Doe"},
        ]
    )
    workspaces_df = pd.DataFrame([{"id": "ws-001", "name": "Finance"}])
    models_df = pd.DataFrame([{"id": "model-001", "name": "Revenue Model"}])
    cloudworks_df = pd.DataFrame(
        [
            {"integrationId": "cw-001", "name": "Daily Sync", "modelId": "model-001"},
        ]
    )
    # act_codes mirrors activity_events.csv column names exactly.
    act_codes_df = pd.DataFrame(
        [
            {
                "Event Code": "CONN-1",
                "Event Message": "Connection created",
                "Associated Object Id": "The associated connection ID",
                "Notes": "--",
            }
        ]
    )
    # actions.model_id uses snake_case — matches the SQL join condition
    # ``a.id || a.model_id``.
    actions_df = pd.DataFrame(
        [
            {"id": "act-001", "name": "Import Action", "model_id": "model-001"},
        ]
    )

    seed_tables(
        db_path,
        {
            "events": events_df,
            "users": users_df,
            "workspaces": workspaces_df,
            "models": models_df,
            "cloudworks": cloudworks_df,
            "act_codes": act_codes_df,
            "actions": actions_df,
        },
    )


_AA_KEYS = (
    "workspaceId",
    "modelId",
    "actionId",
    "name",
    "type",
    "auth_id",
    "modelRoleName",
    "modelRoleId",
    "objectTypeId",
    "roleId",
    "roleName",
    "objectTenantId",
    "objectId",
    "active",
    "appId",
    "appName",
    "pageId",
    "pageName",
    "pipelineId",
    "dataspaceId",
    "scheduleId",
    "connectionId",
    "taskId",
    "workflowTemplateId",
    "commentId",
)


def _event(**over: object) -> dict[str, object]:
    """A complete events-table row (every column audit_query.sql references),
    with sensible defaults; pass overrides for the fields under test."""
    base: dict[str, object] = {
        "id": "e1",
        "eventDate": 1705312200000,
        "index": 0,
        "eventTimeZone": "UTC",
        "createdDate": 1705312200000,
        "createdTimeZone": "UTC",
        "eventTypeId": "USR-41",
        "userId": "user-001",
        "tenantId": "tenant-001",
        "objectId": "",
        "message": "",
        "success": True,
        "errorNumber": None,
        "ipAddress": "",
        "userAgent": "",
        "sessionId": "",
        "hostName": "",
        "serviceVersion": "",
        "objectTypeId": "",
        "objectTenantId": "",
        "checksum": "c1",
    }
    for k in _AA_KEYS:
        base[f"additionalAttributes.{k}"] = None
    base.update(over)
    return base


class TestDuckDBLoader:
    """Test loading data into DuckDB."""

    def test_load_metadata_tables(self, tmp_path: Path) -> None:
        """Metadata tables are created and populated."""
        db_path = tmp_path / "test.db"
        datasets = {
            "workspaces": pd.DataFrame([{"id": "ws-001", "name": "Finance", "active": True}]),
            "users": pd.DataFrame(
                [{"id": "u-001", "userName": "john@test.com", "displayName": "John"}]
            ),
        }
        load_to_duckdb(db_path, datasets)

        with closing(duckdb.connect(str(db_path))) as conn:
            ws = conn.execute("SELECT * FROM workspaces").fetchall()
            assert len(ws) == 1
            users = conn.execute("SELECT * FROM users").fetchall()
            assert len(users) == 1

    def test_events_upsert_preserves_history(self, tmp_path: Path) -> None:
        """Audit events use upsert to preserve historical data across runs."""
        db_path = tmp_path / "test.db"

        batch1 = pd.DataFrame(
            [
                {"id": "evt-001", "eventDate": 1000, "message": "Open"},
                {"id": "evt-002", "eventDate": 2000, "message": "Export"},
            ]
        )
        load_to_duckdb(db_path, {"events": batch1})

        # Second run overlaps on evt-002, adds evt-003.
        batch2 = pd.DataFrame(
            [
                {"id": "evt-002", "eventDate": 2000, "message": "ExportUpdated"},
                {"id": "evt-003", "eventDate": 3000, "message": "Import"},
            ]
        )
        load_to_duckdb(db_path, {"events": batch2})

        with closing(duckdb.connect(str(db_path))) as conn:
            rows = conn.execute("SELECT id, message FROM events ORDER BY id").fetchall()
        assert len(rows) == 3
        assert rows[1] == ("evt-002", "ExportUpdated")  # updated, not duplicated

    def test_events_schema_migrates_when_new_attribute_appears(self, tmp_path: Path) -> None:
        """A new additionalAttributes.* key in a later batch is added via ALTER TABLE.

        Anaplan adds new audit event types over time (UX, ADO, Workflow templates,
        Comments). Each carries new ``additionalAttributes.*`` keys, which
        ``pd.json_normalize`` surfaces as new DataFrame columns. The events
        table — created from the first batch's columns — must grow to accept
        them. Without the migration, ``_upsert_events`` would fail with
        ``OperationalError: no such column``.
        """
        db_path = tmp_path / "test.db"

        # First batch — schema does NOT include the new UX column.
        batch1 = pd.DataFrame([{"id": "evt-001", "eventTypeId": "USR-8", "message": "Login"}])
        load_to_duckdb(db_path, {"events": batch1})

        # Second batch — introduces a brand-new dotted column.
        batch2 = pd.DataFrame(
            [
                {
                    "id": "evt-002",
                    "eventTypeId": "USR-48",
                    "message": "UX app opened",
                    "additionalAttributes.appId": "app-xyz",
                }
            ]
        )
        load_to_duckdb(db_path, {"events": batch2})

        with closing(duckdb.connect(str(db_path))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            row = conn.execute(
                'SELECT "additionalAttributes.appId" FROM events WHERE id = ?',
                ("evt-002",),
            ).fetchone()

        assert "additionalAttributes.appId" in cols
        assert row[0] == "app-xyz"

    def test_known_optional_columns_predeclared(self, tmp_path: Path) -> None:
        """The well-known optional event columns are pre-created on first write.

        ``audit_query.sql`` references columns for UX, ADO, Workflow templates,
        and Comments. On a tenant that has not yet produced those events, the
        SELECT must still succeed, so the columns are pre-declared when the
        events table is first written.
        """
        db_path = tmp_path / "test.db"
        # Minimal first batch — only the legacy columns. The pre-declaration
        # should still add the optional columns.
        batch = pd.DataFrame([{"id": "evt-001", "eventTypeId": "USR-8"}])
        load_to_duckdb(db_path, {"events": batch})

        with closing(duckdb.connect(str(db_path))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}

        for optional in (
            "additionalAttributes.appId",
            "additionalAttributes.pageId",
            "additionalAttributes.pipelineId",
            "additionalAttributes.taskId",
            "additionalAttributes.workflowTemplateId",
            "additionalAttributes.commentId",
        ):
            assert optional in cols, f"{optional} was not pre-declared"

    # (v3 had a WAL-journal-mode assertion here — a SQLite-only concept with
    # no DuckDB analog; DuckDB manages its own WAL unconditionally.)

    def test_utc_session_timezone_pinned(self, tmp_path: Path) -> None:
        """Connections pin the session TimeZone to UTC.

        DuckDB defaults to the host machine's local timezone; audit_query.sql
        formats timestamps via strftime, which renders in session time —
        without the pin, every timestamp would shift by the host's UTC offset.
        """
        from anaplan_audit.transform.loader import _connect

        db_path = tmp_path / "test.db"
        with closing(_connect(db_path)) as conn:
            tz = conn.execute("SELECT current_setting('TimeZone')").fetchone()[0]
        assert tz == "UTC"

    def test_nested_dict_column_serialised_to_json(self, tmp_path: Path) -> None:
        """Columns with dict values (from extra="allow" API models) are JSON-serialised."""
        db_path = tmp_path / "test.db"
        # Simulates a SCIM user with a nested 'groups' list returned by Anaplan.
        users_df = pd.DataFrame(
            [{"id": "u-001", "userName": "alice@test.com", "groups": [{"value": "g1"}]}]
        )
        load_to_duckdb(db_path, {"users": users_df})

        with closing(duckdb.connect(str(db_path))) as conn:
            row = conn.execute("SELECT groups FROM users").fetchone()
        import json

        assert json.loads(row[0]) == [{"value": "g1"}]

    def test_nested_list_column_serialised_to_json(self, tmp_path: Path) -> None:
        """Columns with list values are JSON-serialised before writing to SQLite."""
        db_path = tmp_path / "test.db"
        df = pd.DataFrame([{"id": "m-001", "name": "Model", "tags": ["a", "b"]}])
        load_to_duckdb(db_path, {"models": df})

        with closing(duckdb.connect(str(db_path))) as conn:
            row = conn.execute("SELECT tags FROM models").fetchone()
        import json

        assert json.loads(row[0]) == ["a", "b"]

    def test_error_message_includes_table_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """StorageLoadError context includes the failing table name."""
        db_path = tmp_path / "test.db"

        # Make the register step raise *after* current_table is set in the
        # loop so the table name makes it into the error context.
        def _explode(self: object, *args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated write failure")

        monkeypatch.setattr(duckdb.DuckDBPyConnection, "register", _explode)
        bad_df = pd.DataFrame([{"id": "1"}])
        with pytest.raises(StorageLoadError) as exc_info:
            load_to_duckdb(db_path, {"broken_table": bad_df})
        assert "broken_table" in exc_info.value.context.get("table", "")


class TestSanitizeForStorage:
    """Unit tests for the _sanitize_for_storage helper."""

    def test_scalar_columns_unchanged(self) -> None:
        df = pd.DataFrame([{"id": "1", "name": "Alice", "active": True}])
        result = _sanitize_for_storage(df)
        assert result["id"].iloc[0] == "1"
        assert result["active"].iloc[0] == True  # noqa: E712 — numpy bool needs ==, not is

    def test_dict_column_becomes_json_string(self) -> None:
        df = pd.DataFrame([{"id": "1", "meta": {"key": "value"}}])
        result = _sanitize_for_storage(df)
        import json

        assert json.loads(result["meta"].iloc[0]) == {"key": "value"}

    def test_list_column_becomes_json_string(self) -> None:
        df = pd.DataFrame([{"id": "1", "groups": [{"value": "g1"}, {"value": "g2"}]}])
        result = _sanitize_for_storage(df)
        import json

        assert json.loads(result["groups"].iloc[0]) == [{"value": "g1"}, {"value": "g2"}]

    def test_mixed_column_only_converts_complex_values(self) -> None:
        """Scalar values in a mixed column are left as-is; only dicts/lists are serialised."""
        df = pd.DataFrame([{"id": "1", "extra": {"a": 1}}, {"id": "2", "extra": "plain"}])
        result = _sanitize_for_storage(df)
        import json

        assert json.loads(result["extra"].iloc[0]) == {"a": 1}
        assert result["extra"].iloc[1] == "plain"

    def test_original_dataframe_not_mutated(self) -> None:
        df = pd.DataFrame([{"id": "1", "groups": [1, 2, 3]}])
        _ = _sanitize_for_storage(df)
        assert isinstance(df["groups"].iloc[0], list)


class TestAuditQueryRunner:
    """Test the audit SQL transform against seeded fixture data."""

    def test_run_audit_query_returns_dataframe(self, tmp_path: Path) -> None:
        """audit_query.sql executes and returns a DataFrame."""
        db_path = tmp_path / "test.db"
        _seed_full_schema(db_path)

        df = run_audit_query(db_path, tenant_name="TestTenant")

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

    def test_run_audit_query_columns(self, tmp_path: Path) -> None:
        """Output DataFrame contains the expected columns from audit_query.sql."""
        db_path = tmp_path / "test.db"
        _seed_full_schema(db_path)

        df = run_audit_query(db_path, tenant_name="TestTenant")

        expected_cols = {
            "LOAD_ID",
            "BATCH_ID",
            "AUDIT_ID",
            "EVENT_DATE",
            "EVENT_ID",
            "EVENT_MESSAGE",
            "USER_ID",
            "USER_NAME",
            "TENANT_NAME",
            "WORKSPACE_ID",
            "IP_ADDRESS",
        }
        assert expected_cols.issubset(set(df.columns))

    def test_model_resolves_from_objectid_case_insensitively(self, tmp_path: Path) -> None:
        """An action-execution event carries its model in objectId (lowercase),
        while the models table id is UPPERCASE. MODEL_ID/MODEL_NAME/OBJECT must
        still resolve — MODEL_ID as the canonical uppercase id."""
        db_path = tmp_path / "test.db"
        low = "e667a85d33c042cfaca069e69441c51f"
        up = low.upper()
        events_df = pd.DataFrame([_event(objectId=low, eventTypeId="USR-41")])  # no modelId
        # Force additionalAttributes.* to VARCHAR — production's loader types
        # every event column VARCHAR, but seed_tables would infer an all-null
        # column as INTEGER, which the MODEL_ID CASE can't mix with the VARCHAR
        # m2.id.
        aa = [c for c in events_df.columns if c.startswith("additionalAttributes.")]
        events_df[aa] = events_df[aa].astype("string")
        seed_tables(
            db_path,
            {
                "events": events_df,
                "users": pd.DataFrame(
                    [{"id": "user-001", "userName": "u@t.com", "displayName": "U"}]
                ),
                "workspaces": pd.DataFrame([{"id": "ws-001", "name": "WS"}]),
                "models": pd.DataFrame([{"id": up, "name": "Prod Model"}]),
                "cloudworks": pd.DataFrame(
                    [{"integrationId": "cw-1", "name": "CW", "modelId": up}]
                ),
                "act_codes": pd.DataFrame(
                    [
                        {
                            "Event Code": "USR-41",
                            "Event Message": "Process executed",
                            "Associated Object Id": "",
                            "Notes": "--",
                        }
                    ]
                ),
                "actions": pd.DataFrame([{"id": "a1", "name": "Proc", "model_id": up}]),
            },
        )
        df = run_audit_query(db_path, tenant_name="T")
        row = df.iloc[0]
        assert row["MODEL_ID"] == up  # canonical uppercase, matches the MODEL list
        assert row["MODEL_NAME"] == "Prod Model"
        assert row["OBJECT_TYPE"] == "Model"
        assert row["OBJECT_NAME"] == "Prod Model"

    def test_event_category_derives_from_taxonomy_even_when_uncatalogued(
        self, tmp_path: Path
    ) -> None:
        """EVENT_CATEGORY is the EVENT_ID parent, derived from the code prefix
        by the taxonomy UDF — not looked up from the catalog. So a code that
        isn't in act_codes (the exact orphan case, e.g. WF-112) still gets its
        category, while a known code resolves the same way."""
        db_path = tmp_path / "test.db"
        events_df = pd.DataFrame(
            [
                _event(id="e1", eventTypeId="USR-8"),  # in the catalog
                _event(id="e2", eventTypeId="WF-112"),  # NOT in the catalog
            ]
        )
        aa = [c for c in events_df.columns if c.startswith("additionalAttributes.")]
        events_df[aa] = events_df[aa].astype("string")
        seed_tables(
            db_path,
            {
                "events": events_df,
                "users": pd.DataFrame(
                    [{"id": "user-001", "userName": "u@t.com", "displayName": "U"}]
                ),
                "workspaces": pd.DataFrame([{"id": "ws-001", "name": "WS"}]),
                "models": pd.DataFrame([{"id": "m-1", "name": "M"}]),
                "cloudworks": pd.DataFrame(
                    [{"integrationId": "cw-1", "name": "CW", "modelId": "m-1"}]
                ),
                # Only USR-8 is catalogued; WF-112 is deliberately absent.
                "act_codes": pd.DataFrame(
                    [
                        {
                            "Event Code": "USR-8",
                            "Event Message": "User login success",
                            "Associated Object Id": "",
                            "Notes": "--",
                        }
                    ]
                ),
                "actions": pd.DataFrame([{"id": "a1", "name": "A", "model_id": "m-1"}]),
            },
        )
        df = run_audit_query(db_path, tenant_name="T")
        by_id = dict(zip(df["EVENT_ID"], df["EVENT_CATEGORY"], strict=True))
        assert by_id["USR-8"] == "USER ACTIVITY"
        assert by_id["WF-112"] == "WORKFLOW"

    def test_tenant_name_substituted(self, tmp_path: Path) -> None:
        """TENANT_NAME column contains the supplied tenant_name value."""
        db_path = tmp_path / "test.db"
        _seed_full_schema(db_path)

        df = run_audit_query(db_path, tenant_name="AcmeCorp")

        assert df["TENANT_NAME"].iloc[0] == "AcmeCorp"

    def test_batch_id_is_integer(self, tmp_path: Path) -> None:
        """BATCH_ID is a non-zero integer (current epoch in milliseconds)."""
        db_path = tmp_path / "test.db"
        _seed_full_schema(db_path)

        df = run_audit_query(db_path, tenant_name="TestTenant")

        assert int(df["BATCH_ID"].iloc[0]) > 0
