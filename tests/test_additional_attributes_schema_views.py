"""Milestone 2 + 3 tests — schema migration and staging views.

Covers:
* ``_upsert_events`` creates the extractor's named columns
* Idempotent re-run doesn't error
* Schema version pragma bumps to 2
* Views emit distinct pairs, null / empty codes and names filtered
* Category gating creates only requested views and drops unwanted ones
* First-run guard: views skip cleanly when events table doesn't exist
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd

from anaplan_audit.transform.additional_attributes import (
    ADDITIONAL_ATTRIBUTES_COLUMNS,
)
from anaplan_audit.transform.loader import (
    _EVENTS_SCHEMA_VERSION,
    ensure_staging_views,
    load_to_duckdb,
)


def _events_columns(db_path: Path) -> set[str]:
    with closing(duckdb.connect(str(db_path))) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}


def _sample_events_df() -> pd.DataFrame:
    # A minimal shape the loader accepts: 'id' unique, one nested
    # additionalAttributes column (dotted), plus a few extracted-column
    # candidates that would be populated by the enrichment step upstream.
    return pd.DataFrame(
        [
            {
                "id": "1",
                "eventTypeId": "USR-1",
                "additionalAttributes.appId": "app-uuid-1",
                "app_id": "app-uuid-1",
                "app_name": "Xperience 2025",
                "page_id": "page-uuid-1",
                "page_name": "13 | G&A Expenses",
                "additional_attributes_raw": '{"appId": "app-uuid-1"}',
            },
            {
                "id": "2",
                "eventTypeId": "AUTHZ-1",
                "additionalAttributes.appId": None,
                "app_id": None,
                "app_name": None,
                "page_id": None,
                "page_name": None,
                "additional_attributes_raw": None,
            },
        ]
    )


class TestSchemaMigration:
    def test_events_table_gets_extractor_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        cols = _events_columns(db_path)
        for expected in ADDITIONAL_ATTRIBUTES_COLUMNS:
            assert expected in cols, f"missing extractor column: {expected}"

    def test_schema_version_recorded_as_2(self, tmp_path: Path) -> None:
        # v4: schema version lives in the _schema_meta table (SQLite's
        # PRAGMA user_version has no DuckDB equivalent).
        db_path = tmp_path / "test.db"
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        with closing(duckdb.connect(str(db_path))) as conn:
            version = conn.execute(
                "SELECT value FROM _schema_meta WHERE key = 'events_schema_version'"
            ).fetchone()[0]
        assert int(version) == _EVENTS_SCHEMA_VERSION == 2

    def test_second_load_is_idempotent(self, tmp_path: Path) -> None:
        # Re-running the same load must not raise "duplicate column"
        # or leave the schema in a broken state. Simulates a real
        # nightly re-run on an existing DB.
        db_path = tmp_path / "test.db"
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        cols = _events_columns(db_path)
        assert "app_id" in cols
        assert "additional_attributes_raw" in cols


class TestStagingViews:
    def test_all_views_created_by_default(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        ensure_staging_views(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            views = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_type='VIEW'"
                ).fetchall()
            }
        expected = {
            "v_ux_app",
            "v_ux_page",
            "v_cw_integration",
            "v_action",
            "v_process",
            "v_role",
            "v_target_user",
        }
        # Subset — ``load_to_duckdb`` also creates ``v_models_export``
        # (v3.3.1). This test only cares that the seven additionalAttributes
        # staging views exist, not that they are the only views in the DB.
        assert expected.issubset(views)

    def test_view_returns_distinct_populated_pairs(self, tmp_path: Path) -> None:
        # Three events: two with the same app, one with different app.
        # View must yield exactly two distinct (code, name) rows.
        db_path = tmp_path / "test.db"
        df = pd.DataFrame(
            [
                {
                    "id": str(i),
                    "additionalAttributes.appId": aid,
                    "app_id": aid,
                    "app_name": aname,
                }
                for i, (aid, aname) in enumerate(
                    [
                        ("app-1", "Xperience 2025"),
                        ("app-1", "Xperience 2025"),
                        ("app-2", "Q4 Forecast"),
                    ]
                )
            ]
        )
        load_to_duckdb(db_path, {"events": df})
        ensure_staging_views(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            rows = conn.execute("SELECT code, name FROM v_ux_app ORDER BY code").fetchall()
        assert rows == [("app-1", "Xperience 2025"), ("app-2", "Q4 Forecast")]

    def test_ux_page_view_is_hierarchical_with_parent_app(self, tmp_path: Path) -> None:
        # UX pages nest under their app: v_ux_page carries parent_code = app_id,
        # and a page with no app_id is dropped (a child list rejects a
        # parentless item). v_ux_app stays flat (no parent_code column).
        db_path = tmp_path / "test.db"
        df = pd.DataFrame(
            [
                {"id": "1", "app_id": "app-1", "app_name": "Sales App",
                 "page_id": "pg-1", "page_name": "Overview"},
                {"id": "2", "app_id": "app-1", "app_name": "Sales App",
                 "page_id": "pg-2", "page_name": "Detail"},
                # No parent app_id → must be dropped from the page view.
                {"id": "3", "app_id": None, "app_name": None,
                 "page_id": "pg-3", "page_name": "Orphan Page"},
            ]
        )
        load_to_duckdb(db_path, {"events": df})
        ensure_staging_views(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            pages = conn.execute(
                "SELECT code, name, parent_code FROM v_ux_page ORDER BY code"
            ).fetchall()
            app_cols = {r[1] for r in conn.execute("PRAGMA table_info(v_ux_app)").fetchall()}
        assert pages == [("pg-1", "Overview", "app-1"), ("pg-2", "Detail", "app-1")]
        assert "parent_code" not in app_cols  # apps stay flat

    def test_view_filters_null_and_empty_codes_and_names(self, tmp_path: Path) -> None:
        # Spec Acceptance criterion #6: no orphan rows.
        db_path = tmp_path / "test.db"
        df = pd.DataFrame(
            [
                {"id": "1", "app_id": "keep", "app_name": "Keep"},
                {"id": "2", "app_id": None, "app_name": "Orphan"},
                {"id": "3", "app_id": "orphan", "app_name": None},
                {"id": "4", "app_id": "", "app_name": "Empty"},
                {"id": "5", "app_id": "empty-name", "app_name": ""},
            ]
        )
        load_to_duckdb(db_path, {"events": df})
        ensure_staging_views(db_path)
        with closing(duckdb.connect(str(db_path))) as conn:
            rows = conn.execute("SELECT code, name FROM v_ux_app").fetchall()
        assert rows == [("keep", "Keep")]

    def test_category_gating_creates_only_requested_views(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        ensure_staging_views(db_path, view_categories={"uxAppPage", "action"})
        with closing(duckdb.connect(str(db_path))) as conn:
            views = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_type='VIEW'"
                ).fetchall()
            }
        # uxAppPage produces both v_ux_app and v_ux_page.
        additional_attribute_views = views - {"v_models_export"}
        assert additional_attribute_views == {"v_ux_app", "v_ux_page", "v_action"}

    def test_disabling_a_category_drops_its_stale_view(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_duckdb(db_path, {"events": _sample_events_df()})
        # First pass creates every view.
        ensure_staging_views(db_path)
        # Second pass narrows to only uxAppPage; the others must be
        # dropped so operators don't stare at data from a prior config.
        ensure_staging_views(db_path, view_categories={"uxAppPage"})
        with closing(duckdb.connect(str(db_path))) as conn:
            views = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_type='VIEW'"
                ).fetchall()
            }
        # ``v_models_export`` is created by ``load_to_duckdb`` — filter it
        # out; this test only cares about the staging-view lifecycle.
        additional_attribute_views = views - {"v_models_export"}
        assert additional_attribute_views == {"v_ux_app", "v_ux_page"}

    def test_first_run_no_events_table_is_a_noop(self, tmp_path: Path) -> None:
        # events table doesn't exist yet; call must not raise. This
        # exercises the first-nightly-run codepath before any batch has
        # landed on a fresh tenant.
        db_path = tmp_path / "test.db"
        # Just open+close so the DB file exists but has no tables.
        with closing(duckdb.connect(str(db_path))):
            pass
        ensure_staging_views(db_path)  # must not raise
        with closing(duckdb.connect(str(db_path))) as conn:
            views = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()
        assert views == []
