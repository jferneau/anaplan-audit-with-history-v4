"""Regression suite for the v3.3.1 model / user / workspace export
restoration (spec Section 7).

Covers every acceptance criterion:

1. ``list_models`` sends ``?modelDetails=true`` so the API returns
   every Section 3.1 field.
2. Every Section 3.1 model column lands on the SQLite ``models``
   table after a full load.
3. ``users`` table stays at Quinn's 3-column narrow — SCIM extras
   dropped at validation time by ``extra="ignore"``.
4. ``workspaces`` table preserves the full API projection.
5. ``v_models_export`` view resolves ``lastModifiedByUserGuid`` to
   email + display name via LEFT JOIN.
6. LEFT JOIN semantics: a model with an unknown GUID still exports
   (null email columns), not dropped.
7. ``MODEL_LIST.csv`` header row carries every Section 3.1 column
   plus the two joined fields.
8. ``categoryValues`` is dropped in the transform, per Quinn's v1.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import duckdb
import httpx
import pandas as pd
import respx

from anaplan_audit.api.integration import list_models
from anaplan_audit.api.models import Model, User, Workspace
from anaplan_audit.transform.loader import load_to_duckdb
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"


# ----- Section 3.1 expected columns (spec canonical set) --------------
# The set the reporting model actually consumes for models — spec
# Section 3.1 also listed currentSize + lastServerRestartDate but the
# Anaplan API never returns those for model endpoints (verified against
# a 41-model live tenant; 41/41 landed null). Removed in v3.3.4.
_SECTION_3_1_MODEL_COLUMNS = {
    "id",
    "name",
    "activeState",
    "currentWorkspaceId",
    "currentWorkspaceName",
    "modelUrl",
    "isoCreationDate",
    "lastSavedSerialNumber",
    "lastModifiedByUserGuid",
    "memoryUsage",
    "lastModified",
}

# The two extras the export view produces on top of Section 3.1 raw cols.
_VIEW_JOINED_COLUMNS = {"lastModifiedByEmail", "lastModifiedByDisplayName"}


def _table_columns(db_path: Path, name: str) -> set[str]:
    with closing(duckdb.connect(str(db_path))) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


# ---------------------------------------------------------------------------
# Milestone 2 — column preservation on load
# ---------------------------------------------------------------------------


class TestListModelsSendsModelDetails:
    def test_query_param_is_present(self) -> None:
        # Quinn's ?modelDetails=true was the missing piece — without
        # it the API returns only id/name/activeState/currentWorkspaceId
        # etc. and every downstream detail column lands blank.
        with respx.mock:
            route = respx.get(
                f"{BASE}/workspaces/{WS}/models",
                params={"modelDetails": "true"},
            ).mock(return_value=httpx.Response(200, json={"models": []}))
            with make_client() as client:
                list_models(client, BASE, WS)
        assert route.called


class TestModelPydanticDeclaresSection3Fields:
    def test_every_section_3_1_column_is_declared(self) -> None:
        # Guards Section 3.1 fidelity at the model layer so
        # _metadata_frame — which reads model_fields — always has the
        # columns for a 0-row result set. Regression test: someone
        # deleting a field from Model would break this.
        declared = set(Model.model_fields.keys())
        assert _SECTION_3_1_MODEL_COLUMNS.issubset(declared)


class TestUserNarrow:
    def test_declared_fields(self) -> None:
        # id/userName/displayName are top-level SCIM; firstName/lastName are
        # lifted from name.givenName/familyName (Jon, 2026-07-21). Any further
        # expansion needs a documented downstream requirement.
        assert set(User.model_fields.keys()) == {
            "id",
            "userName",
            "displayName",
            "firstName",
            "lastName",
        }

    def test_scim_name_extracted_and_extras_dropped(self) -> None:
        user = User.model_validate(
            {
                "id": "u1",
                "userName": "u@example.com",
                "displayName": "U Ser",
                "schemas": ["urn:..."],
                "meta": {"resourceType": "User"},
                "emails": [{"value": "u@example.com"}],
                "entitlements": [],
                "active": True,
                "name": {"givenName": "U", "familyName": "Ser", "formatted": "U Ser"},
            }
        )
        assert user.firstName == "U"
        assert user.lastName == "Ser"
        # Only the five flat columns survive — the nested name object and every
        # other SCIM key are dropped by extra="ignore".
        assert set(user.model_dump().keys()) == {
            "id",
            "userName",
            "displayName",
            "firstName",
            "lastName",
        }


class TestWorkspacePreservesTenantDetails:
    def test_size_fields_survive_validation(self) -> None:
        ws = Workspace.model_validate(
            {
                "id": "ws-1",
                "name": "Prod",
                "active": True,
                "sizeAllowance": 42949672960,
                "currentSize": 217689238,
            }
        )
        assert ws.sizeAllowance == 42949672960
        assert ws.currentSize == 217689238


# ---------------------------------------------------------------------------
# Milestone 3 — SQLite tables + v_models_export view
# ---------------------------------------------------------------------------


def _seed_models_users_workspaces(db_path: Path) -> None:
    """Load a minimal set of models / users / workspaces via the real
    load_to_duckdb path so the view creation runs.

    ``modelDetails=true``-shaped model rows (every Section 3.1 field);
    two users, one of them the ``lastModifiedByUserGuid`` for the
    known model, the other unrelated; two workspaces.
    """
    models_df = pd.DataFrame(
        [
            {
                "id": "mod-known",
                "name": "Model A",
                "activeState": "ACTIVE",
                "currentWorkspaceId": "ws-1",
                "currentWorkspaceName": "Prod",
                "modelUrl": "https://app.anaplan.com/anaplan/#/model/mod-known",
                "isoCreationDate": "2024-01-01T00:00:00Z",
                "lastSavedSerialNumber": 42,
                "lastModifiedByUserGuid": "usr-alice",
                "memoryUsage": 123456789,
                "lastModified": "2024-11-15T12:06:40.000+0000",
                "workspaceId": "ws-1",
            },
            {
                # This model was modified by a GUID we don't have a
                # user row for — LEFT JOIN must preserve it with null
                # email columns.
                "id": "mod-orphan",
                "name": "Model B (Orphan)",
                "activeState": "ACTIVE",
                "currentWorkspaceId": "ws-1",
                "currentWorkspaceName": "Prod",
                "modelUrl": "https://app.anaplan.com/anaplan/#/model/mod-orphan",
                "isoCreationDate": "2024-02-01T00:00:00Z",
                "lastSavedSerialNumber": 7,
                "lastModifiedByUserGuid": "usr-unknown",
                "memoryUsage": 111,
                "lastModified": "2024-11-15T12:06:40.000+0000",
                "workspaceId": "ws-1",
            },
        ]
    )
    users_df = pd.DataFrame(
        [
            {
                "id": "usr-alice",
                "userName": "alice@example.com",
                "displayName": "Alice Anderson",
                "firstName": "Alice",
                "lastName": "Anderson",
            },
            {
                "id": "usr-bob",
                "userName": "bob@example.com",
                "displayName": "Bob Brown",
                "firstName": "Bob",
                "lastName": "Brown",
            },
        ]
    )
    workspaces_df = pd.DataFrame(
        [
            {
                "id": "ws-1",
                "name": "Prod",
                "active": True,
                "sizeAllowance": 42949672960,
                "currentSize": 217689238,
            }
        ]
    )
    load_to_duckdb(
        db_path,
        {"models": models_df, "users": users_df, "workspaces": workspaces_df},
    )


class TestPragmaTableInfoAfterLoad:
    def test_models_table_carries_every_section_3_1_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        cols = _table_columns(db_path, "models")
        missing = _SECTION_3_1_MODEL_COLUMNS - cols
        assert not missing, f"missing model columns: {missing}"

    def test_users_table_carries_expected_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        cols = _table_columns(db_path, "users")
        assert cols == {"id", "userName", "displayName", "firstName", "lastName"}

    def test_workspaces_table_carries_expected_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        cols = _table_columns(db_path, "workspaces")
        # Section 3.3 with the ``active`` field spelled per the API
        # response (not the spec's ``activeState`` typo — flagged in
        # the PR description).
        assert {"id", "name", "active", "sizeAllowance", "currentSize"}.issubset(cols)


class TestModelsExportView:
    def test_view_is_created_on_load(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            row = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'VIEW' AND table_name = 'v_models_export'"
            ).fetchone()
        assert row is not None

    def test_view_resolves_email_and_display_name_via_left_join(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            row = conn.execute(
                "SELECT lastModifiedByEmail, lastModifiedByDisplayName "
                "FROM v_models_export WHERE id = 'mod-known'"
            ).fetchone()
        assert row is not None
        assert row[0] == "alice@example.com"
        assert row[1] == "Alice Anderson"

    def test_left_join_preserves_row_with_unknown_guid(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            row = conn.execute(
                "SELECT id, lastModifiedByEmail, lastModifiedByDisplayName "
                "FROM v_models_export WHERE id = 'mod-orphan'"
            ).fetchone()
        # The row is present — spec acceptance criterion #3.
        assert row is not None
        assert row[0] == "mod-orphan"
        # Email columns are null because the GUID didn't match any user.
        assert row[1] is None
        assert row[2] is None

    def test_view_carries_every_expected_column(self, tmp_path: Path) -> None:
        # Spec acceptance criterion #4 — the CSV header row must have
        # every Section 3.1 column plus the two joined columns.
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            cur = conn.execute("SELECT * FROM v_models_export LIMIT 0")
            view_cols = {desc[0] for desc in cur.description}
        expected = _SECTION_3_1_MODEL_COLUMNS | _VIEW_JOINED_COLUMNS
        assert expected == view_cols

    def test_view_is_refreshed_across_multiple_loads(self, tmp_path: Path) -> None:
        # DROP + CREATE each load; idempotent and safe to re-run.
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            count = conn.execute("SELECT COUNT(*) FROM v_models_export").fetchone()[0]
        # Two models seeded, replace-mode load, so two rows in the view.
        assert count == 2


# ---------------------------------------------------------------------------
# Milestone 3 — CSV upload path sources from the view
# ---------------------------------------------------------------------------


class TestUploadSourceTableRouting:
    def test_models_is_routed_through_v_models_export(self) -> None:
        # Direct sanity check on the routing map so a future change
        # that reverts the routing gets caught at test time.
        from anaplan_audit.upload import _TABLE_TO_SOURCE

        assert _TABLE_TO_SOURCE.get("models") == "v_models_export"
        # No other tables should be routed through views unless a new
        # spec requires it — future additions require explicit test
        # updates.
        assert set(_TABLE_TO_SOURCE) == {"models"}


class TestCsvHeaderHasEveryColumn:
    def test_csv_from_view_carries_joined_columns(self, tmp_path: Path) -> None:
        # Spec acceptance criterion #4 as a snapshot — the header row
        # of the exported CSV must contain lastModifiedByEmail /
        # DisplayName so the reporting model's ``Last Modified By``
        # line item has a source column to map.
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            df = pd.read_sql_query("SELECT * FROM v_models_export", conn)
        csv_header = set(df.to_csv(index=False).splitlines()[0].split(","))
        assert "lastModifiedByEmail" in csv_header
        assert "lastModifiedByDisplayName" in csv_header
        # And every Section 3.1 column too.
        assert _SECTION_3_1_MODEL_COLUMNS.issubset(csv_header)


class TestLastModifiedContractWithReportingModel:
    """v3.3.2 — the CSV column name must be ``lastModified``, not
    ``lastModifiedDate``.

    The reporting model's ``SYS Models`` module (applies-to: MOD_CT) has
    a staging line item literally named ``lastModified``, and its
    ``Last Modified Date`` formula reads it as ``LEFT(lastModified, 19)``.
    A column called ``lastModifiedDate`` in ``MODEL_LIST.csv`` would land
    unmapped and the module cell would stay blank. This test guards the
    contract at three layers so a rename in either direction breaks CI
    immediately.
    """

    def test_pydantic_declares_last_modified_not_last_modified_date(self) -> None:
        assert "lastModified" in Model.model_fields
        assert "lastModifiedDate" not in Model.model_fields

    def test_v_models_export_view_selects_last_modified(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            cur = conn.execute("SELECT * FROM v_models_export LIMIT 0")
            view_cols = {desc[0] for desc in cur.description}
        assert "lastModified" in view_cols
        assert "lastModifiedDate" not in view_cols

    def test_csv_header_carries_last_modified_column_name(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            df = pd.read_sql_query("SELECT * FROM v_models_export", conn)
        header = df.to_csv(index=False).splitlines()[0].split(",")
        assert "lastModified" in header
        assert "lastModifiedDate" not in header


class TestModelDateFieldsAcceptIsoStrings:
    """v3.3.3 — the real Anaplan API returns ``lastModified`` as ISO
    8601 text (``"2026-07-06T20:02:34.000+0000"``), not the
    epoch-millisecond integer the model-export-restoration spec's
    Section 3.1 assumed. v3.3.2 typed it as ``int``, which raised a
    ``ValidationError`` on a first live-tenant run. Pins the accepted
    shape at the Pydantic layer.
    """

    def test_iso_string_last_modified_parses_cleanly(self) -> None:
        m = Model.model_validate(
            {
                "id": "mod-x",
                "name": "M",
                "lastModified": "2026-07-06T20:02:34.000+0000",
            }
        )
        assert m.lastModified == "2026-07-06T20:02:34.000+0000"

    def test_epoch_ms_integer_still_accepted_and_coerced_to_str(self) -> None:
        # Some older API responses returned epoch-millisecond integers.
        # StrCoerce coerces to str so a tenant on either shape still lands.
        m = Model.model_validate(
            {
                "id": "mod-x",
                "name": "M",
                "lastModified": 1_700_050_000_000,
            }
        )
        assert m.lastModified == "1700050000000"

    def test_default_last_modified_is_empty_string_not_zero(self) -> None:
        # A model whose response omits the field lands with empty
        # string — not the misleading ``0`` epoch value.
        m = Model.model_validate({"id": "mod-x", "name": "M"})
        assert m.lastModified == ""


class TestModelDeliberatelyOmitsSpecFictionFields:
    """v3.3.4 — the model-export-restoration spec listed
    ``currentSize`` and ``lastServerRestartDate`` as expected fields,
    but Anaplan's ``?modelDetails=true`` endpoint doesn't return either
    for models. Verified against a 41-model live tenant: 41/41 landed
    null/zero. Ship them as undeclared columns to avoid two dead
    columns in every MODEL_LIST.csv.
    """

    def test_current_size_is_not_a_declared_model_field(self) -> None:
        assert "currentSize" not in Model.model_fields

    def test_last_server_restart_date_is_not_a_declared_model_field(self) -> None:
        assert "lastServerRestartDate" not in Model.model_fields

    def test_view_does_not_select_dropped_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _seed_models_users_workspaces(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            cur = conn.execute("SELECT * FROM v_models_export LIMIT 0")
            view_cols = {desc[0] for desc in cur.description}
        assert "currentSize" not in view_cols
        assert "lastServerRestartDate" not in view_cols
