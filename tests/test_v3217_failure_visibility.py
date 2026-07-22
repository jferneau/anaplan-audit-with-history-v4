"""Tests for v3.2.17 — surface the reason Anaplan rejected list-item
and cell-write requests, and always send both ``code`` and ``name``.

Live tenant showed the transactional-API path failing with a bland
``add-list-items failed for N of N items`` message that hid Anaplan's
actual complaint. The exception message now leads with the first
failure reason so ``str(exc)`` in the caller's warning log tells the
operator what to fix. In parallel, list-item payloads now always
include both ``code`` and ``name`` — some Anaplan list flavors reject
code-only bodies.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from anaplan_audit.api.transactional import (
    _extract_failure_reasons,
    add_list_items,
    write_module_cells,
)
from anaplan_audit.exceptions import UnexpectedResponseError
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"


class TestExtractFailureReasons:
    def test_reason_key(self) -> None:
        assert _extract_failure_reasons([{"index": 0, "reason": "Duplicate key"}]) == [
            "Duplicate key"
        ]

    def test_failure_reason_key(self) -> None:
        assert _extract_failure_reasons(
            [{"index": 0, "failureReason": "Property not writable"}]
        ) == ["Property not writable"]

    def test_failure_message_details_key(self) -> None:
        assert _extract_failure_reasons([{"failureMessageDetails": "Line item not found"}]) == [
            "Line item not found"
        ]

    def test_nested_status_message(self) -> None:
        assert _extract_failure_reasons(
            [{"status": {"code": 400, "message": "Bad code format"}}]
        ) == ["Bad code format"]

    def test_skips_non_dict_and_empty_reasons(self) -> None:
        assert _extract_failure_reasons(["not a dict", {}, {"reason": ""}]) == []

    def test_returns_multiple_reasons_in_order(self) -> None:
        assert _extract_failure_reasons(
            [
                {"reason": "First"},
                {"reason": "Second"},
                {"reason": "Third"},
            ]
        ) == ["First", "Second", "Third"]


class TestAddListItemsFailureVisibility:
    def test_first_failure_reason_appears_in_exception_message(self) -> None:
        with respx.mock:
            respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "failures": [
                            {
                                "index": 0,
                                "reason": "Codes cannot be provided for numbered lists",
                            }
                        ]
                    },
                )
            )
            with (
                make_client() as client,
                pytest.raises(UnexpectedResponseError) as excinfo,
            ):
                add_list_items(client, BASE, WS, MODEL, "L1", [{"code": "1720543095"}])
        # str(exc) is what the callers pass to log.warning — so the
        # reason must be there, not just the count.
        assert "Codes cannot be provided for numbered lists" in str(excinfo.value)
        assert "1 of 1" in str(excinfo.value)

    def test_context_carries_bounded_failure_slice(self) -> None:
        # 10 failures — context should only carry the first 5 so log
        # payloads stay scannable.
        with respx.mock:
            respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"failures": [{"index": i, "reason": f"err {i}"} for i in range(10)]},
                )
            )
            with (
                make_client() as client,
                pytest.raises(UnexpectedResponseError) as excinfo,
            ):
                add_list_items(
                    client,
                    BASE,
                    WS,
                    MODEL,
                    "L1",
                    [{"code": str(i)} for i in range(10)],
                )
        assert len(excinfo.value.context["failures"]) == 5


class TestWriteModuleCellsFailureVisibility:
    def test_first_failure_reason_appears_in_exception_message(self) -> None:
        with respx.mock:
            respx.post(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/M1/data").mock(
                return_value=httpx.Response(
                    200,
                    json={"failures": [{"index": 0, "reason": "Line item is not writable"}]},
                )
            )
            with (
                make_client() as client,
                pytest.raises(UnexpectedResponseError) as excinfo,
            ):
                write_module_cells(
                    client,
                    BASE,
                    WS,
                    MODEL,
                    "M1",
                    [{"lineItemId": "LI1", "dimensions": [], "value": 1}],
                )
        assert "Line item is not writable" in str(excinfo.value)


class TestItemPayloadIncludesName:
    """Refresh log + list sync now send both code and name in the body."""

    def _settings_refresh(self) -> object:
        from anaplan_audit.config import (
            AnaplanUris,
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
                    batchIdListName="BATCH_ID",
                    refreshLogModuleName="Refresh Log",
                ),
            ),
        )

    def _stub_logger(self) -> object:
        import structlog

        return structlog.get_logger().bind(test=True)

    def test_refresh_log_batch_item_includes_name(self) -> None:
        from anaplan_audit.upload import _write_refresh_log_transactional

        settings = self._settings_refresh()
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": [{"id": "L1", "name": "BATCH_ID"}]})
            )
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules").mock(
                return_value=httpx.Response(
                    200, json={"modules": [{"id": "M1", "name": "Refresh Log"}]}
                )
            )
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/M1/lineItems").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": "LI1", "name": "Time Stamp"},
                            {"id": "LI2", "name": "Audit Records Loaded"},
                        ]
                    },
                )
            )
            add_route = respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200, json={"added": 1, "ignored": 0, "total": 1, "failures": []}
                )
            )
            respx.post(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/M1/data").mock(
                return_value=httpx.Response(200, json={"failures": []})
            )

            with make_client() as client:
                _write_refresh_log_transactional(
                    client,
                    settings,
                    last_run_epoch=1720543095,
                    row_count=42,
                    log=self._stub_logger(),
                )

        body = add_route.calls[0].request.content.decode()
        assert '"code"' in body and '"name"' in body
        assert body.count("1720543095") >= 2  # once for code, once for name

    def test_list_sync_items_include_name(self) -> None:
        import pandas as pd

        from anaplan_audit.config import (
            AnaplanUris,
            ListSyncEntry,
            Settings,
            TargetModelConfig,
            TargetModelObjects,
        )
        from anaplan_audit.upload import _sync_lists_transactional

        settings = Settings(
            anaplanTenantName="test",
            authenticationMode="basic",
            basic_username="u",
            basic_password="p",
            uris=AnaplanUris(integrationUri=BASE),
            targetAnaplanModel=TargetModelConfig(
                workspaceId=WS,
                modelId=MODEL,
                objects=TargetModelObjects(
                    syncLists=[ListSyncEntry(listName="EVENT_ID", codeColumn="EVENT_ID")],
                ),
            ),
        )
        df = pd.DataFrame({"EVENT_ID": ["user.loggedIn"]})

        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": [{"id": "L1", "name": "EVENT_ID"}]})
            )
            respx.get(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"includeAll": "true"},
            ).mock(return_value=httpx.Response(200, json={"listItems": []}))
            add_route = respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200, json={"added": 1, "ignored": 0, "total": 1, "failures": []}
                )
            )
            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

        body = add_route.calls[0].request.content.decode()
        assert '"code"' in body and '"name"' in body
        # The observed code appears twice: once for code, once for name.
        assert body.count("user.loggedIn") >= 2
