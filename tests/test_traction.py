import unittest

from signal_room.models import RawItem
from signal_room.traction import rank_items_by_traction, traction_score
from signal_room.web import _result_context


def item(
    item_id,
    title,
    platform,
    engagement=None,
    engagement_score=None,
    date="2026-05-07",
):
    source = {
        "x": "X / @source",
        "instagram": "www.instagram.com",
        "youtube": "YouTube",
        "reddit": "Reddit",
        "grounding": "example.com",
    }.get(platform, platform)
    return RawItem(
        id=item_id,
        title=title,
        source=source,
        source_url=f"https://example.com/{item_id}",
        date=date,
        summary="summary",
        content="content",
        discovery_method="last30days",
        candidate_source=True,
        tags=[f"platform:{platform}"],
        engagement=engagement or {},
        engagement_score=engagement_score,
    )


class TractionRankingTest(unittest.TestCase):
    def test_social_ranking_prefers_instagram_views_over_low_x_likes(self):
        rows = rank_items_by_traction(
            [
                item("x-low", "X low likes", "x", {"likes": 4, "replies": 2}, engagement_score=7),
                item("ig-high", "Instagram high views", "instagram", {"views": 101_472, "likes": 8_203}),
            ]
        )

        self.assertEqual([row["id"] for row in rows], ["ig-high", "x-low"])
        self.assertEqual(rows[0]["result_bucket"], "social")

    def test_zero_traction_x_sorts_below_x_with_engagement(self):
        rows = rank_items_by_traction(
            [
                item("x-zero", "X zero", "x", {"likes": 0, "reposts": 0, "replies": 0}),
                item("x-active", "X active", "x", {"likes": 35, "reposts": 5, "replies": 9}),
            ]
        )

        self.assertEqual([row["id"] for row in rows], ["x-active", "x-zero"])

    def test_reddit_high_score_sorts_above_reddit_one_point(self):
        rows = rank_items_by_traction(
            [
                item("reddit-low", "Reddit low", "reddit", {"score": 1, "num_comments": 0}),
                item("reddit-high", "Reddit high", "reddit", {"score": 1_350, "num_comments": 99}),
            ]
        )

        self.assertEqual([row["id"] for row in rows], ["reddit-high", "reddit-low"])

    def test_fallback_metrics_are_used_without_engagement_score(self):
        row = item("ig", "Instagram", "instagram", {"views": 10_000, "likes": 250})

        self.assertGreater(traction_score(row), 0)

    def test_result_context_returns_social_before_references_without_ce_score_order(self):
        rows = [
            item("web", "Reference", "grounding", date="2026-05-07").to_dict(),
            item("x", "Social", "x", {"likes": 1}, date="2026-05-07").to_dict(),
        ]

        context = _result_context(rows)

        self.assertEqual([row["id"] for row in context["items"]], ["x", "web"])
        self.assertEqual(context["date_groups"][0]["rows"][0]["id"], "x")
        self.assertEqual(context["reference_groups"][0]["rows"][0]["id"], "web")

    def test_date_groups_order_rows_by_traction(self):
        rows = [
            {
                **item("low", "Low traction", "x", {"likes": 1}, date="2026-05-07").to_dict(),
                "result_bucket": "social",
                "traction_score": 1,
                "score": 1,
                "rank": 1,
            },
            {
                **item("high", "High traction", "x", {"likes": 50}, date="2026-05-07").to_dict(),
                "result_bucket": "social",
                "traction_score": 50,
                "score": 50,
                "rank": 2,
            },
        ]

        context = _result_context(rows)

        self.assertEqual([row["id"] for row in context["date_groups"][0]["rows"]], ["high", "low"])


if __name__ == "__main__":
    unittest.main()
