"""Tests for v3.2.13 — write the BATCH_ID + Refresh Log via the
Transactional API.

Colleague's model uses a ``BATCH_ID`` list (code = epoch seconds) and a
``Refresh Log`` module with two line items (``Time Stamp``, ``Audit
Records Loaded``). Previously the process's nested "Load Last Run"
import was expected to populate this — and it wasn't. This suite covers
the Transactional-API path that writes the two cells directly.
"""

from __future__ import annotations

import httpx
import respx

from anaplan_audit.api.transactional import (
    add_list_items,
    list_lists,
    list_module_line_items,
    list_modules,
    write_module_cells,
)
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"


class TestListLookups:
    def test_list_lists_parses_name_id_pairs(self) -> None:
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "lists": [
                            {"id": "101000000037", "name": "BATCH_ID"},
                            {"id": "101000000038", "name": "Users"},
                        ],
                    },
                )
            )
            with make_client() as client:
                lists = list_lists(client, BASE, WS, MODEL)
        assert [(li.name, li.id) for li in lists] == [
            ("BATCH_ID", "101000000037"),
            ("Users", "101000000038"),
        ]

    def test_list_modules_parses_name_id_pairs(self) -> None:
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "modules": [
                            {"id": "10200000005", "name": "Refresh Log"},
                            {"id": "10200000006", "name": "Some Other Module"},
                        ],
                    },
                )
            )
            with make_client() as client:
                modules = list_modules(client, BASE, WS, MODEL)
        assert [(m.name, m.id) for m in modules] == [
            ("Refresh Log", "10200000005"),
            ("Some Other Module", "10200000006"),
        ]

    def test_list_module_line_items_parses_items_array(self) -> None:
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/mod-1/lineItems").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": "20200000001", "name": "Time Stamp"},
                            {"id": "20200000002", "name": "Audit Records Loaded"},
                        ],
                    },
                )
            )
            with make_client() as client:
                items = list_module_line_items(client, BASE, WS, MODEL, "mod-1")
        assert [(li.name, li.id) for li in items] == [
            ("Time Stamp", "20200000001"),
            ("Audit Records Loaded", "20200000002"),
        ]


class TestAddListItems:
    def test_add_list_items_success(self) -> None:
        with respx.mock:
            route = respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"added": 1, "ignored": 0, "total": 1, "failures": []},
                )
            )
            with make_client() as client:
                result = add_list_items(client, BASE, WS, MODEL, "L1", [{"code": "1720543095"}])
        assert route.called
        assert result["added"] == 1
        # The item body must be under the "items" key.
        sent = route.calls[0].request.content.decode()
        assert '"items"' in sent and '"1720543095"' in sent

    def test_add_list_items_failure_raises(self) -> None:
        import pytest

        from anaplan_audit.exceptions import UnexpectedResponseError

        with respx.mock:
            respx.post(
                f"{BASE}/workspaces/{WS}/models/{MODEL}/lists/L1/items",
                params={"action": "add"},
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "added": 0,
                        "ignored": 0,
                        "total": 1,
                        "failures": [{"index": 0, "reason": "Invalid code"}],
                    },
                )
            )
            with (
                make_client() as client,
                pytest.raises(UnexpectedResponseError),
            ):
                add_list_items(client, BASE, WS, MODEL, "L1", [{"code": ""}])


