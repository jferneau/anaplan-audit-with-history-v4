"""Tests for v3.2.10 — treat a process's `successful=false` with no
failure dump and no details as a warning, not a hard failure.

Anaplan flags a process `successful=false` on any non-perfect nested
import (rows ignored, warnings) even when the underlying data landed —
Anaplan's own UI reports this as "completed with warnings". The reason
we ran into this: the real Update Anaplan Audit Environment process ran,
uploaded, and the reporting model was refreshed — but the polled task
still returned `successful=false, failureDumpAvailable=false, details=[]`
and the tool raised, masking a genuine success.

Import tasks keep the strict check — their `successful` signal is
reliable and a bad Anaplan-side load must still surface as exit code 4.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from anaplan_audit.api.integration import run_import, run_process
from anaplan_audit.exceptions import UnexpectedResponseError
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"


_KIND_TO_PATH = {"process": "processes", "import": "imports"}


def _mock_action(kind: str, action_id: str, result: dict[str, object]) -> None:
    base = f"{BASE}/workspaces/{WS}/models/{MODEL}/{_KIND_TO_PATH[kind]}/{action_id}"
    respx.post(f"{base}/tasks").mock(
        return_value=httpx.Response(200, json={"task": {"taskId": "t-1"}})
    )
    respx.get(f"{base}/tasks/t-1").mock(
        return_value=httpx.Response(
            200,
            json={"task": {"taskId": "t-1", "taskState": "COMPLETE", "result": result}},
        )
    )


class TestProcessSoftWarn:
    """Real error we hit: process ran, data landed, but successful=false."""

    def test_process_success_false_with_no_dump_and_no_details_warns_not_raises(self) -> None:
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {"successful": False, "failureDumpAvailable": False, "details": []},
            )
            with make_client() as client:
                # Must NOT raise: this is the "completed with warnings" case.
                task = run_process(client, BASE, WS, MODEL, "P1")
        assert task["taskState"] == "COMPLETE"

    def test_process_success_false_with_no_dump_and_missing_details_key_warns(self) -> None:
        # Guard against `.get("details", [])` returning None on some payloads.
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {"successful": False, "failureDumpAvailable": False},
            )
            with make_client() as client:
                task = run_process(client, BASE, WS, MODEL, "P1")
        assert task["taskState"] == "COMPLETE"


class TestProcessRealFailuresStillRaise:
    """A process with a real failure signal must still exit code 4."""

    def test_failure_dump_available_still_raises(self) -> None:
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {"successful": False, "failureDumpAvailable": True, "details": []},
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError):
                run_process(client, BASE, WS, MODEL, "P1")

    def test_details_present_still_raises(self) -> None:
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {
                    "successful": False,
                    "failureDumpAvailable": False,
                    "details": [{"localMessageText": "Cannot locate import id X"}],
                },
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError):
                run_process(client, BASE, WS, MODEL, "P1")


class TestImportStillStrict:
    """Imports keep the strict check — their success signal is reliable."""

    def test_import_success_false_still_raises_even_with_no_details(self) -> None:
        with respx.mock:
            _mock_action(
                "import",
                "I1",
                {"successful": False, "failureDumpAvailable": False, "details": []},
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError):
                run_import(client, BASE, WS, MODEL, "I1")


class TestNestedResultsFailPropagation:
    """v3.2.12: a nested import failure inside a process must still fail the run.

    Previously the top-level ``successful=false`` with no top-level dump was
    silently accepted; if a nested "Load Last Run" import failed but the
    outer process didn't surface a dump, the tool reported success.
    """

    def test_nested_result_with_failure_dump_raises(self) -> None:
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {
                    "successful": False,
                    "failureDumpAvailable": False,
                    "details": [],
                    "nestedResults": [
                        {"objectName": "Load Users", "successful": True},
                        {
                            "objectName": "Load Last Run",
                            "successful": False,
                            "failureDumpAvailable": True,
                            "details": [],
                        },
                    ],
                },
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError):
                run_process(client, BASE, WS, MODEL, "P1")

    def test_nested_result_with_details_raises(self) -> None:
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {
                    "successful": False,
                    "failureDumpAvailable": False,
                    "details": [],
                    "nestedResults": [
                        {
                            "objectName": "Load Audit Events",
                            "successful": False,
                            "failureDumpAvailable": False,
                            "details": [{"localMessageText": "Mapping error on User"}],
                        },
                    ],
                },
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError):
                run_process(client, BASE, WS, MODEL, "P1")

    def test_nested_all_success_or_soft_warn_does_not_raise(self) -> None:
        # Real "rows ignored" case: nested actions successful, outer wrapper
        # still flips successful=false with no evidence anywhere. Stays a warn.
        with respx.mock:
            _mock_action(
                "process",
                "P1",
                {
                    "successful": False,
                    "failureDumpAvailable": False,
                    "details": [],
                    "nestedResults": [
                        {"objectName": "Load Users", "successful": True},
                        {"objectName": "Load Models", "successful": True},
                    ],
                },
            )
            with make_client() as client:
                task = run_process(client, BASE, WS, MODEL, "P1")
        assert task["taskState"] == "COMPLETE"
