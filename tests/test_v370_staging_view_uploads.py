"""v3.7.0 regression tests — additionalAttributes staging view CSV uploads.

The seven staging views (v_ux_app, v_ux_page, v_cw_integration,
v_action, v_process, v_role, v_target_user) have existed in SQLite
since v3.3.0 but were never pushed to Anaplan. This closes the gap —
each configured view uploads as a two-column ``(code, name)`` CSV to
the file source named in ``TargetModelObjects``, ready for the
reporting model's list imports.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pandas as pd

from anaplan_audit.api.models import ImportDataSource
from anaplan_audit.config import (
    AdditionalAttributesCategoryConfig,
    AdditionalAttributesConfig,
    AnaplanUris,
    Settings,
    TargetModelConfig,
    TargetModelObjects,
)
from anaplan_audit.transform.loader import ensure_staging_views, load_to_duckdb
from anaplan_audit.upload import _STAGING_VIEW_UPLOADS, _upload_staging_views


def _seed_events(db_path: Path) -> None:
    df = pd.DataFrame(
        [
            {
                "id": "e1",
                "app_id": "app-1",
                "app_name": "Xperience 2025",
                "page_id": "page-1",
                "page_name": "13 | G&A Expenses",
                "integration_id": "int-1",
                "integration_name": "Salesforce Sync",
                "action_id": "act-1",
                "action_name": "Load Users",
                "process_id": "proc-1",
                "process_name": "Nightly Refresh",
                "role_id": "",
                "role_name": "",
                "target_user_id": "",
                "target_user_name": "",
            },
        ]
    )
    load_to_duckdb(db_path, {"events": df})
    ensure_staging_views(
        db_path,
        view_categories={"uxAppPage", "cwIntegration", "action", "process"},
    )


def _make_settings(
    *,
    ux_app_filename: str = "v_ux_app.csv",
    cw_int_filename: str = "",
    aa_enabled: bool = True,
    ux_app_emit: bool = True,
    cw_emit: bool = True,
) -> Settings:
    return Settings(
        anaplanTenantName="test",
        authenticationMode="basic",
        basic_username="u",
        basic_password="p",
        uris=AnaplanUris(),
        targetAnaplanModel=TargetModelConfig(
            workspaceId="ws-1",
            modelId="mod-1",
            objects=TargetModelObjects(
                uxAppListFileName=ux_app_filename,
                cwIntegrationListFileName=cw_int_filename,
            ),
        ),
        additionalAttributes=AdditionalAttributesConfig(
            enabled=aa_enabled,
            categories={
                "uxAppPage": AdditionalAttributesCategoryConfig(
                    enabled=True, emitLists=ux_app_emit
                ),
                "cwIntegration": AdditionalAttributesCategoryConfig(
                    enabled=True, emitLists=cw_emit
                ),
                "action": AdditionalAttributesCategoryConfig(enabled=True, emitLists=False),
                "process": AdditionalAttributesCategoryConfig(enabled=True, emitLists=False),
                "role": AdditionalAttributesCategoryConfig(enabled=False, emitLists=False),
                "targetUser": AdditionalAttributesCategoryConfig(enabled=False, emitLists=False),
            },
        ),
    )


def _fake_client(files: list[ImportDataSource]) -> MagicMock:
    """A test client stub whose ``list_files`` shim returns the given files."""
    return MagicMock()


class TestStagingViewUploadsWiring:
    def test_all_seven_views_are_declared(self) -> None:
        # Contract check: the upload table matches the seven views the
        # additionalAttributes extractor creates.
        view_names = {v for v, _, _ in _STAGING_VIEW_UPLOADS}
        assert view_names == {
            "v_ux_app",
            "v_ux_page",
            "v_cw_integration",
            "v_action",
            "v_process",
            "v_role",
            "v_target_user",
        }

    def test_category_gate_is_declared_for_every_view(self) -> None:
        # Every entry must reference an AdditionalAttributesConfig
        # category name (guards against typos when adding a new view).
        cats = {c for _, _, c in _STAGING_VIEW_UPLOADS}
        assert cats == {"uxAppPage", "cwIntegration", "action", "process", "role", "targetUser"}

    def test_target_model_objects_has_every_filename_field(self) -> None:
        # If a filename field is missing on TargetModelObjects, the
        # opt-out check (``getattr``) would silently return ``""`` and
        # the view would never upload — hard to notice. This guards
        # against that class of typo.
        obj = TargetModelObjects()
        for _, file_attr, _ in _STAGING_VIEW_UPLOADS:
            assert hasattr(obj, file_attr), f"missing config field: {file_attr}"


class TestUploadStagingViews:
    def test_blank_filename_opts_out_of_upload(self, tmp_path: Path, monkeypatch: object) -> None:
        import anaplan_audit.upload as upload_mod

        db = tmp_path / "test.db"
        _seed_events(db)
        settings = _make_settings(ux_app_filename="", cw_int_filename="")

        uploaded_files: list[tuple[str, str]] = []
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "list_files",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "upload_file_chunks",
            lambda client, uri, ws, model, fid, data: uploaded_files.append((fid, data)),
        )

        import structlog

        _upload_staging_views(
            MagicMock(),
            settings,
            db,
            structlog.get_logger().bind(test=True),
        )
        # Blank filenames → nothing uploaded even though views exist.
        assert uploaded_files == []

    def test_disabled_top_level_skips_everything(self, tmp_path: Path, monkeypatch: object) -> None:
        import anaplan_audit.upload as upload_mod

        db = tmp_path / "test.db"
        _seed_events(db)
        settings = _make_settings(aa_enabled=False)

        uploaded_files: list[str] = []
        monkeypatch.setattr(upload_mod, "list_files", lambda *a, **k: [])  # type: ignore[attr-defined]
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "upload_file_chunks",
            lambda *a, **k: uploaded_files.append("!"),
        )

        import structlog

        _upload_staging_views(
            MagicMock(),
            settings,
            db,
            structlog.get_logger().bind(test=True),
        )
        assert uploaded_files == []

    def test_emit_lists_false_skips_that_view(self, tmp_path: Path, monkeypatch: object) -> None:
        import anaplan_audit.upload as upload_mod

        db = tmp_path / "test.db"
        _seed_events(db)
        # Both file-names set, but cwIntegration.emitLists=false → only
        # uxAppPage uploads (two views: v_ux_app + v_ux_page).
        settings = _make_settings(
            ux_app_filename="v_ux_app.csv",
            cw_int_filename="v_cw_integration.csv",
            cw_emit=False,
        )

        uploaded_names: list[str] = []
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "list_files",
            lambda *a, **k: [
                ImportDataSource(id="f-ux-app", name="v_ux_app.csv"),
                ImportDataSource(id="f-cw", name="v_cw_integration.csv"),
            ],
        )
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "upload_file_chunks",
            lambda client, uri, ws, model, fid, data: uploaded_names.append(fid),
        )

        import structlog

        _upload_staging_views(
            MagicMock(),
            settings,
            db,
            structlog.get_logger().bind(test=True),
        )
        # Only the ux-app file was pushed; cw was gated by emitLists=false.
        assert uploaded_names == ["f-ux-app"]

    def test_view_csv_carries_code_and_name_from_seed(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        import anaplan_audit.upload as upload_mod

        db = tmp_path / "test.db"
        _seed_events(db)
        settings = _make_settings(ux_app_filename="v_ux_app.csv")

        uploaded: list[str] = []
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "list_files",
            lambda *a, **k: [ImportDataSource(id="f", name="v_ux_app.csv")],
        )
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "upload_file_chunks",
            lambda client, uri, ws, model, fid, data: uploaded.append(data),
        )

        import structlog

        _upload_staging_views(
            MagicMock(),
            settings,
            db,
            structlog.get_logger().bind(test=True),
        )
        assert len(uploaded) == 1
        # Two-column CSV with the values seeded above.
        assert "code,name" in uploaded[0]
        assert "app-1" in uploaded[0]
        assert "Xperience 2025" in uploaded[0]

    def test_missing_file_source_skips_but_does_not_raise(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        # File source name doesn't exist in the target model — opt-in
        # per operator; skip with a warning-log rather than crash.
        import anaplan_audit.upload as upload_mod

        db = tmp_path / "test.db"
        _seed_events(db)
        settings = _make_settings(ux_app_filename="v_ux_app.csv")

        uploaded: list[str] = []
        monkeypatch.setattr(upload_mod, "list_files", lambda *a, **k: [])  # type: ignore[attr-defined]
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "upload_file_chunks",
            lambda *a, **k: uploaded.append("!"),
        )

        import structlog

        _upload_staging_views(
            MagicMock(),
            settings,
            db,
            structlog.get_logger().bind(test=True),
        )
        # Nothing uploaded because the file source didn't resolve.
        assert uploaded == []

    def test_missing_view_in_sqlite_pushes_empty_csv(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        # Fresh DB with no events / views yet — must still push an empty
        # CSV so the reporting model's import can clear its list cleanly.
        import anaplan_audit.upload as upload_mod

        db = tmp_path / "empty.db"
        with closing(duckdb.connect(str(db))):
            pass
        settings = _make_settings(ux_app_filename="v_ux_app.csv")

        uploaded: list[str] = []
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "list_files",
            lambda *a, **k: [ImportDataSource(id="f", name="v_ux_app.csv")],
        )
        monkeypatch.setattr(  # type: ignore[attr-defined]
            upload_mod,
            "upload_file_chunks",
            lambda client, uri, ws, model, fid, data: uploaded.append(data),
        )

        import structlog

        _upload_staging_views(
            MagicMock(),
            settings,
            db,
            structlog.get_logger().bind(test=True),
        )
        # One empty header-only CSV pushed.
        assert len(uploaded) == 1
        assert uploaded[0].strip() == "code,name"
