"""v4 — model history uploads to its own target model, separate from audit.

The audit reporting model and the model-history model are distinct Anaplan
models (a UX page can show both; history grows a model far faster than
audit). These tests pin the routing: ``_run_model_history`` must upload to
``modelHistory.targetAnaplanModel``, never to ``targetAnaplanModel``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from anaplan_audit import orchestrator
from anaplan_audit.config import Settings
from tests.conftest import make_client


def _settings_split_targets() -> Settings:
    return Settings(
        auditEnabled=False,
        targetAnaplanModel={  # type: ignore[arg-type]
            "workspaceId": "audit-ws",
            "modelId": "audit-model",
        },
        modelHistory={  # type: ignore[arg-type]
            "enabled": True,
            "targetAnaplanModel": {"workspaceId": "mh-ws", "modelId": "mh-model"},
        },
    )


def test_model_history_uploads_to_its_own_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_upload(**kwargs: Any) -> None:
        captured.update(kwargs)

    # Capture the upload target; neutralize the side-effectful neighbours so
    # the test needs no network and no real Anaplan model.
    monkeypatch.setattr(orchestrator, "upload_model_history", _fake_upload)
    monkeypatch.setattr(orchestrator, "backup_database", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "purge_old_history", lambda *a, **k: None)

    settings = _settings_split_targets()
    db_path = tmp_path / "audit.duckdb"

    with make_client() as client:
        orchestrator._run_model_history(
            client,
            settings,
            db_path,
            structlog.get_logger(),
            combos=[],  # no exports to run — we only assert the upload target
            ws_names={"mh-ws": "MH Workspace"},
            model_names={"mh-model": "MH Model"},
        )

    assert captured["workspace_id"] == "mh-ws"
    assert captured["model_id"] == "mh-model"
    # Explicitly NOT the audit target.
    assert captured["workspace_id"] != settings.targetAnaplanModel.workspaceId
    assert captured["model_id"] != settings.targetAnaplanModel.modelId
    # Process name still comes from the model-history config block.
    assert captured["process_name"] == settings.modelHistory.anaplanProcess
