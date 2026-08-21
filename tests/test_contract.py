from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from orchestra_surgical_display.client import ALLOWED_PAYLOAD_FIELDS, PUBLIC_STATES


ROOT = Path(__file__).resolve().parents[1]


class PublicContractTest(unittest.TestCase):
    def test_state_catalog_sdk_and_openapi_are_synchronized(self) -> None:
        catalog = json.loads((ROOT / "contract/states.json").read_text())
        openapi = (ROOT / "contract/openapi.yaml").read_text()

        state_block = openapi.split("    SurgicalState:", 1)[1].split(
            "    EventType:", 1
        )[0]
        openapi_states = set(
            re.findall(r"^        - ([A-Z][A-Z0-9_]*)$", state_block, re.MULTILINE)
        )
        payload_block = openapi.split("    SurgicalPayload:", 1)[1].split(
            "    SurgicalEventRequest:", 1
        )[0]
        openapi_payload_fields = set(
            re.findall(r"^        ([a-z][a-z0-9_]*):$", payload_block, re.MULTILINE)
        )

        self.assertEqual(set(catalog["transport_states"]), set(PUBLIC_STATES))
        self.assertEqual(set(catalog["transport_states"]), openapi_states)
        self.assertEqual(set(catalog["payload_fields"]), set(ALLOWED_PAYLOAD_FIELDS))
        self.assertEqual(set(catalog["payload_fields"]), openapi_payload_fields)

    def test_public_documents_do_not_contain_local_machine_paths(self) -> None:
        for relative in (
            "README.md",
            "docs/INTEGRATION_GUIDE_KO.md",
            "docs/API_CONTRACT_KO.md",
            "contract/states.json",
            "contract/openapi.yaml",
        ):
            text = (ROOT / relative).read_text()
            self.assertNotIn("/Users/", text, relative)


if __name__ == "__main__":
    unittest.main()
