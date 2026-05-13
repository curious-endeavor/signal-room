import json
import tempfile
import unittest
from pathlib import Path

from signal_room import discovery_store


class NormalizeUrlTest(unittest.TestCase):
    def test_trailing_slash_collapses(self):
        a = discovery_store.normalize_url("https://example.com/path/")
        b = discovery_store.normalize_url("https://example.com/path")
        self.assertEqual(a, b)

    def test_root_slash_preserved(self):
        self.assertEqual(discovery_store.normalize_url("https://example.com/"), "https://example.com/")

    def test_drops_utm_params(self):
        a = discovery_store.normalize_url("https://example.com/x?utm_source=foo&utm_medium=bar")
        b = discovery_store.normalize_url("https://example.com/x")
        self.assertEqual(a, b)

    def test_drops_fbclid_and_gclid(self):
        a = discovery_store.normalize_url("https://example.com/x?fbclid=abc&gclid=def")
        b = discovery_store.normalize_url("https://example.com/x")
        self.assertEqual(a, b)

    def test_preserves_real_query_params(self):
        a = discovery_store.normalize_url("https://example.com/x?id=42&utm_source=foo")
        self.assertIn("id=42", a)
        self.assertNotIn("utm_source", a)

    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            discovery_store.normalize_url("HTTPS://Example.COM/Path"),
            "https://example.com/Path",
        )

    def test_strips_fragment(self):
        self.assertEqual(
            discovery_store.normalize_url("https://example.com/x#section"),
            "https://example.com/x",
        )

    def test_empty_input(self):
        self.assertEqual(discovery_store.normalize_url(""), "")


def _gdelt_row(url, title="t", first_seen=None):
    row = {
        "id": "gdelt-1",
        "title": title,
        "source": "example.com",
        "source_url": url,
        "discovery_method": "gdelt",
        "meta": {"source": "gdelt"},
        "metadata": {"language": "English", "sourcecountry": "United States"},
        "tags": ["pillar:p1", "platform:news"],
    }
    if first_seen:
        row["first_seen_at"] = first_seen
    return row


def _last30_row(url, title="t", first_seen=None, summary="some snippet"):
    row = {
        "id": "l30-1",
        "title": title,
        "source": "Reddit / r/news",
        "source_url": url,
        "summary": summary,
        "discovery_method": "last30days",
        "engagement": {"upvotes": 42},
        "metadata": {"subreddit": "news"},
        "tags": ["platform:reddit"],
    }
    if first_seen:
        row["first_seen_at"] = first_seen
    return row


class WriteMergedDiscoveredItemsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "discovered_items.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_disjoint_urls_produce_sum_of_rows(self):
        l30 = {"items": [_last30_row("https://a.example/1")], "backend": "last30days"}
        gd = {"items": [_gdelt_row("https://b.example/2")], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [l30, gd])
        self.assertEqual(out["item_count"], 2)
        urls = sorted(r["source_url"] for r in out["items"])
        self.assertEqual(urls, ["https://a.example/1", "https://b.example/2"])

    def test_shared_url_merges_with_sorted_unique_source_list(self):
        shared = "https://example.com/article"
        l30 = {"items": [_last30_row(shared)], "backend": "last30days"}
        gd = {"items": [_gdelt_row(shared)], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [l30, gd])
        self.assertEqual(out["item_count"], 1)
        row = out["items"][0]
        self.assertEqual(row["meta"]["source"], ["gdelt", "last30days"])
        # last30days fields preserved
        self.assertEqual(row["summary"], "some snippet")
        # gdelt metadata merged in
        self.assertEqual(row["metadata"]["language"], "English")
        self.assertEqual(row["metadata"]["sourcecountry"], "United States")
        # tags unioned
        self.assertIn("platform:reddit", row["tags"])
        self.assertIn("platform:news", row["tags"])

    def test_url_normalization_collapses_utm_variant_with_trailing_slash(self):
        l30 = {"items": [_last30_row("https://example.com/x/")], "backend": "last30days"}
        gd = {"items": [_gdelt_row("https://example.com/x?utm_source=foo")], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [l30, gd])
        self.assertEqual(out["item_count"], 1)
        self.assertEqual(out["items"][0]["meta"]["source"], ["gdelt", "last30days"])

    def test_inferred_source_when_meta_missing(self):
        legacy = _last30_row("https://example.com/legacy")
        legacy.pop("meta", None)  # legacy row from before meta.source was stamped
        l30 = {"items": [legacy], "backend": "last30days"}
        gd = {"items": [_gdelt_row("https://example.com/legacy")], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [l30, gd])
        self.assertEqual(out["items"][0]["meta"]["source"], ["gdelt", "last30days"])

    def test_first_seen_at_preserves_earliest_on_collision(self):
        url = "https://example.com/article"
        l30 = {"items": [_last30_row(url, first_seen="2026-05-10T00:00:00+00:00")], "backend": "last30days"}
        gd = {"items": [_gdelt_row(url, first_seen="2026-05-13T00:00:00+00:00")], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [l30, gd])
        self.assertEqual(out["items"][0]["first_seen_at"], "2026-05-10T00:00:00+00:00")

    def test_first_seen_at_stamped_for_new_url(self):
        gd = {"items": [_gdelt_row("https://example.com/fresh")], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [gd])
        self.assertTrue(out["items"][0]["first_seen_at"].endswith("+00:00"))

    def test_existing_on_disk_file_seeds_merge_and_first_seen_survives_refetch(self):
        # First write seeds the file with a gdelt row.
        first = {"items": [_gdelt_row("https://example.com/x", first_seen="2026-05-01T00:00:00+00:00")], "backend": "gdelt"}
        discovery_store.write_merged_discovered_items(self.path, [first])
        # Second write of the same URL should preserve the earlier first_seen_at.
        second = {"items": [_gdelt_row("https://example.com/x", first_seen="2026-05-13T00:00:00+00:00")], "backend": "gdelt"}
        out = discovery_store.write_merged_discovered_items(self.path, [second])
        self.assertEqual(out["item_count"], 1)
        self.assertEqual(out["items"][0]["first_seen_at"], "2026-05-01T00:00:00+00:00")

    def test_legacy_list_payload_accepted(self):
        # Sample fixtures historically used a bare list shape.
        l30_list = [_last30_row("https://example.com/legacy-list")]
        out = discovery_store.write_merged_discovered_items(self.path, [l30_list])
        self.assertEqual(out["item_count"], 1)

    def test_persists_to_disk_with_backend_marker(self):
        gd = {"items": [_gdelt_row("https://example.com/y")], "backend": "gdelt"}
        discovery_store.write_merged_discovered_items(self.path, [gd])
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(on_disk["backend"], "gdelt")
        self.assertEqual(on_disk["item_count"], 1)


class PipelineDispatchTest(unittest.TestCase):
    """Verify pipeline.run_pipeline routes to the right fetcher(s) by backend."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.discovered = self.tmp / "discovered.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_gdelt_backend_invokes_only_gdelt(self):
        from unittest.mock import patch
        from signal_room import pipeline

        with patch.object(pipeline, "fetch_last30days") as l30:
            with patch.object(pipeline, "fetch_gdelt", return_value={"items": [], "backend": "gdelt"}) as gd:
                with patch.object(pipeline, "write_merged_discovered_items") as merge:
                    with patch.object(pipeline, "load_raw_items", return_value=[]):
                        with patch.object(pipeline, "score_items", return_value=[]):
                            with patch.object(pipeline, "render_digest"):
                                pipeline.run_pipeline(
                                    fetch_backend="gdelt",
                                    discovered_path=self.discovered,
                                    fetch_pillars=["chatbot-failures"],
                                    fetch_timespan="1d",
                                    fetch_max=5,
                                    include_fixtures=False,
                                )
        l30.assert_not_called()
        gd.assert_called_once()
        merge.assert_called_once()  # gdelt-only writes still route through merge so first_seen survives
        self.assertEqual(gd.call_args.kwargs["pillars"], ["chatbot-failures"])
        self.assertIsNone(gd.call_args.kwargs["output_path"])

    def test_both_backend_invokes_both_and_merges(self):
        from unittest.mock import patch
        from signal_room import pipeline

        with patch.object(pipeline, "fetch_last30days", return_value={"items": [], "backend": "last30days"}) as l30:
            with patch.object(pipeline, "fetch_gdelt", return_value={"items": [], "backend": "gdelt"}) as gd:
                with patch.object(pipeline, "write_merged_discovered_items") as merge:
                    with patch.object(pipeline, "load_raw_items", return_value=[]):
                        with patch.object(pipeline, "score_items", return_value=[]):
                            with patch.object(pipeline, "render_digest"):
                                pipeline.run_pipeline(
                                    fetch_backend="both",
                                    discovered_path=self.discovered,
                                    include_fixtures=False,
                                )
        l30.assert_called_once()
        gd.assert_called_once()
        merge.assert_called_once()
        self.assertIsNone(l30.call_args.kwargs["output_path"])
        self.assertIsNone(gd.call_args.kwargs["output_path"])


if __name__ == "__main__":
    unittest.main()
