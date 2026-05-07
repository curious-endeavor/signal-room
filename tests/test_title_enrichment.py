import os
import unittest
from unittest.mock import patch

from signal_room.title_enrichment import clean_result_titles


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "output_text": '{"titles":[{"id":"a1","title":"McKinsey Lilli AI Breach Exposes 57,000 Accounts"}]}'
        }


class UngroundedResponse:
    status_code = 200

    def json(self):
        return {"output_text": '{"titles":[{"id":"a1","title":"iOS Jailbreaking Explained"}]}'}


class TitleEnrichmentTest(unittest.TestCase):
    def test_missing_api_key_preserves_titles_and_sets_original_title(self):
        items = [{"id": "a1", "title": "🚨 messy title", "source": "X", "summary": ""}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            cleaned, warning = clean_result_titles(items)

        self.assertIn("OPENAI_API_KEY", warning)
        self.assertEqual(cleaned[0]["title"], "🚨 messy title")
        self.assertEqual(cleaned[0]["original_title"], "🚨 messy title")

    def test_openai_title_response_replaces_title_and_preserves_original(self):
        items = [{"id": "a1", "title": "🚨 The McKinsey Lilli AI Breach: 57,000 Accounts Exposed ...", "source": "X", "summary": ""}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("signal_room.title_enrichment.requests.post", return_value=FakeResponse()):
                cleaned, warning = clean_result_titles(items)

        self.assertEqual(warning, "")
        self.assertEqual(cleaned[0]["title"], "McKinsey Lilli AI Breach Exposes 57,000 Accounts")
        self.assertEqual(cleaned[0]["original_title"], "🚨 The McKinsey Lilli AI Breach: 57,000 Accounts Exposed ...")

    def test_ungrounded_openai_title_is_rejected(self):
        items = [{"id": "a1", "title": "What is RAG (Retrieval Augmented Generation)? | IBM", "source": "Web", "summary": ""}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("signal_room.title_enrichment.requests.post", return_value=UngroundedResponse()):
                cleaned, warning = clean_result_titles(items)

        self.assertIn("not grounded", warning)
        self.assertEqual(cleaned[0]["title"], "What is RAG (Retrieval Augmented Generation)? | IBM")
        self.assertEqual(cleaned[0]["original_title"], "What is RAG (Retrieval Augmented Generation)? | IBM")

    def test_unmapped_openai_result_leaves_title_unchanged(self):
        items = [{"id": "b2", "title": "Original title", "source": "Web", "summary": ""}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("signal_room.title_enrichment.requests.post", return_value=FakeResponse()):
                cleaned, warning = clean_result_titles(items)

        self.assertIn("not grounded", warning)
        self.assertEqual(cleaned[0]["title"], "Original title")
        self.assertEqual(cleaned[0]["original_title"], "Original title")


if __name__ == "__main__":
    unittest.main()
