"""Regression tests for the v3.2 improvement batch.

Covers:
- BUG 5: multi-chunk export download (no more silent truncation).
- BUG 6: Retry-After floors the retry wait.
- ENH 1: import/process tasks are polled; Anaplan-side failures raise.
- ENH 4: workspace/model names resolve to IDs.
- ENH 5: audit-event retention purge.
- ENH 7: fetch_audit_events honors max_events.
- init wizard: writes a settings file; refuses to overwrite.
"""

from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path

import duckdb
import httpx
import pandas as pd
import pytest
import respx
from typer.testing import CliRunner

from anaplan_audit.api.audit import fetch_audit_events
from anaplan_audit.api.client import RateLimitError, _wait_honoring_retry_after
from anaplan_audit.api.integration import download_export_file, run_import
from anaplan_audit.cli import app
from anaplan_audit.exceptions import UnexpectedResponseError
from anaplan_audit.transform.loader import load_to_duckdb, purge_old_audit_events
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"

runner = CliRunner()


class TestMultiChunkDownload:
    """BUG 5 — every chunk is downloaded and concatenated."""

    def test_multiple_chunks_concatenated(self) -> None:
        file_base = f"{BASE}/workspaces/{WS}/models/{MODEL}/files/exp-1"
        with respx.mock:
            respx.get(f"{file_base}/chunks").mock(
                return_value=httpx.Response(
                    200, json={"chunks": [{"id": "0"}, {"id": "1"}, {"id": "2"}]}
                )
            )
            respx.get(f"{file_base}/chunks/0").mock(
                return_value=httpx.Response(200, text="header\nrow1\n")
            )
            respx.get(f"{file_base}/chunks/1").mock(return_value=httpx.Response(200, text="row2\n"))
            respx.get(f"{file_base}/chunks/2").mock(return_value=httpx.Response(200, text="row3\n"))
            with make_client() as client:
                result = download_export_file(client, BASE, WS, MODEL, "exp-1")

        assert result == "header\nrow1\nrow2\nrow3\n"

    def test_empty_chunk_list_falls_back_to_chunk_zero(self) -> None:
        file_base = f"{BASE}/workspaces/{WS}/models/{MODEL}/files/exp-1"
        with respx.mock:
            respx.get(f"{file_base}/chunks").mock(
                return_value=httpx.Response(200, json={"chunks": []})
            )
            respx.get(f"{file_base}/chunks/0").mock(
                return_value=httpx.Response(200, text="only-chunk\n")
            )
            with make_client() as client:
                result = download_export_file(client, BASE, WS, MODEL, "exp-1")

        assert result == "only-chunk\n"


class TestRetryAfterFloor:
    """BUG 6 — Retry-After acts as a lower bound on the computed wait."""

    class _FakeOutcome:
        def __init__(self, exc: BaseException | None) -> None:
            self._exc = exc

        def exception(self) -> BaseException | None:
            return self._exc

    class _FakeState:
        def __init__(self, exc: BaseException | None, attempt: int = 1) -> None:
            self.outcome = TestRetryAfterFloor._FakeOutcome(exc)
            self.attempt_number = attempt

    def test_retry_after_floors_wait(self) -> None:
        exc = RateLimitError("429", retry_after=30.0)
        state = self._FakeState(exc)
        wait = _wait_honoring_retry_after(state)  # type: ignore[arg-type]
        assert wait >= 30.0

    def test_no_retry_after_uses_backoff(self) -> None:
        exc = RateLimitError("429", retry_after=None)
        state = self._FakeState(exc)
        wait = _wait_honoring_retry_after(state)  # type: ignore[arg-type]
        assert 0 <= wait <= 16 + 1  # jittered exponential, capped at 16s


class TestImportPolling:
    """ENH 1 — import tasks are polled and Anaplan-side failures raise."""

    def _mock_import(self, result: dict[str, object]) -> None:
        import_base = f"{BASE}/workspaces/{WS}/models/{MODEL}/imports/imp-1"
        respx.post(f"{import_base}/tasks").mock(
            return_value=httpx.Response(200, json={"task": {"taskId": "t-1"}})
        )
        respx.get(f"{import_base}/tasks/t-1").mock(
            return_value=httpx.Response(
                200,
                json={"task": {"taskId": "t-1", "taskState": "COMPLETE", "result": result}},
            )
        )

    def test_successful_import_returns_task(self) -> None:
        with respx.mock:
            self._mock_import({"successful": True, "failureDumpAvailable": False})
            with make_client() as client:
                task = run_import(client, BASE, WS, MODEL, "imp-1")
        assert task["taskState"] == "COMPLETE"

    def test_unsuccessful_import_raises(self) -> None:
        with respx.mock:
            self._mock_import({"successful": False, "failureDumpAvailable": True})
            with (
                make_client() as client,
                pytest.raises(UnexpectedResponseError, match="unsuccessfully"),
            ):
                run_import(client, BASE, WS, MODEL, "imp-1")

    def test_failed_task_state_raises(self) -> None:
        import_base = f"{BASE}/workspaces/{WS}/models/{MODEL}/imports/imp-1"
        with respx.mock:
            respx.post(f"{import_base}/tasks").mock(
                return_value=httpx.Response(200, json={"task": {"taskId": "t-1"}})
            )
            respx.get(f"{import_base}/tasks/t-1").mock(
                return_value=httpx.Response(
                    200, json={"task": {"taskId": "t-1", "taskState": "FAILED"}}
                )
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError, match="FAILED"):
                run_import(client, BASE, WS, MODEL, "imp-1")


