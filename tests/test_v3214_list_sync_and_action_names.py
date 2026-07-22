"""Tests for v3.2.14 — resolve nested action IDs to names, and sync
audit-events lists via the Transactional API.

Prior version's log surfaced `112000000190` when a nested import failed
inside a process — an ID with no human meaning. Now the caller can pass
an id -> name map so the log shows `Load Users` alongside the ID.

Second half covers the diff-and-add list sync path used to keep the
target model's ``EVENT_ID`` (or ``AUDIT_ID``) list current with what
this run actually observed — belt-and-suspenders for reporting models
whose nested imports don't handle new codes cleanly.
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest
import respx

from anaplan_audit.api.integration import _summarize_nested_results, run_process
from anaplan_audit.api.transactional import get_list_item_identifiers
from anaplan_audit.exceptions import UnexpectedResponseError
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"


class TestActionNameResolution:
    def test_summary_falls_back_to_provided_names(self) -> None:
        # Anaplan can omit objectName on failed nested imports (real logs
        # from the colleague's run showed this). The caller's action_names
        # map fills in the gap.
        summary = _summarize_nested_results(
            [
                {
                    "objectId": "112000000190",
                    "successful": False,
                    "failureDumpAvailable": False,
                    "details": [{"localMessageText": "Property-based import broken"}],
                },
                {
                    "objectId": "112000000191",
                    "successful": False,
                },
            ],
            action_names={"112000000190": "Load Users", "112000000191": "Load Models"},
        )
        assert summary[0]["name"] == "Load Users"
        assert summary[0]["id"] == "112000000190"
        assert summary[0]["details"] == ["Property-based import broken"]
        assert summary[1]["name"] == "Load Models"
        assert summary[1]["id"] == "112000000191"

    def test_summary_prefers_anaplan_name_over_map(self) -> None:
        # If Anaplan already provided objectName, don't overwrite it.
        summary = _summarize_nested_results(
            [{"objectId": "IMP1", "objectName": "Anaplan-provided name"}],
            action_names={"IMP1": "Fallback name"},
        )
        assert summary[0]["name"] == "Anaplan-provided name"

    def test_summary_falls_back_to_id_when_nothing_matches(self) -> None:
        summary = _summarize_nested_results(
            [{"objectId": "UNKNOWN_ID"}],
            action_names={"SOMETHING_ELSE": "Other"},
        )
        assert summary[0]["name"] == "UNKNOWN_ID"

    def test_run_process_threads_action_names_into_failure_log(self) -> None:
        # End-to-end: pass action_names to run_process, force a nested
        # failure, confirm the raised context references the resolved name.
        base = f"{BASE}/workspaces/{WS}/models/{MODEL}/processes/P1"
        with respx.mock:
            respx.post(f"{base}/tasks").mock(
                return_value=httpx.Response(200, json={"task": {"taskId": "t-1"}})
            )
            respx.get(f"{base}/tasks/t-1").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "task": {
                            "taskId": "t-1",
                            "taskState": "COMPLETE",
                            "result": {
                                "successful": False,
                                "failureDumpAvailable": False,
                                "details": [],
                                "nestedResults": [
                                    {
                                        "objectId": "112000000190",
                                        "successful": False,
                                        "failureDumpAvailable": False,
                                        "details": [{"localMessageText": "Key property unmapped"}],
                                    }
                                ],
                            },
                        }
                    },
                )
            )
            with make_client() as client, pytest.raises(UnexpectedResponseError) as excinfo:
                run_process(
                    client,
                    BASE,
                    WS,
                    MODEL,
                    "P1",
                    action_names={"112000000190": "Load Users"},
                )
        assert "Load Users" in excinfo.value.context["failed_nested"]


class TestGetListItemIdentifiers:
    def test_returns_union_of_codes_and_names(self) -> None:
        # v3.2.18: identifiers now include BOTH code and name because
        # Anaplan enforces uniqueness on both columns independently.
        # A value present in either would collide on POST.
        with respx.mock:
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"includeAll": "true"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "listItems": [
                            {"id": "1", "code": "user.loggedIn", "name": "User Log In"},
                            {"id": "2", "code": "usr-38", "name": "USR-38"},
                            {"id": "3", "code": "", "name": "just-a-name"},
                            {"id": "4", "code": "just-a-code"},
                            {"id": "5"},  # both missing → skip
                        ]
                    },
                )
            )
            with make_client() as client:
                identifiers = get_list_item_identifiers(client, BASE, WS, MODEL, "L1")
        # Both codes and both names present, empties skipped.
        assert identifiers == {
            "user.loggedIn",
            "User Log In",
            "usr-38",
            "USR-38",
            "just-a-name",
            "just-a-code",
        }

    def test_handles_items_key_variant(self) -> None:
        # Some Anaplan responses use "items" instead of "listItems".
        with respx.mock:
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"includeAll": "true"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"items": [{"id": "1", "code": "X", "name": "Y"}]},
                )
            )
            with make_client() as client:
                identifiers = get_list_item_identifiers(client, BASE, WS, MODEL, "L1")
        assert identifiers == {"X", "Y"}


class TestSyncListsTransactional:
    """Full _sync_lists_transactional orchestration."""

    def _settings(
        self,
        entries: list[dict[str, str]],
    ) -> object:
        from anaplan_audit.config import (
            AnaplanUris,
            ListSyncEntry,
            Settings,
            TargetModelConfig,
            TargetModelObjects,
        )

        return Settings(
            anaplanTenantName="test",
            authenticationMode="basic",
            basic_username="u",
            basic_password="p",
            uris=AnaplanUris(integrationUri=BASE),
            targetAnaplanModel=TargetModelConfig(
                workspaceId=WS,
                modelId=MODEL,
                objects=TargetModelObjects(
                    syncLists=[ListSyncEntry(**e) for e in entries],
                ),
            ),
        )

    def _stub_logger(self) -> object:
        import structlog

        return structlog.get_logger().bind(test=True)

    def test_returns_immediately_when_no_entries_configured(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings([])
        df = pd.DataFrame({"EVENT_ID": ["a", "b"]})
        with respx.mock, make_client() as client:
            # No HTTP routes registered — must not be called.
            _sync_lists_transactional(client, settings, df, log=self._stub_logger())

    def test_end_to_end_adds_only_net_new_codes(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings([{"listName": "EVENT_ID", "codeColumn": "EVENT_ID"}])
        df = pd.DataFrame(
            {
                # Observed codes: a, b, c (b duplicated to test .unique(),
                # empty-string filtered out).
                "EVENT_ID": ["a", "b", "b", "c", ""],
            }
        )

        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": [{"id": "L1", "name": "EVENT_ID"}]})
            )
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"includeAll": "true"},
            ).mock(
                # Existing list already has 'a'. Only 'b' and 'c' should be POSTed.
                return_value=httpx.Response(200, json={"listItems": [{"id": "1", "code": "a"}]})
            )
            add_route = respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200, json={"added": 2, "ignored": 0, "total": 2, "failures": []}
                )
            )

            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

        assert add_route.called
        sent = add_route.calls[0].request.content.decode()
        assert '"b"' in sent and '"c"' in sent
        # 'a' was already in the list — must not be POSTed.
        assert '"a"' not in sent

    def test_no_new_codes_skips_add(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings([{"listName": "EVENT_ID", "codeColumn": "EVENT_ID"}])
        df = pd.DataFrame({"EVENT_ID": ["a", "b"]})

        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": [{"id": "L1", "name": "EVENT_ID"}]})
            )
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"includeAll": "true"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "listItems": [
                            {"id": "1", "code": "a"},
                            {"id": "2", "code": "b"},
                        ]
                    },
                )
            )
            # No add-items route — must not be called.
            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

    def test_missing_column_logs_and_continues(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings([{"listName": "EVENT_ID", "codeColumn": "MISSING_COL"}])
        df = pd.DataFrame({"EVENT_ID": ["a"]})

        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": [{"id": "L1", "name": "EVENT_ID"}]})
            )
            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

    def test_missing_list_logs_and_continues(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings([{"listName": "NOT_THERE", "codeColumn": "EVENT_ID"}])
        df = pd.DataFrame({"EVENT_ID": ["a"]})

        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(
                    200, json={"lists": [{"id": "L1", "name": "SOMETHING_ELSE"}]}
                )
            )
            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

    def test_multiple_entries_processed_independently(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings(
            [
                {"listName": "EVENT_ID", "codeColumn": "EVENT_ID"},
                {"listName": "AUDIT_ID", "codeColumn": "AUDIT_ID"},
            ]
        )
        df = pd.DataFrame(
            {
                "EVENT_ID": ["user.loggedIn", "user.loggedIn"],
                "AUDIT_ID": ["7001", "7002"],
            }
        )

        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "lists": [
                            {"id": "L_EV", "name": "EVENT_ID"},
                            {"id": "L_AU", "name": "AUDIT_ID"},
                        ]
                    },
                )
            )
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L_EV/items",
                params={"includeAll": "true"},
            ).mock(return_value=httpx.Response(200, json={"listItems": []}))
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L_AU/items",
                params={"includeAll": "true"},
            ).mock(return_value=httpx.Response(200, json={"listItems": []}))
            ev_add = respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L_EV/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200, json={"added": 1, "ignored": 0, "total": 1, "failures": []}
                )
            )
            au_add = respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L_AU/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200, json={"added": 2, "ignored": 0, "total": 2, "failures": []}
                )
            )
            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

        assert ev_add.called and au_add.called
        assert '"user.loggedIn"' in ev_add.calls[0].request.content.decode()
        au_body = au_add.calls[0].request.content.decode()
        assert '"7001"' in au_body and '"7002"' in au_body
