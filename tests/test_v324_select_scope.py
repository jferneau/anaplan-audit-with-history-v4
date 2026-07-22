"""Regression tests for v3.2.4 — `select` scoping of audit metadata.

Bug: in ``select`` mode the audit metadata fetch listed every model in the
selected workspaces and called the actions/processes endpoints on all of
them — not just the selected models. A model the user hadn't selected
(archived, inaccessible, being copied) returned 404 and crashed the run.

Fixes verified here:
- Actions/processes are fetched ONLY for selected (workspace, model) pairs.
- A non-selected model's actions endpoint is never called.
- A selected-but-inaccessible model is skipped, not fatal.
"""

from __future__ import annotations

import httpx
import respx

from anaplan_audit.config import Settings, WorkspaceModelCombo
from anaplan_audit.orchestrator import _fetch_metadata
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
SCIM = "https://scim.test.com"
CW = "https://cw.test.com/2/0"

WS = "W1"
SELECTED = "M-SELECTED"
OTHER = "M-OTHER"


def _settings() -> Settings:
    return Settings(
        uris={  # type: ignore[arg-type]
            "integrationUri": BASE,
            "scimUri": SCIM,
            "cloudWorksUri": CW,
        }
    )


def _mock_common() -> None:
    """Mock the workspace/user/cloudworks/model listings shared by every test."""
    respx.get(f"{BASE}/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [{"id": WS, "name": "WS One"}]})
    )
    respx.get(f"{SCIM}/Users").mock(
        return_value=httpx.Response(200, json={"Resources": [], "totalResults": 0})
    )
    respx.get(f"{CW}/integrations").mock(
        return_value=httpx.Response(200, json={"integrations": []})
    )
    respx.get(f"{BASE}/workspaces/{WS}/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"id": SELECTED, "name": "Selected Model"},
                    {"id": OTHER, "name": "Other Model"},
                ]
            },
        )
    )


class TestSelectScopesActions:
    def test_only_selected_model_actions_are_fetched(self) -> None:
        with respx.mock:
            _mock_common()
            respx.get(f"{BASE}/workspaces/{WS}/models/{SELECTED}/actions").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "actions": [
                            # Real API returns the kind as ``actionType``, not ``type``.
                            {"id": "a1", "name": "Import A", "actionType": "IMPORT"}
                        ]
                    },
                )
            )
            respx.get(f"{BASE}/workspaces/{WS}/models/{SELECTED}/processes").mock(
                return_value=httpx.Response(200, json={"processes": [{"id": "p1", "name": "Proc"}]})
            )
            respx.get(f"{BASE}/workspaces/{WS}/models/{SELECTED}/files").mock(
                return_value=httpx.Response(200, json={"files": [{"id": "f1", "name": "DATA.csv"}]})
            )
            # The non-selected model's actions/processes must NEVER be called.
            other_actions = respx.get(f"{BASE}/workspaces/{WS}/models/{OTHER}/actions").mock(
                return_value=httpx.Response(404, json={"status": {"code": 404}})
            )
            other_processes = respx.get(f"{BASE}/workspaces/{WS}/models/{OTHER}/processes").mock(
                return_value=httpx.Response(404, json={"status": {"code": 404}})
            )

            combos = [WorkspaceModelCombo(workspaceId=WS, modelId=SELECTED)]
            with make_client() as client:
                datasets, _ws_names, model_names = _fetch_metadata(client, _settings(), combos)

        # The non-selected model was never queried — no 404, no crash.
        assert not other_actions.called
        assert not other_processes.called

        # Actions came only from the selected model.
        actions = datasets["actions"]
        assert len(actions) == 1
        assert actions.iloc[0]["model_id"] == SELECTED
        # The action kind (API ``actionType``) is surfaced on the ``type`` column
        # the SYS Actions import maps.
        assert actions.iloc[0]["type"] == "IMPORT"

        # v4 — actions / processes / files share ONE snake_case provenance
        # contract (workspace_id / model_id / workspace_name / model_name), so
        # SYS Files / SYS Actions / SYS Processes map identically and match the
        # model_history tables. No stale camelCase keys survive.
        provenance = {"workspace_id", "model_id", "workspace_name", "model_name"}
        for name in ("actions", "processes", "files"):
            frame = datasets[name]
            assert len(frame) == 1, name
            assert provenance <= set(frame.columns), name
            assert "workspaceId" not in frame.columns and "modelId" not in frame.columns, name
            row = frame.iloc[0]
            assert row["workspace_id"] == WS
            assert row["model_id"] == SELECTED
            assert row["workspace_name"] == "WS One"
            assert row["model_name"] == "Selected Model"

        # Both models still appear in the models table / name lookup (cheap,
        # helps resolve names in the report) — only the per-model action calls
        # are scoped.
        assert set(model_names) == {SELECTED, OTHER}
        assert len(datasets["models"]) == 2

    def test_selected_but_inaccessible_model_is_skipped_not_fatal(self) -> None:
        with respx.mock:
            _mock_common()
            # The selected model itself 404s on actions — must be skipped.
            respx.get(f"{BASE}/workspaces/{WS}/models/{SELECTED}/actions").mock(
                return_value=httpx.Response(404, json={"status": {"code": 404}})
            )

            combos = [WorkspaceModelCombo(workspaceId=WS, modelId=SELECTED)]
            with make_client() as client:
                datasets, _ws_names, _model_names = _fetch_metadata(client, _settings(), combos)

        # No crash; the failed model just contributes no actions.
        assert len(datasets["actions"]) == 0

    def test_unreachable_workspace_is_skipped(self) -> None:
        with respx.mock:
            respx.get(f"{BASE}/workspaces").mock(
                return_value=httpx.Response(200, json={"workspaces": [{"id": WS, "name": "WS"}]})
            )
            respx.get(f"{SCIM}/Users").mock(
                return_value=httpx.Response(200, json={"Resources": [], "totalResults": 0})
            )
            respx.get(f"{CW}/integrations").mock(
                return_value=httpx.Response(200, json={"integrations": []})
            )
            # Listing models for the workspace fails outright.
            respx.get(f"{BASE}/workspaces/{WS}/models").mock(
                return_value=httpx.Response(403, json={"status": {"code": 403}})
            )

            combos = [WorkspaceModelCombo(workspaceId=WS, modelId=SELECTED)]
            with make_client() as client:
                datasets, _ws_names, _model_names = _fetch_metadata(client, _settings(), combos)

        # Whole workspace skipped, run continues with empty model metadata.
        assert len(datasets["models"]) == 0
        assert len(datasets["actions"]) == 0