class TestNameResolution:
    """ENH 4 — combos may reference workspaces/models by display name."""

    def test_names_resolve_to_ids(self) -> None:
        from anaplan_audit.config import Settings, WorkspaceModelCombo
        from anaplan_audit.orchestrator import _resolve_names_to_ids

        with respx.mock:
            respx.get(f"{BASE}/workspaces").mock(
                return_value=httpx.Response(
                    200,
                    json={"workspaces": [{"id": "ws-abc", "name": "Finance"}]},
                )
            )
            respx.get(f"{BASE}/workspaces/ws-abc/models").mock(
                return_value=httpx.Response(
                    200,
                    json={"models": [{"id": "m-xyz", "name": "Revenue Model"}]},
                )
            )
            settings = Settings(
                uris={"integrationUri": BASE},  # type: ignore[arg-type]
            )
            combos = [WorkspaceModelCombo(workspaceId="Finance", modelId="revenue model")]
            with make_client() as client:
                resolved = _resolve_names_to_ids(client, settings, combos)

        assert resolved[0].workspaceId == "ws-abc"
        assert resolved[0].modelId == "m-xyz"

    def test_ids_pass_through_unchanged(self) -> None:
        from anaplan_audit.config import Settings, WorkspaceModelCombo
        from anaplan_audit.orchestrator import _resolve_names_to_ids

        with respx.mock:
            respx.get(f"{BASE}/workspaces").mock(
                return_value=httpx.Response(
                    200, json={"workspaces": [{"id": "ws-abc", "name": "Finance"}]}
                )
            )
            respx.get(f"{BASE}/workspaces/ws-abc/models").mock(
                return_value=httpx.Response(
                    200, json={"models": [{"id": "m-xyz", "name": "Revenue Model"}]}
                )
            )
            settings = Settings(uris={"integrationUri": BASE})  # type: ignore[arg-type]
            combos = [WorkspaceModelCombo(workspaceId="ws-abc", modelId="m-xyz")]
            with make_client() as client:
                resolved = _resolve_names_to_ids(client, settings, combos)

        assert resolved[0].workspaceId == "ws-abc"
        assert resolved[0].modelId == "m-xyz"

    def test_unknown_name_raises_config_error(self) -> None:
        from anaplan_audit.config import Settings, WorkspaceModelCombo
        from anaplan_audit.exceptions import ConfigError
        from anaplan_audit.orchestrator import _resolve_names_to_ids

        with respx.mock:
            respx.get(f"{BASE}/workspaces").mock(
                return_value=httpx.Response(200, json={"workspaces": []})
            )
            settings = Settings(uris={"integrationUri": BASE})  # type: ignore[arg-type]
            combos = [WorkspaceModelCombo(workspaceId="Nope", modelId="m")]
            with make_client() as client, pytest.raises(ConfigError, match="Nope"):
                _resolve_names_to_ids(client, settings, combos)


class TestAuditRetention:
    """ENH 5 — audit events beyond the retention window are purged."""

    def test_old_events_purged_new_events_kept(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        now_ms = int(time.time() * 1000)
        three_years_ms = 3 * 365 * 24 * 3600 * 1000
        df = pd.DataFrame(
            [
                {"id": "old", "eventDate": now_ms - three_years_ms},
                {"id": "new", "eventDate": now_ms},
            ]
        )
        load_to_duckdb(db, {"events": df})

        purge_old_audit_events(db, retention_years=2)

        with closing(duckdb.connect(str(db))) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM events").fetchall()]
        assert ids == ["new"]

    def test_zero_retention_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        df = pd.DataFrame([{"id": "e1", "eventDate": 1}])
        load_to_duckdb(db, {"events": df})

        purge_old_audit_events(db, retention_years=0)

        with closing(duckdb.connect(str(db))) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1

    def test_missing_events_table_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "empty.db"
        purge_old_audit_events(db, retention_years=2)  # must not raise


class TestFetchLimit:
    """ENH 7 — max_events caps the fetch."""

    def test_max_events_stops_pagination(self) -> None:
        audit_uri = "https://audit.test.com/audit/api/1"
        page = {
            "response": [{"id": f"evt-{i}", "eventTypeId": "USR-8"} for i in range(100)],
            "meta": {"paging": {}},
        }
        with respx.mock:
            respx.post(url__startswith=f"{audit_uri}/events/search").mock(
                return_value=httpx.Response(200, json=page)
            )
            with make_client() as client:
                events = list(
                    fetch_audit_events(
                        client,
                        audit_uri,
                        since_epoch=0,
                        batch_size=100,
                        max_events=5,
                    )
                )
        assert len(events) == 5


class TestInitWizard:
    """The init command writes a minimal settings file."""

    def test_creates_settings_file(self, tmp_path: Path) -> None:
        out = tmp_path / "settings.json"
        answers = "\n".join(
            [
                "AcmeCorp",  # tenant
                "OAuth",  # auth mode
                "client-123",  # oauth client id
                "Finance",  # source workspace
                "Revenue Model",  # source model
                "ws-target",  # target workspace
                "m-target",  # target model
                "113000000001",  # file id
                "112000000001",  # import id
                "n",  # model history
            ]
        )
        result = runner.invoke(app, ["init", "--output", str(out)], input=answers + "\n")
        assert result.exit_code == 0, result.output

        config = json.loads(out.read_text())
        assert config["anaplanTenantName"] == "AcmeCorp"
        assert config["oauthClientId"] == "client-123"
        assert config["workspaceModelCombos"][0]["workspaceId"] == "Finance"
        assert config["modelHistory"]["enabled"] is False

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        out = tmp_path / "settings.json"
        out.write_text("{}")
        result = runner.invoke(app, ["init", "--output", str(out)])
        assert result.exit_code == 2
        assert "already exists" in result.output
