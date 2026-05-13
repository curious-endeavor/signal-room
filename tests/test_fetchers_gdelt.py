import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from signal_room.fetchers import gdelt


FIXTURE = gdelt.FIXTURE_PATH


# ---------------------------------------------------------------------------
# U1 — config / binary resolution
# ---------------------------------------------------------------------------


class ResolveBinaryTest(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"GDELT_PP_CLI": __file__}, clear=False):
            self.assertEqual(gdelt._resolve_binary(), __file__)

    def test_env_override_missing_raises(self):
        with patch.dict(os.environ, {"GDELT_PP_CLI": "/no/such/binary"}, clear=False):
            with self.assertRaises(gdelt.GdeltError):
                gdelt._resolve_binary()

    def test_config_binary_path_used_when_set(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GDELT_PP_CLI", None)
            with patch.object(gdelt, "_load_backend_config", return_value={"binary_path": __file__}):
                self.assertEqual(gdelt._resolve_binary(), __file__)

    def test_config_binary_path_missing_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GDELT_PP_CLI", None)
            with patch.object(gdelt, "_load_backend_config", return_value={"binary_path": "/no/such/file"}):
                with self.assertRaises(gdelt.GdeltError):
                    gdelt._resolve_binary()

    def test_falls_back_to_local_dev_when_repo_built_missing(self):
        if not gdelt._LOCAL_DEV_BINARY.exists():
            self.skipTest("local-dev gdelt-pp-cli not present")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GDELT_PP_CLI", None)
            with patch.object(gdelt, "_load_backend_config", return_value={}):
                with patch.object(gdelt, "_REPO_BUILT_BINARY", Path("/no/such/bin")):
                    self.assertEqual(gdelt._resolve_binary(), str(gdelt._LOCAL_DEV_BINARY))

    def test_no_candidate_raises_with_clear_message(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GDELT_PP_CLI", None)
            with patch.object(gdelt, "_load_backend_config", return_value={}):
                with patch.object(gdelt, "_REPO_BUILT_BINARY", Path("/no/such/bin")):
                    with patch.object(gdelt, "_LOCAL_DEV_BINARY", Path("/no/such/local")):
                        with self.assertRaises(gdelt.GdeltError) as ctx:
                            gdelt._resolve_binary()
        self.assertIn("gdelt-pp-cli", str(ctx.exception))


class LoadBackendConfigTest(unittest.TestCase):
    def test_returns_empty_dict_when_file_missing(self):
        with patch.object(gdelt, "BACKEND_CONFIG_PATH", Path("/no/such/config.json")):
            self.assertEqual(gdelt._load_backend_config(), {})

    def test_parses_real_config(self):
        config = gdelt._load_backend_config()
        self.assertEqual(config.get("default_timespan"), "1d")
        self.assertEqual(config.get("default_max"), 75)
        self.assertEqual(config.get("timeout_seconds"), 60)

    def test_timeout_floor_at_10(self):
        with patch.object(gdelt, "_load_backend_config", return_value={"timeout_seconds": 3}):
            self.assertEqual(gdelt._resolve_timeout(), 10)

    def test_timeout_invalid_falls_back_to_default(self):
        with patch.object(gdelt, "_load_backend_config", return_value={"timeout_seconds": "abc"}):
            self.assertEqual(gdelt._resolve_timeout(), 60)


# ---------------------------------------------------------------------------
# U2 — pure helpers
# ---------------------------------------------------------------------------


class FilterRateLimitNoiseTest(unittest.TestCase):
    def test_drops_rate_limit_lines(self):
        stderr = (
            "rate limited, waiting 5s (attempt 1/3, rate adjusted to 0.18 req/s)\n"
            "real error: something went wrong\n"
        )
        self.assertEqual(gdelt._filter_rate_limit_noise(stderr), "real error: something went wrong")

    def test_returns_empty_when_only_noise(self):
        stderr = "rate limited, waiting 2s\nrate limited, waiting 10s (attempt 2/3)\n"
        self.assertEqual(gdelt._filter_rate_limit_noise(stderr), "")

    def test_passthrough_when_no_noise(self):
        self.assertEqual(gdelt._filter_rate_limit_noise("boom"), "boom")

    def test_empty_stderr(self):
        self.assertEqual(gdelt._filter_rate_limit_noise(""), "")


class ParseSeendateTest(unittest.TestCase):
    def test_well_formed(self):
        self.assertEqual(gdelt._parse_seendate("20260513T120000Z"), "2026-05-13")

    def test_malformed_returns_today(self):
        from datetime import date as _date
        self.assertEqual(gdelt._parse_seendate("garbage"), _date.today().isoformat())

    def test_empty_returns_today(self):
        from datetime import date as _date
        self.assertEqual(gdelt._parse_seendate(""), _date.today().isoformat())


class NormalizeArticleTest(unittest.TestCase):
    def test_fixture_round_trip(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        articles = payload["results"]["articles"]
        self.assertGreater(len(articles), 0)
        for article in articles:
            row = gdelt._normalize_article(article, "chatbot-failures")
            self.assertIsNotNone(row)
            self.assertTrue(row["id"].startswith("gdelt-"))
            self.assertEqual(row["source_url"], article["url"])
            self.assertEqual(row["meta"], {"source": "gdelt"})
            self.assertEqual(row["discovery_method"], "gdelt")
            self.assertIn("pillar:chatbot-failures", row["tags"])
            self.assertIn("platform:news", row["tags"])
            self.assertEqual(row["metadata"]["language"], article.get("language"))
            self.assertEqual(row["metadata"]["sourcecountry"], article.get("sourcecountry"))
            self.assertTrue(row["first_seen_at"].endswith("+00:00"))

    def test_drops_articles_without_url(self):
        self.assertIsNone(gdelt._normalize_article({"title": "no url"}, "p"))
        self.assertIsNone(gdelt._normalize_article({"url": ""}, "p"))
        self.assertIsNone(gdelt._normalize_article("not a dict", "p"))

    def test_stable_id_same_for_same_url(self):
        a = gdelt._normalize_article({"url": "https://x.example/a"}, "p")
        b = gdelt._normalize_article({"url": "https://x.example/a"}, "p")
        self.assertEqual(a["id"], b["id"])

    def test_title_falls_back_to_url(self):
        row = gdelt._normalize_article({"url": "https://x.example/a"}, "p")
        self.assertEqual(row["title"], "https://x.example/a")


# ---------------------------------------------------------------------------
# U2 — fetch_gdelt() integration (via mocked subprocess)
# ---------------------------------------------------------------------------


def _make_completed_process(returncode=0, stdout="", stderr=""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class FetchGdeltTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.run_root = self.tmp / "runs"

    def tearDown(self):
        self._tmp.cleanup()

    def test_mock_reads_fixture_without_subprocess(self):
        with patch.object(gdelt.subprocess, "run", side_effect=AssertionError("must not call subprocess in mock mode")):
            payload = gdelt.fetch_gdelt(
                pillars=["chatbot-failures"],
                mock=True,
                run_root=self.run_root,
                output_path=None,
            )
        self.assertEqual(payload["backend"], "gdelt")
        self.assertEqual(payload["pillar_count"], 1)
        self.assertGreater(payload["item_count"], 0)
        for item in payload["items"]:
            self.assertEqual(item["meta"]["source"], "gdelt")

    def test_real_subprocess_path_with_fixture_stdout(self):
        fixture_text = FIXTURE.read_text(encoding="utf-8")

        def fake_run(cmd, **_kwargs):
            return _make_completed_process(
                returncode=0,
                stdout=fixture_text,
                stderr="rate limited, waiting 5s (attempt 1/3)\n",
            )

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                payload = gdelt.fetch_gdelt(
                    pillars=["chatbot-failures"],
                    mock=False,
                    run_root=self.run_root,
                    output_path=None,
                )
        self.assertGreater(payload["item_count"], 0)
        # manifest written
        manifest = json.loads((self.run_root / "chatbot-failures" / "manifest.json").read_text())
        self.assertEqual(manifest["exit_code"], 0)
        self.assertGreater(manifest["item_count"], 0)

    def test_empty_articles_does_not_raise(self):
        empty_payload = json.dumps({"meta": {"source": "live"}, "results": {"articles": [], "query": "foo"}})

        def fake_run(cmd, **_kwargs):
            return _make_completed_process(returncode=0, stdout=empty_payload, stderr="")

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                payload = gdelt.fetch_gdelt(
                    pillars=["bogus-pillar"],
                    mock=False,
                    run_root=self.run_root,
                    output_path=None,
                )
        self.assertEqual(payload["item_count"], 0)
        self.assertEqual(payload["errors"], [])

    def test_exit_code_3_with_continue_on_error_skips_pillar(self):
        def fake_run(cmd, **_kwargs):
            return _make_completed_process(returncode=3, stderr="pillar not found\n")

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                payload = gdelt.fetch_gdelt(
                    pillars=["missing"],
                    mock=False,
                    run_root=self.run_root,
                    output_path=None,
                    continue_on_error=True,
                )
        self.assertEqual(payload["item_count"], 0)
        self.assertEqual(len(payload["errors"]), 1)
        self.assertEqual(payload["errors"][0]["pillar"], "missing")

    def test_exit_code_3_without_continue_on_error_raises(self):
        def fake_run(cmd, **_kwargs):
            return _make_completed_process(returncode=3, stderr="pillar not found\n")

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                with self.assertRaises(gdelt.GdeltError):
                    gdelt.fetch_gdelt(
                        pillars=["missing"],
                        mock=False,
                        run_root=self.run_root,
                        output_path=None,
                        continue_on_error=False,
                    )

    def test_timeout_records_manifest_and_raises(self):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                with self.assertRaises(gdelt.GdeltError) as ctx:
                    gdelt.fetch_gdelt(
                        pillars=["slow"],
                        mock=False,
                        run_root=self.run_root,
                        output_path=None,
                        continue_on_error=False,
                    )
        self.assertIn("timed out", str(ctx.exception))
        manifest = json.loads((self.run_root / "slow" / "manifest.json").read_text())
        self.assertIn("timed out", manifest["error"])

    def test_multiple_pillars_with_one_failure_continues(self):
        ok_payload = FIXTURE.read_text(encoding="utf-8")

        def fake_run(cmd, **_kwargs):
            # cmd is [binary, "pillar", "pull", <name>, ...]
            name = cmd[3]
            if name == "broken":
                return _make_completed_process(returncode=4, stderr="network error\n")
            return _make_completed_process(returncode=0, stdout=ok_payload)

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                payload = gdelt.fetch_gdelt(
                    pillars=["broken", "chatbot-failures"],
                    mock=False,
                    run_root=self.run_root,
                    output_path=None,
                    continue_on_error=True,
                )
        self.assertEqual(len(payload["errors"]), 1)
        self.assertEqual(payload["errors"][0]["pillar"], "broken")
        self.assertGreater(payload["item_count"], 0)

    def test_output_path_persists_payload(self):
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        out = self.tmp / "out.json"

        def fake_run(cmd, **_kwargs):
            return _make_completed_process(returncode=0, stdout=fixture_text)

        with patch.object(gdelt.subprocess, "run", side_effect=fake_run):
            with patch.object(gdelt, "_resolve_binary", return_value="/fake/gdelt-pp-cli"):
                gdelt.fetch_gdelt(
                    pillars=["chatbot-failures"],
                    mock=False,
                    run_root=self.run_root,
                    output_path=out,
                )
        on_disk = json.loads(out.read_text())
        self.assertEqual(on_disk["backend"], "gdelt")
        self.assertGreater(on_disk["item_count"], 0)


# ---------------------------------------------------------------------------
# U3 — CLI wiring (argparse + dispatch)
# ---------------------------------------------------------------------------


class CliWiringTest(unittest.TestCase):
    def test_fetch_gdelt_dispatches_to_fetch_gdelt(self):
        from signal_room import cli

        with patch.object(cli, "fetch_gdelt", return_value={"item_count": 0, "items": []}) as m:
            with patch.object(cli, "_emit"):
                rc = cli.main(["fetch", "--backend", "gdelt", "--pillars", "chatbot-failures", "--timespan", "1d", "--max", "3"])
        self.assertEqual(rc, 0)
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["pillars"], ["chatbot-failures"])
        self.assertEqual(kwargs["timespan"], "1d")
        self.assertEqual(kwargs["max_records"], 3)
        self.assertFalse(kwargs["mock"])

    def test_fetch_gdelt_default_all_pillars_passes_none(self):
        from signal_room import cli

        with patch.object(cli, "fetch_gdelt", return_value={"item_count": 0, "items": []}) as m:
            with patch.object(cli, "_emit"):
                cli.main(["fetch", "--backend", "gdelt"])
        self.assertIsNone(m.call_args.kwargs["pillars"])

    def test_fetch_both_calls_both_and_merges(self):
        from signal_room import cli

        with patch.object(cli, "fetch_last30days", return_value={"items": [], "item_count": 0}) as l30:
            with patch.object(cli, "fetch_gdelt", return_value={"items": [], "item_count": 0}) as gd:
                # discovery_store doesn't exist yet (lands in U4). Stub the import.
                import sys, types
                stub = types.ModuleType("signal_room.discovery_store")
                stub.write_merged_discovered_items = MagicMock(return_value={"item_count": 0, "items": []})
                sys.modules["signal_room.discovery_store"] = stub
                try:
                    with patch.object(cli, "_emit"):
                        rc = cli.main(["fetch", "--backend", "both"])
                finally:
                    sys.modules.pop("signal_room.discovery_store", None)
        self.assertEqual(rc, 0)
        l30.assert_called_once()
        gd.assert_called_once()
        # Both call sites pass output_path=None so neither writes solo.
        self.assertIsNone(l30.call_args.kwargs["output_path"])
        self.assertIsNone(gd.call_args.kwargs["output_path"])
        stub.write_merged_discovered_items.assert_called_once()

    def test_unknown_backend_argparse_error(self):
        from signal_room import cli

        with self.assertRaises(SystemExit) as ctx:
            cli.main(["fetch", "--backend", "nope"])
        self.assertEqual(ctx.exception.code, 2)

    def test_positional_gdelt_remains_unsupported(self):
        from signal_room import cli

        with self.assertRaises(SystemExit):
            cli.main(["fetch", "gdelt"])

    def test_parse_pillars_helper(self):
        from signal_room.cli import _parse_pillars
        self.assertIsNone(_parse_pillars("all"))
        self.assertIsNone(_parse_pillars(""))
        self.assertEqual(_parse_pillars("a,b,c"), ["a", "b", "c"])
        self.assertEqual(_parse_pillars("a, b , ,c"), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
