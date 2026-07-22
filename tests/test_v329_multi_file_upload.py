"""Tests for v3.2.9 — v1-compatible multi-file + process upload mode.

The OEG Audit Reporting Model uploads eight per-table CSVs then runs a
process to stitch them together. This test suite covers the routing
(processName -> multi-file, else single-file), the CSV uploads read from
SQLite, and the clear ConfigError on a typo'd process name.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from anaplan_audit.api.models import ImportAction, ImportDataSource, Process
from anaplan_audit.config import Settings
from anaplan_audit.exceptions import ConfigError
from tests.conftest import make_client, seed_tables


def _write_metadata_db(db_path: Path) -> None:
    seed_tables(
        db_path,
        {
            "workspaces": pd.DataFrame([{"id": "w1", "name": "WS"}]),
            "users": pd.DataFrame([{"id": "u1", "userName": "a@b.co"}]),
            "models": pd.DataFrame([{"id": "m1", "name": "Model"}]),
            "actions": pd.DataFrame(
                [{"id": "a1", "name": "Import A", "type": "import", "model_id": "m1"}]
            ),
            "cloudworks": pd.DataFrame([{"integrationId": "cw1", "name": "Sync"}]),
            "act_codes": pd.DataFrame([{"Event Code": "USR-8", "Event Message": "Login"}]),
        },
    )


def _settings_multi_file() -> Settings:
    return Settings(
        targetAnaplanModel={  # type: ignore[arg-type]
            "workspaceId": "w1",
            "modelId": "m1",
            "objects": {"processName": "Update Anaplan Audit Environment"},
        }
    )


class TestRouting:
    def test_process_name_routes_to_multi_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """processName set -> multi-file uploader is used, not the single-file one."""
        from anaplan_audit import upload as upload_mod

        db = tmp_path / "test.db"
        _write_metadata_db(db)

        called: dict[str, bool] = {"single": False, "multi": False}

        def _fake_single(*a: object, **k: object) -> None:
            called["single"] = True

        def _fake_multi(*a: object, **k: object) -> None:
            called["multi"] = True

        monkeypatch.setattr(upload_mod, "_upload_single_file", _fake_single)
        monkeypatch.setattr(upload_mod, "_upload_via_process", _fake_multi)
        monkeypatch.setattr(upload_mod, "list_files", lambda *a, **k: [])
        monkeypatch.setattr(upload_mod, "list_imports", lambda *a, **k: [])
        monkeypatch.setattr(upload_mod, "_update_last_run", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_upload_last_run_to_anaplan", lambda *a, **k: None)

        with make_client() as client:
            upload_mod.upload_audit_data(
                client, pd.DataFrame([{"AUDIT_ID": "1"}]), _settings_multi_file(), db_path=db
            )

        assert called["multi"] is True
        assert called["single"] is False


class TestMultiFileUpload:
    def test_uploads_all_csvs_then_runs_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anaplan_audit import upload as upload_mod

        db = tmp_path / "test.db"
        _write_metadata_db(db)

        # The target model advertises all 8 file sources and the process.
        file_defs = [
            ("AUDIT_LOG.csv", "F_AUDIT"),
            ("USER_LIST.csv", "F_USERS"),
            ("WORKSPACE_LIST.csv", "F_WS"),
            ("MODEL_LIST.csv", "F_MODELS"),
            ("ACTION_LIST.csv", "F_ACTIONS"),
            ("FILE_LIST.csv", "F_FILES"),
            ("CLOUDWORKS_LIST.csv", "F_CW"),
            ("ACTIVITY_CODES.csv", "F_AC"),
        ]
        monkeypatch.setattr(
            upload_mod,
            "list_files",
            lambda *a, **k: [ImportDataSource(id=fid, name=fname) for fname, fid in file_defs],
        )
        monkeypatch.setattr(upload_mod, "list_imports", lambda *a, **k: [])
        monkeypatch.setattr(
            upload_mod,
            "list_processes",
            lambda *a, **k: [Process(id="P1", name="Update Anaplan Audit Environment")],
        )

        uploads: list[str] = []
        monkeypatch.setattr(
            upload_mod,
            "upload_file_chunks",
            lambda client, uri, ws, model, file_id, data: uploads.append(file_id),
        )
        process_calls: list[str] = []
        monkeypatch.setattr(
            upload_mod,
            "run_process",
            lambda client, uri, ws, model, pid, **kwargs: process_calls.append(pid),
        )
        monkeypatch.setattr(upload_mod, "_update_last_run", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_upload_last_run_to_anaplan", lambda *a, **k: None)

        with make_client() as client:
            upload_mod.upload_audit_data(
                client,
                pd.DataFrame([{"AUDIT_ID": "evt-1"}]),
                _settings_multi_file(),
                db_path=db,
            )

        # Audit CSV + 6 metadata CSVs uploaded (files table is optional/absent
        # in this fixture — 7 total).
        assert "F_AUDIT" in uploads
        assert "F_USERS" in uploads
        assert "F_WS" in uploads
        assert "F_MODELS" in uploads
        assert "F_ACTIONS" in uploads
        assert "F_CW" in uploads
        assert "F_AC" in uploads
        # Process ran once at the end with the resolved ID.
        assert process_calls == ["P1"]

    def test_missing_process_name_raises_with_available_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anaplan_audit import upload as upload_mod

        db = tmp_path / "test.db"
        _write_metadata_db(db)

        monkeypatch.setattr(upload_mod, "list_files", lambda *a, **k: [])
        monkeypatch.setattr(upload_mod, "list_imports", lambda *a, **k: [])
        monkeypatch.setattr(
            upload_mod,
            "list_processes",
            lambda *a, **k: [Process(id="P2", name="Some Other Process")],
        )
        monkeypatch.setattr(upload_mod, "upload_file_chunks", lambda *a, **k: None)

        with make_client() as client, pytest.raises(ConfigError, match="Some Other Process"):
            upload_mod.upload_audit_data(
                client,
                pd.DataFrame([{"AUDIT_ID": "1"}]),
                _settings_multi_file(),
                db_path=db,
            )


class TestBackwardCompat:
    def test_single_file_mode_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No processName -> falls through to the original single-file path."""
        from anaplan_audit import upload as upload_mod

        settings = Settings(
            targetAnaplanModel={  # type: ignore[arg-type]
                "workspaceId": "w1",
                "modelId": "m1",
                "objects": {
                    "auditFileName": "Audit Data.csv",
                    "auditImportName": "Load Audit Data",
                },
            }
        )

        monkeypatch.setattr(
            upload_mod,
            "list_files",
            lambda *a, **k: [ImportDataSource(id="F1", name="Audit Data.csv")],
        )
        monkeypatch.setattr(
            upload_mod,
            "list_imports",
            lambda *a, **k: [ImportAction(id="I1", name="Load Audit Data")],
        )
        captured: dict[str, str] = {}
        monkeypatch.setattr(
            upload_mod,
            "upload_and_import",
            lambda client, uri, ws, model, file_id, import_id, data: captured.update(
                file_id=file_id, import_id=import_id
            ),
        )
        monkeypatch.setattr(upload_mod, "_update_last_run", lambda *a, **k: None)
        monkeypatch.setattr(upload_mod, "_upload_last_run_to_anaplan", lambda *a, **k: None)

        with make_client() as client:
            upload_mod.upload_audit_data(client, pd.DataFrame([{"AUDIT_ID": "1"}]), settings)

        assert captured == {"file_id": "F1", "import_id": "I1"}
