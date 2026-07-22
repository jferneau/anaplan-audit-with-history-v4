"""Tests for v3.2.8 — resolve audit file/import by NAME, not brittle IDs.

Import and file IDs change when a model is copied or rebuilt. Referencing
them by name and resolving to IDs at runtime keeps the config stable.
"""

from __future__ import annotations

import pytest
import structlog

from anaplan_audit.api.models import ImportAction, ImportDataSource
from anaplan_audit.config import Settings
from anaplan_audit.exceptions import ConfigError
from anaplan_audit.upload import _resolve_object_id, upload_audit_data
from tests.conftest import make_client

_LOG = structlog.get_logger()


class TestResolveObjectId:
    def test_name_resolves_to_id(self) -> None:
        resolved = _resolve_object_id(
            "import",
            "Load Audit Data",
            "112FALLBACK",
            {"Load Audit Data": "112REAL"},
            required=True,
            log=_LOG,
        )
        assert resolved == "112REAL"

    def test_blank_name_uses_fallback_id(self) -> None:
        resolved = _resolve_object_id(
            "import", "", "112FALLBACK", {"Something Else": "999"}, required=True, log=_LOG
        )
        assert resolved == "112FALLBACK"

    def test_required_name_not_found_raises(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            _resolve_object_id(
                "import", "Nope", "", {"Load Audit Data": "112REAL"}, required=True, log=_LOG
            )

    def test_optional_name_not_found_returns_empty(self) -> None:
        resolved = _resolve_object_id(
            "file", "Missing", "", {"Other": "1"}, required=False, log=_LOG
        )
        assert resolved == ""


class TestUploadResolvesByName:
    def test_upload_uses_resolved_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pandas as pd

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
            lambda *a, **k: [ImportDataSource(id="113REAL", name="Audit Data.csv")],
        )
        monkeypatch.setattr(
            upload_mod,
            "list_imports",
            lambda *a, **k: [ImportAction(id="112REAL", name="Load Audit Data")],
        )
        captured: dict[str, str] = {}

        def _fake_upload_and_import(client, uri, ws, model, file_id, import_id, data):  # noqa: ANN001, ANN202
            captured["file_id"] = file_id
            captured["import_id"] = import_id

        monkeypatch.setattr(upload_mod, "upload_and_import", _fake_upload_and_import)

        df = pd.DataFrame([{"AUDIT_ID": "evt-1"}])
        with make_client() as client:
            upload_audit_data(client, df, settings)

        # The stale example IDs are gone; names resolved to the live IDs.
        assert captured["file_id"] == "113REAL"
        assert captured["import_id"] == "112REAL"

    def test_upload_missing_import_name_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pandas as pd

        from anaplan_audit import upload as upload_mod

        settings = Settings(
            targetAnaplanModel={  # type: ignore[arg-type]
                "workspaceId": "w1",
                "modelId": "m1",
                "objects": {
                    "auditFileName": "Audit Data.csv",
                    "auditImportName": "Typo Import Name",
                },
            }
        )
        monkeypatch.setattr(
            upload_mod,
            "list_files",
            lambda *a, **k: [ImportDataSource(id="113REAL", name="Audit Data.csv")],
        )
        monkeypatch.setattr(
            upload_mod,
            "list_imports",
            lambda *a, **k: [ImportAction(id="112REAL", name="Load Audit Data")],
        )

        df = pd.DataFrame([{"AUDIT_ID": "evt-1"}])
        with make_client() as client, pytest.raises(ConfigError, match="Typo Import Name"):
            upload_audit_data(client, df, settings)