class TestWriteModuleCells:
    def test_write_module_cells_success_posts_payload(self) -> None:
        with respx.mock:
            route = respx.post(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/M1/data").mock(
                return_value=httpx.Response(200, json={"failures": []})
            )
            cells = [
                {
                    "lineItemId": "LI1",
                    "dimensions": [{"dimensionId": "L1", "itemCode": "1720543095"}],
                    "value": "2024-07-09T15:38:15Z",
                },
                {
                    "lineItemId": "LI2",
                    "dimensions": [{"dimensionId": "L1", "itemCode": "1720543095"}],
                    "value": 380,
                },
            ]
            with make_client() as client:
                result = write_module_cells(client, BASE, WS, MODEL, "M1", cells)
        assert route.called
        assert result["failures"] == []
        sent = route.calls[0].request.content.decode()
        assert '"lineItemId"' in sent
        assert '"1720543095"' in sent
        assert "380" in sent

    def test_write_module_cells_failure_raises(self) -> None:
        import pytest

        from anaplan_audit.exceptions import UnexpectedResponseError

        with respx.mock:
            respx.post(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/M1/data").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "failures": [
                            {
                                "index": 1,
                                "reason": "Line item not writable",
                            }
                        ]
                    },
                )
            )
            with (
                make_client() as client,
                pytest.raises(UnexpectedResponseError),
            ):
                write_module_cells(
                    client,
                    BASE,
                    WS,
                    MODEL,
                    "M1",
                    [{"lineItemId": "LI1", "dimensions": [], "value": 1}],
                )


class TestRefreshLogOrchestration:
    """The full upload._write_refresh_log_transactional flow."""

    def _settings(
        self,
        *,
        list_name: str = "BATCH_ID",
        module_name: str = "Refresh Log",
        timestamp_li: str = "Time Stamp",
        records_li: str = "Audit Records Loaded",
    ) -> object:
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
                    batchIdListName=list_name,
                    refreshLogModuleName=module_name,
                    refreshLogTimeStampLineItem=timestamp_li,
                    refreshLogRecordsLoadedLineItem=records_li,
                ),
            ),
        )

    def test_skips_silently_when_names_blank(self) -> None:
        from anaplan_audit.upload import _write_refresh_log_transactional
        from tests.conftest import make_client

        settings = self._settings(list_name="", module_name="")
        with (
            respx.mock,
            make_client() as client,
        ):
            _write_refresh_log_transactional(
                client,
                settings,
                last_run_epoch=1720543095,
                row_count=42,
                log=_stub_logger(),
            )
        # Nothing to assert other than: no HTTP calls were made.

    def test_end_to_end_writes_batch_and_two_cells(self) -> None:
        from anaplan_audit.upload import _write_refresh_log_transactional
        from tests.conftest import make_client

        settings = self._settings()
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
            cells_route = respx.post(f"{BASE}/workspaces/{WS}/models/{MODEL}/modules/M1/data").mock(
                return_value=httpx.Response(200, json={"failures": []})
            )

            with make_client() as client:
                _write_refresh_log_transactional(
                    client,
                    settings,
                    last_run_epoch=1720543095,
                    row_count=380,
                    log=_stub_logger(),
                )

        assert add_route.called
        assert cells_route.called
        # Batch code must equal the epoch as a string.
        assert '"1720543095"' in add_route.calls[0].request.content.decode()
        # Both line-item IDs and the 380 count must be in the cells payload.
        cells_body = cells_route.calls[0].request.content.decode()
        assert '"LI1"' in cells_body and '"LI2"' in cells_body
        assert "380" in cells_body

    def test_missing_list_logs_and_returns(self) -> None:
        from anaplan_audit.upload import _write_refresh_log_transactional
        from tests.conftest import make_client

        settings = self._settings()
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/lists").mock(
                return_value=httpx.Response(200, json={"lists": []})
            )
            # No cell / add-item routes registered — should not be hit.
            with make_client() as client:
                _write_refresh_log_transactional(
                    client,
                    settings,
                    last_run_epoch=1720543095,
                    row_count=380,
                    log=_stub_logger(),
                )

    def test_missing_line_item_logs_and_returns(self) -> None:
        from anaplan_audit.upload import _write_refresh_log_transactional
        from tests.conftest import make_client

        settings = self._settings()
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
                    # Missing the two line items we care about.
                    json={"items": [{"id": "LI9", "name": "Some Other LI"}]},
                )
            )
            with make_client() as client:
                _write_refresh_log_transactional(
                    client,
                    settings,
                    last_run_epoch=1720543095,
                    row_count=380,
                    log=_stub_logger(),
                )


def _stub_logger() -> object:
    """A structlog-like logger stub with the two methods we call."""
    import structlog

    return structlog.get_logger().bind(test=True)
