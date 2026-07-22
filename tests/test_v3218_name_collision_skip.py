"""Tests for v3.2.18 — skip observed values that collide with an
existing list item's ``name``, not just its ``code``.

Live tenant showed:
    failureType: 'DUPLICATE'
    failureMessageDetails: 'duplicate -- column name:name, value:USR-38'
The EVENT_ID list's saved-view import had populated items where
``name = "USR-38"`` while the ``code`` differed. Our diff was against
codes only, so USR-38 looked new; the POST then collided on the name
column and Anaplan rejected all three items.

Now the diff runs against the union of codes and names, so anything
already in the list on either column is filtered out before we POST.
"""

from __future__ import annotations

import httpx
import pandas as pd
import respx

from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "model-1"


class TestSkipsNameCollisions:
    def _settings(self, list_name: str, code_column: str) -> object:
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
                    syncLists=[ListSyncEntry(listName=list_name, codeColumn=code_column)],
                ),
            ),
        )

    def _stub_logger(self) -> object:
        import structlog

        return structlog.get_logger().bind(test=True)

    def test_skips_observed_value_matching_only_existing_name(self) -> None:
        # Reproduces the live failure: existing item has code="internal-27"
        # but name="USR-38". Observed data has EVENT_ID="USR-38". The diff
        # must NOT try to POST USR-38 — it would collide on the name.
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings("EVENT_ID", "EVENT_ID")
        df = pd.DataFrame({"EVENT_ID": ["USR-38", "USR-41", "WF-103", "NEW-1"]})

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
                            # These three would have failed the pre-v3.2.18
                            # diff (code column differs) but the NAME
                            # collides with the observed values below.
                            {"id": "1", "code": "internal-27", "name": "USR-38"},
                            {"id": "2", "code": "internal-28", "name": "USR-41"},
                            {"id": "3", "code": "internal-29", "name": "WF-103"},
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

            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())

        assert add_route.called
        body = add_route.calls[0].request.content.decode()
        # NEW-1 is the only observed value not already in the list.
        assert '"NEW-1"' in body
        # The three name-collisions must NOT be POSTed.
        assert "USR-38" not in body
        assert "USR-41" not in body
        assert "WF-103" not in body

    def test_no_post_when_every_observed_value_collides(self) -> None:
        from anaplan_audit.upload import _sync_lists_transactional

        settings = self._settings("EVENT_ID", "EVENT_ID")
        df = pd.DataFrame({"EVENT_ID": ["USR-38", "USR-41"]})

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
                            {"id": "1", "code": "x", "name": "USR-38"},
                            {"id": "2", "code": "y", "name": "USR-41"},
                        ]
                    },
                )
            )
            # No add route registered — must not be called.
            with make_client() as client:
                _sync_lists_transactional(client, settings, df, log=self._stub_logger())
