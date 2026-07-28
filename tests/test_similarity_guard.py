#!/usr/bin/env python3
"""Unit tests for catalog similarity guard."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import similarity_guard as sg  # noqa: E402


class TestLocalSimilarity(unittest.TestCase):
    def test_identical_is_one(self) -> None:
        self.assertEqual(sg.local_similarity("iPhone 18 leak", "iPhone 18 leak"), 1.0)

    def test_unrelated_is_low(self) -> None:
        score = sg.local_similarity(
            "Did iPhone 18 just rewrite the odds?",
            "Federal Reserve interest rate decision",
        )
        self.assertLess(score, 0.55)

    def test_same_scaffold_different_entity_is_high(self) -> None:
        a = "How iPhone 18 Pro got here so fast"
        b = "How Costco lawsuit got here so fast"
        score = sg.channel_similarity(
            a, b, entity_a="iPhone 18 Pro", entity_b="Costco lawsuit", mode="structural"
        )
        self.assertGreaterEqual(score, 0.85)

    def test_different_scaffold_same_entity_is_lower(self) -> None:
        a = "How iPhone 18 Pro got here so fast"
        b = "iPhone 18 Pro under pressure"
        score = sg.channel_similarity(
            a, b, entity_a="iPhone 18 Pro", entity_b="iPhone 18 Pro", mode="structural"
        )
        # Distinct structure should stay well under the tightened title threshold.
        self.assertLess(score, 0.72)


class TestCatalogGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.catalog = Path(self._tmp.name) / "publish_catalog.json"
        self.catalog.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "title": "How iPhone 18 Pro got here so fast",
                            "opener": "Cold open: iPhone 18 Pro.",
                            "cta": "What do you make of it? Say so below.",
                            "trend": "iPhone 18 Pro",
                        },
                        {
                            "title": "Most takes on FIFA World Cup 2026 miss this",
                            "opener": "Skip the loud take on FIFA World Cup 2026.",
                            "cta": "One-line verdict in the comments.",
                            "trend": "FIFA World Cup 2026",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        self._orig_catalog = sg.CATALOG_PATH
        self._orig_history = sg.TITLE_HISTORY_PATH
        sg.CATALOG_PATH = self.catalog
        sg.TITLE_HISTORY_PATH = Path(self._tmp.name) / "missing_history.json"

    def tearDown(self) -> None:
        sg.CATALOG_PATH = self._orig_catalog
        sg.TITLE_HISTORY_PATH = self._orig_history
        self._tmp.cleanup()

    def test_rejects_near_duplicate_title_scaffold(self) -> None:
        scores = sg.score_candidate_against_catalog(
            title="How Sony Xperia 1 VIII got here so fast",
            opener="Totally different opener about Xperia.",
            cta="Curious what you'd do — tell me.",
            entity="Sony Xperia 1 VIII",
        )
        self.assertTrue(scores["title"]["reject"], msg=scores)
        self.assertTrue(scores["reject"])

    def test_accepts_distinct_structure(self) -> None:
        scores = sg.score_candidate_against_catalog(
            title="Sony Xperia 1 VIII under pressure",
            opener="Straight facts on the Xperia drop.",
            cta="Reply with the counter-argument.",
            entity="Sony Xperia 1 VIII",
        )
        self.assertFalse(scores["title"]["reject"], msg=scores)
        self.assertFalse(scores["reject"], msg=scores)

    def test_diversify_regenerates_until_clear(self) -> None:
        content = {
            "title": "How iPhone 18 Pro got here so fast",
            "hook": "Cold open: iPhone 18 Pro.",
            "close_lines": ["Wrap", "What do you make of it? Say so below."],
            "trend_topic": "iPhone 18 Pro",
        }
        calls = {"title": 0, "opener": 0, "cta": 0}

        def regen_title(entity, _c):
            calls["title"] += 1
            return "iPhone 18 Pro under pressure"

        def regen_opener(entity, _c):
            calls["opener"] += 1
            return "Straight facts on the leak."

        def regen_cta(entity, _c):
            calls["cta"] += 1
            return ["Wrap", "Reply with the counter-argument."]

        out, scores = sg.diversify_content_against_catalog(
            content,
            regenerate_title=regen_title,
            regenerate_opener=regen_opener,
            regenerate_cta=regen_cta,
            max_attempts=4,
        )
        self.assertFalse(scores["reject"])
        self.assertGreaterEqual(calls["title"] + calls["opener"] + calls["cta"], 1)
        self.assertIn("under pressure", out["title"].lower())

    def test_assert_fail_closed(self) -> None:
        with self.assertRaises(sg.SimilarityRejectError):
            sg.assert_below_similarity_threshold(
                title="How iPhone 18 Pro got here so fast",
                opener="Cold open: iPhone 18 Pro.",
                cta="What do you make of it? Say so below.",
                entity="iPhone 18 Pro",
            )


if __name__ == "__main__":
    unittest.main()
