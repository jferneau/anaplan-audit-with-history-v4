"""Tests for the Model History service (fetch_model_history)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from anaplan_audit.api.client import APIClient
from anaplan_audit.auth.models import AuthToken
from anaplan_audit.model_history.history_service import fetch_model_history

BASE = "https://api.anaplan.com/2/0"
WS_ID = "ws001"
MODEL_ID = "m001"
EXPORT_ID = "exp001"
TASK_ID = "task001"


def _make_client() -> APIClient:
    token = AuthToken(
        access_token="test-token",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    return APIClient(token)


@pytest.fixture()
def mock_api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        yield router


class TestFetchModelHistory:
    def test_returns_csv_on_success(self, mock_api: respx.MockRouter) -> None:
        """Happy path: export triggers, completes, returns CSV."""
        csv_content = (
            "date_time_utc,user,description\n"
            "2024-01-01T00:00:00,alice@example.com,Changed formula\n"
        )

        mock_api.get(f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports").mock(
            return_value=httpx.Response(
                200,
                json={"exports": [{"id": EXPORT_ID, "name": "MODEL_HISTORY_EXPORT"}]},
            )
        )
        mock_api.post(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": TASK_ID, "taskState": "NOT_STARTED"}},
            )
        )
        mock_api.get(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks/{TASK_ID}"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": TASK_ID, "taskState": "COMPLETE"}},
            )
        )
        mock_api.get(f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/files/{EXPORT_ID}/chunks").mock(
            return_value=httpx.Response(200, json={"chunks": [{"id": "0"}]})
        )
        mock_api.get(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/files/{EXPORT_ID}/chunks/0"
        ).mock(return_value=httpx.Response(200, text=csv_content))

        with _make_client() as client:
            result = fetch_model_history(
                client=client,
                integration_uri=BASE,
                workspace_id=WS_ID,
                workspace_name="Test WS",
                model_id=MODEL_ID,
                model_name="Test Model",
                export_action_name="MODEL_HISTORY_EXPORT",
                timeout_seconds=60,
            )

        assert result is not None
        assert "date_time_utc" in result
        assert "alice@example.com" in result

    def test_returns_none_when_export_action_not_found(self, mock_api: respx.MockRouter) -> None:
        """No export action matching name → returns None silently."""
        mock_api.get(f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports").mock(
            return_value=httpx.Response(
                200,
                json={"exports": [{"id": "other", "name": "SOME_OTHER_EXPORT"}]},
            )
        )

        with _make_client() as client:
            result = fetch_model_history(
                client=client,
                integration_uri=BASE,
                workspace_id=WS_ID,
                workspace_name="Test WS",
                model_id=MODEL_ID,
                model_name="Test Model",
            )

        assert result is None

    def test_returns_none_when_task_fails(self, mock_api: respx.MockRouter) -> None:
        """Export task enters FAILED state → returns None."""
        mock_api.get(f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports").mock(
            return_value=httpx.Response(
                200,
                json={"exports": [{"id": EXPORT_ID, "name": "MODEL_HISTORY_EXPORT"}]},
            )
        )
        mock_api.post(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": TASK_ID, "taskState": "NOT_STARTED"}},
            )
        )
        mock_api.get(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks/{TASK_ID}"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": TASK_ID, "taskState": "FAILED"}},
            )
        )

        with _make_client() as client:
            result = fetch_model_history(
                client=client,
                integration_uri=BASE,
                workspace_id=WS_ID,
                workspace_name="Test WS",
                model_id=MODEL_ID,
                model_name="Test Model",
                timeout_seconds=60,
            )

        assert result is None

    def test_returns_none_on_timeout(self, mock_api: respx.MockRouter) -> None:
        """Export task stays IN_PROGRESS until timeout → returns None."""
        mock_api.get(f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports").mock(
            return_value=httpx.Response(
                200,
                json={"exports": [{"id": EXPORT_ID, "name": "MODEL_HISTORY_EXPORT"}]},
            )
        )
        mock_api.post(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": TASK_ID, "taskState": "NOT_STARTED"}},
            )
        )
        # Always returns IN_PROGRESS.
        mock_api.get(
            f"{BASE}/workspaces/{WS_ID}/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks/{TASK_ID}"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": TASK_ID, "taskState": "IN_PROGRESS"}},
            )
        )

        with _make_client() as client:
            result = fetch_model_history(
                client=client,
                integration_uri=BASE,
                workspace_id=WS_ID,
                workspace_name="Test WS",
                model_id=MODEL_ID,
                model_name="Test Model",
                timeout_seconds=0,  # Immediate timeout.
            )

        assert result is None
