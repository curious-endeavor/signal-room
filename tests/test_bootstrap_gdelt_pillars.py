import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bootstrap_gdelt_pillars_from_alice as bootstrap  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class ParseBriefTest(unittest.TestCase):
    @unittest.skipUnless(
        (REPO_ROOT / "config" / "brands" / "alice" / "brief.yaml").exists(),
        "alice brief.yaml not present (ships on a sibling branch)",
    )
    def test_parses_real_brief(self):
        ids = bootstrap._parse_brief_pillar_ids(REPO_ROOT / "config" / "brands" / "alice" / "brief.yaml")
        for expected in ("P1", "P2", "P3", "P3b", "P4", "P5"):
            self.assertIn(expected, ids)

    def test_missing_file_raises(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap._parse_brief_pillar_ids(Path("/no/such/brief.yaml"))


class BootstrapDispatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.brief = self.tmp / "brief.yaml"
        # Minimal brief with all expected pillar IDs.
        self.brief.write_text(
            "pillars:\n"
            "  - id: P1\n"
            "  - id: P2\n"
            "  - id: P3\n"
            "  - id: P3b\n"
            "  - id: P4\n"
            "  - id: P5\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_all_pillars_from_empty_state(self):
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if cmd[1] == "pillar" and cmd[2] == "list":
                return _completed(stdout=json.dumps({"pillars": []}))
            if cmd[1] == "pillar" and cmd[2] == "add":
                return _completed()
            return _completed(returncode=2, stderr=f"unexpected: {cmd}")

        with patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            summary = bootstrap.bootstrap(
                brief_path=self.brief,
                binary="/fake/gdelt",
                pillars_path=None,
                dry_run=False,
            )
        self.assertEqual(summary["added"], 10)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["skipped"], 0)
        # One list + 10 adds
        list_calls = [c for c in calls if c[2] == "list"]
        add_calls = [c for c in calls if c[2] == "add"]
        self.assertEqual(len(list_calls), 1)
        self.assertEqual(len(add_calls), 10)

    def test_idempotent_when_pillars_match(self):
        existing = [
            {"name": name, "query": query} for name, query in bootstrap.ALICE_PILLARS.items()
        ]

        def fake_run(cmd, **_kwargs):
            if cmd[2] == "list":
                return _completed(stdout=json.dumps({"pillars": existing}))
            return _completed(returncode=2, stderr=f"should not run: {cmd}")

        with patch.object(bootstrap.subprocess, "run", side_effect=fake_run) as m:
            summary = bootstrap.bootstrap(
                brief_path=self.brief,
                binary="/fake/gdelt",
                pillars_path=None,
                dry_run=False,
            )
        self.assertEqual(summary["skipped"], 10)
        self.assertEqual(summary["added"], 0)
        # Only the list call should have run.
        self.assertEqual(m.call_count, 1)

    def test_updates_pillar_when_query_changed(self):
        existing = [{"name": "chatbot-failures", "query": "OLD QUERY"}]
        # Other pillars remain unchanged so they don't pollute the test.

        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if cmd[2] == "list":
                return _completed(stdout=json.dumps({"pillars": existing}))
            return _completed()

        with patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            summary = bootstrap.bootstrap(
                brief_path=self.brief,
                binary="/fake/gdelt",
                pillars_path=None,
                dry_run=False,
            )
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["added"], 9)
        # Updated pillar got rm + add
        rms = [c for c in calls if c[2] == "rm"]
        self.assertEqual(len(rms), 1)
        self.assertEqual(rms[0][3], "chatbot-failures")

    def test_dry_run_does_not_invoke_add_or_rm(self):
        def fake_run(cmd, **_kwargs):
            if cmd[2] == "list":
                return _completed(stdout=json.dumps({"pillars": []}))
            raise AssertionError(f"dry-run should not invoke subprocess: {cmd}")

        with patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            summary = bootstrap.bootstrap(
                brief_path=self.brief,
                binary="/fake/gdelt",
                pillars_path=None,
                dry_run=True,
            )
        self.assertEqual(summary["added"], 10)

    def test_missing_expected_brief_pillar_id_raises(self):
        # Strip P4 — short-acronym statutes — from the brief.
        self.brief.write_text(
            "pillars:\n  - id: P1\n  - id: P2\n  - id: P3\n  - id: P3b\n  - id: P5\n",
            encoding="utf-8",
        )

        def fake_run(cmd, **_kwargs):
            return _completed(stdout=json.dumps({"pillars": []}))

        with patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(bootstrap.BootstrapError) as ctx:
                bootstrap.bootstrap(
                    brief_path=self.brief,
                    binary="/fake/gdelt",
                    pillars_path=None,
                    dry_run=False,
                )
        self.assertIn("P4", str(ctx.exception))

    def test_short_acronym_pillars_added_as_separate_entries(self):
        # Regression guard for origin §6 gotcha 2.
        p4_names = {
            "ai-reg-eu-act", "ai-reg-ab-1988", "ai-reg-iso-42001",
            "ai-reg-nist-rmf", "ai-reg-mitre",
        }
        self.assertTrue(p4_names.issubset(set(bootstrap.ALICE_PILLARS)))
        for name in p4_names:
            query = bootstrap.ALICE_PILLARS[name]
            # No OR'd phrases — each P4 query is a single quoted statute name.
            self.assertNotIn(" OR ", query, f"{name} should not OR multiple statutes")


if __name__ == "__main__":
    unittest.main()
