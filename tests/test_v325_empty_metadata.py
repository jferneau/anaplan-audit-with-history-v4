"""Regression tests for v3.2.5 — empty metadata tables must not crash the load.

Bug: a tenant with no CloudWorks integrations (or a model with no actions)
produced an empty ``pd.DataFrame([])`` with zero columns. ``to_sql`` then
emitted ``CREATE TABLE cloudworks ()`` → ``OperationalError: near ")":
syntax error``, aborting the whole run.

Fixes verified here:
- ``_metadata_frame`` guarantees the expected columns even for a 0-row result.
- ``load_to_duckdb`` creates those empty tables cleanly (with columns), so
  ``audit_query.sql`` can still join against them.
- The loader also defensively skips a truly column-less frame instead of
  crashing.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import duckdb
import pandas as pd

from anaplan_audit.api.models import Action, CloudWorksIntegration
from anaplan_audit.orchestrator import _metadata_frame
from anaplan_audit.transform.loader import load_to_duckdb


class TestMetadataFrame:
    def test_empty_cloudworks_has_expected_columns(self) -> None:
        df = _metadata_frame([], CloudWorksIntegration)
        assert df.shape[0] == 0
        # Columns the audit_query.sql join relies on must be present.
        for col in ("integrationId", "name", "modelId"):
            assert col in df.columns

    def test_empty_actions_include_extra_columns(self) -> None:
        df = _metadata_frame([], Action, extra=["workspaceId", "model_id"])
        assert df.shape[0] == 0
        assert "model_id" in df.columns
        assert "workspaceId" in df.columns

    def test_populated_rows_gain_missing_extra_columns(self) -> None:
        rows = [{"integrationId": "cw-1", "name": "Sync", "modelId": "m-1"}]
        df = _metadata_frame(rows, CloudWorksIntegration)
        assert len(df) == 1
        # `type` and `workspaceId` weren't in the row but are declared fields.
        assert "type" in df.columns
        assert "workspaceId" in df.columns


class TestEmptyMetadataLoad:
    def test_empty_cloudworks_loads_without_syntax_error(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        datasets = {
            "workspaces": pd.DataFrame([{"id": "w1", "name": "WS"}]),
            "cloudworks": _metadata_frame([], CloudWorksIntegration),  # empty
        }
        # Must not raise "near ')': syntax error".
        load_to_duckdb(db, datasets)

        with closing(duckdb.connect(str(db))) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(cloudworks)").fetchall()}
            count = conn.execute("SELECT COUNT(*) FROM cloudworks").fetchone()[0]
        assert count == 0
        assert {"integrationId", "name", "modelId"}.issubset(cols)

    def test_column_less_frame_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # A raw column-less frame (the pre-fix failure shape) must not crash.
        datasets = {
            "workspaces": pd.DataFrame([{"id": "w1", "name": "WS"}]),
            "cloudworks": pd.DataFrame([]),  # zero columns
        }
        load_to_duckdb(db, datasets)  # should not raise

        with closing(duckdb.connect(str(db))) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_type='BASE TABLE'"
                ).fetchall()
            }
        # workspaces loaded; cloudworks skipped rather than crashing.
        assert "workspaces" in tables
        assert "cloudworks" not in tables
