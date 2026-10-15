"""Unit tests for the Anapedia activity-code harvester (scripts/).

The harvester is a maintenance tool, not shipped runtime code, but its HTML
parsing and merge policy are easy to get subtly wrong, so lock the behaviour
with network-free unit tests over the pure functions.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "harvest_activity_codes.py"
_spec = importlib.util.spec_from_file_location("harvest_activity_codes", _SCRIPT)
assert _spec and _spec.loader
harvest = importlib.util.module_from_spec(_spec)
sys.modules["harvest_activity_codes"] = harvest
_spec.loader.exec_module(harvest)


_DSM_ROW = (
    "<tr><td>DSM-071 displays as: DSM-DAO0071I</td>"
    "<td>Create key pair with key Note: same key</td><td>N/A</td></tr>"
)
SAMPLE_HTML = f"""
<table>
  <tr><th>Event code</th><th>Event message</th><th>Associated object ID</th></tr>
  <tr><td>USR-81</td><td>UX board page published</td><td>UX App GUID, UX Page GUID</td></tr>
  {_DSM_ROW}
</table>
"""


class TestParsePage:
    def test_extracts_code_message_assoc(self) -> None:
        got = harvest.parse_page(SAMPLE_HTML)
        assert got["USR-81"] == ("UX board page published", "UX App GUID, UX Page GUID")

    def test_dsm_dual_code_both_map_to_message(self) -> None:
        # "DSM-071 displays as: DSM-DAO0071I" yields BOTH codes, Note stripped.
        got = harvest.parse_page(SAMPLE_HTML)
        assert got["DSM-071"][0] == "Create key pair with key"
        assert got["DSM-DAO0071I"][0] == "Create key pair with key"


class TestMergePolicy:
    def _rows(self) -> list[list[str]]:
        ph = "User activity event (message pending Anaplan documentation)"
        return [
            ["USR-82", ph, "", ""],  # documented placeholder -> filled
            ["USR-13", "User access model success", "", ""],  # live value -> kept
            ["USR-60", "UX worksheet page published", "", ""],  # shift-orphan -> reset
            ["USR-59", "Resend activation email", "", ""],  # undocumented live -> kept
        ]

    def _doc(self) -> dict[str, tuple[str, str]]:
        return {
            "USR-82": ("UX worksheet page published", "app/page"),
            "USR-13": ("User access to model, success", "Model"),  # docs differ from live
            "USR-84": ("UX board My page published", "app/page"),  # new code
        }

    def test_policy(self) -> None:
        rows, rep = harvest.merge(self._rows(), self._doc())
        by = {r[0]: r[1] for r in rows}
        # documented placeholder filled from Anapedia
        assert by["USR-82"] == "UX worksheet page published"
        # live/curated value kept even though docs differ (live-authoritative)
        assert by["USR-13"] == "User access model success"
        # shift-orphan (holds USR-82's documented message) reset to placeholder
        assert harvest._is_placeholder(by["USR-60"])
        # undocumented, non-shift live value kept
        assert by["USR-59"] == "Resend activation email"
        # new documented code appended
        assert by["USR-84"] == "UX board My page published"
        assert rep["added"] == ["USR-84"]
