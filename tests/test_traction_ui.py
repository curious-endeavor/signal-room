from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TractionUiTest(unittest.TestCase):
    def test_results_ui_hides_ce_ranking_context(self):
        partial = (ROOT / "signal_room" / "templates" / "partials" / "results_list.html").read_text(encoding="utf-8")
        script = (ROOT / "signal_room" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn('class="score"', partial)
        self.assertNotIn('class="score"', script)
        self.assertNotIn("CE angle", partial)
        self.assertNotIn("CE angle", script)
        self.assertNotIn("Unsorted", partial)
        self.assertNotIn("Unsorted", script)
        self.assertIn("traction_label", partial)
        self.assertIn("traction_label", script)
        self.assertIn("References", partial)
        self.assertIn("References", script)


if __name__ == "__main__":
    unittest.main()
