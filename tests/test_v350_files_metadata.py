"""v3.5.0 regression tests — SYS Files metadata fetch.

v3 was uploading ``FILE_LIST.csv`` as an empty file every run because
``_fetch_metadata`` never called ``list_files`` on the source models.
Reporting model's ``SYS Files`` module and ``Import into FILE_CT``
action both silently ignored the empty file.

Now files are fetched per-selected-model alongside actions/processes,
attach ``workspaceId`` + ``modelId``, and land as the ``files``
metadata dataset routed through the standard CSV upload loop.
"""

from __future__ import annotations

import httpx
import respx

from anaplan_audit.api.integration import list_files
from anaplan_audit.api.models import ImportDataSource
from anaplan_audit.upload import _TABLE_TO_COUNTER_COLUMN, _TABLE_TO_FILE_ATTR
from tests.conftest import make_client

BASE = "https://api.test.com/2/0"
WS = "ws-1"
MODEL = "mod-1"


class TestFilesDatasetWiring:
    def test_files_is_in_the_upload_loop(self) -> None:
        # The tuple ordering matches the reporting model's expected
        # process ordering: WS -> USR -> MOD -> ACT -> FILE -> CW ->
        # act_codes.
        table_names = [t for t, _ in _TABLE_TO_FILE_ATTR]
        assert table_names == [
            "workspaces",
            "users",
            "models",
            "actions",
            "files",
            "cloudworks",
            "act_codes",
        ]

    def test_files_gets_file_ct_counter_column(self) -> None:
        # The reporting model's Import into FILE_CT is property-based
        # keyed on FILE_CT; without the counter, the import fails the
        # same way WS_CT did back in v3.2.15.
        assert _TABLE_TO_COUNTER_COLUMN.get("files") == "FILE_CT"


class TestListFilesClient:
    def test_returns_import_data_sources(self) -> None:
        with respx.mock:
            respx.get(f"{BASE}/workspaces/{WS}/models/{MODEL}/files").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "files": [
                            {"id": "file-1", "name": "AUDIT_LOG.csv"},
                            {"id": "file-2", "name": "MODEL_LIST.csv"},
                        ]
                    },
                )
            )
            with make_client() as client:
                files = list_files(client, BASE, WS, MODEL)
        assert [(f.id, f.name) for f in files] == [
            ("file-1", "AUDIT_LOG.csv"),
            ("file-2", "MODEL_LIST.csv"),
        ]

    def test_import_data_source_declares_id_and_name(self) -> None:
        # SYS Files expects Name / Id / Workspace ID / Model ID.
        # Pydantic declares id+name; workspaceId+modelId are attached
        # in orchestrator._fetch_metadata via extra keys.
        f = ImportDataSource(id="x", name="y")
        assert f.id == "x"
        assert f.name == "y"
