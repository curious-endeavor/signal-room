"""Live GDELT integration tests.

These tests hit the real GDELT API and require:
  - a working bin/gdelt-pp-cli or ~/printing-press/library/gdelt/gdelt-pp-cli
  - the `chatbot-failures` pillar present in the user's pillars file
    (run scripts/bootstrap_gdelt_pillars_from_alice.py once if absent)
  - network access

Skipped by default. To run:

    GDELT_LIVE=1 python3 -m unittest discover tests
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from signal_room.fetchers.gdelt import fetch_gdelt, _resolve_binary


@unittest.skipUnless(os.environ.get("GDELT_LIVE") == "1", "set GDELT_LIVE=1 to run live tests")
class GdeltLiveTest(unittest.TestCase):
    def test_chatbot_failures_returns_items_with_metadata(self):
        payload = fetch_gdelt(
            pillars=["chatbot-failures"],
            timespan="7d",
            max_records=5,
            output_path=None,
        )
        self.assertEqual(payload["errors"], [], f"pillar errors: {payload['errors']}")
        self.assertGreaterEqual(
            payload["item_count"], 1,
            f"chatbot-failures returned 0 items — likely a GDELT data-availability issue, "
            f"not a code regression. Compiled query: {payload['runs'][0].get('query')}",
        )
        for item in payload["items"]:
            self.assertEqual(item["meta"]["source"], "gdelt")
            self.assertIsNotNone(item["metadata"].get("language"))
            self.assertIsNotNone(item["metadata"].get("sourcecountry"))

    def test_short_acronym_pillar_does_not_raise_on_empty(self):
        # Create a temp short-acronym pillar to exercise origin §6 gotcha 2:
        # GDELT may silently return zero results — that must surface as an
        # empty item list, not a raised exception.
        binary = _resolve_binary()
        previous = os.environ.get("GDELT_PILLARS_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            pillars_path = str(Path(tmp) / "pillars.json")
            os.environ["GDELT_PILLARS_PATH"] = pillars_path
            try:
                subprocess.run(
                    [binary, "pillar", "add", "ab-1988-test", '"AB 1988"'],
                    env=dict(os.environ), check=True, capture_output=True, text=True,
                )
                payload = fetch_gdelt(
                    pillars=["ab-1988-test"],
                    timespan="1d",
                    max_records=5,
                    output_path=None,
                    continue_on_error=True,
                )
            finally:
                if previous is None:
                    os.environ.pop("GDELT_PILLARS_PATH", None)
                else:
                    os.environ["GDELT_PILLARS_PATH"] = previous
        self.assertEqual(payload["errors"], [], f"unexpected errors: {payload['errors']}")
        # item_count may be 0 — that's fine; the test only asserts no raise.


if __name__ == "__main__":
    unittest.main()
