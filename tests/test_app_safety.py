#!/usr/bin/env python3
"""Tests for BlinkViral app-safety series."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import app_safety as asafety  # noqa: E402
import content_gen as cg  # noqa: E402
from topic_validation import passes_named_entity_gate  # noqa: E402


class AppSafetySeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_path = Path(self._tmpdir.name) / "app_safety_queue.json"
        self._orig_path = asafety.QUEUE_STATE_PATH
        asafety.QUEUE_STATE_PATH = self.state_path
        os.environ["CHANNEL_NICHE"] = "app_safety"

    def tearDown(self) -> None:
        asafety.QUEUE_STATE_PATH = self._orig_path

    def test_title_pattern(self) -> None:
        title = asafety.app_safety_title("Vidmate")
        self.assertEqual(title, "Vidmate - Is It Safe? The TRUTH")

    def test_queue_has_20(self) -> None:
        self.assertEqual(len(asafety.APP_SAFETY_QUEUE), 20)

    def test_package_fields(self) -> None:
        pkg = asafety.build_app_safety_package(asafety.APP_SAFETY_QUEUE[0])
        self.assertTrue(pkg["title"].endswith("Is It Safe? The TRUTH"))
        self.assertEqual(pkg["niche"], "app_safety")
        self.assertEqual(len(pkg["why_lines"]), 3)
        self.assertEqual(len(pkg["captions"]), 8)
        self.assertIn("verdict", pkg)

    def test_rotation_advances(self) -> None:
        a = asafety.pick_next_app()
        b = asafety.pick_next_app()
        self.assertNotEqual(a["id"], b["id"])

    def test_niche_for_slot_defaults_app_safety(self) -> None:
        self.assertEqual(cg.niche_for_slot("morning"), "app_safety")
        self.assertEqual(cg.niche_for_slot("evening"), "app_safety")

    def test_sanitize_keeps_truth_title(self) -> None:
        out = cg._sanitize_title(
            "Something else",
            trend="Snaptube",
            niche="app_safety",
        )
        self.assertEqual(out, "Snaptube - Is It Safe? The TRUTH")

    def test_apps_pass_named_entity_gate(self) -> None:
        for app in asafety.APP_SAFETY_QUEUE:
            self.assertTrue(
                passes_named_entity_gate(app["name"]),
                msg=f"{app['name']} failed NER gate",
            )

    def test_resolve_app_id_from_topic_slug(self) -> None:
        self.assertEqual(asafety.resolve_app_id("app_safety_movie_box"), "movie_box")
        self.assertEqual(asafety.resolve_app_id("Movie Box"), "movie_box")

    def test_catalog_app_id_prefers_app_id_field(self) -> None:
        entry = {"title": "X - Is It Safe? The TRUTH", "app_id": "cinema_hd", "trend": "app_safety_movie_box"}
        self.assertEqual(asafety.catalog_app_id(entry), "cinema_hd")

    def test_pick_next_skips_published_apps(self) -> None:
        with mock.patch.object(
            asafety, "published_app_ids", return_value={"vidmate", "snaptube", "movie_box"}
        ):
            app = asafety.pick_next_app()
        self.assertEqual(app["id"], "cinema_hd")

    def test_duplicate_guard_blocks_same_app_same_title(self) -> None:
        import similarity_guard as sg

        catalog_path = Path(self._tmpdir.name) / "publish_catalog.json"
        self._orig_catalog = sg.CATALOG_PATH
        sg.CATALOG_PATH = catalog_path
        self.addCleanup(lambda: setattr(sg, "CATALOG_PATH", self._orig_catalog))
        sg.save_catalog(
            [
                {
                    "title": "Movie Box - Is It Safe? The TRUTH",
                    "trend": "Movie Box",
                    "app_id": "movie_box",
                    "opener": "a",
                    "cta": "b",
                    "source": "upload.success",
                }
            ]
        )
        content = asafety.build_app_safety_package(asafety.lookup_app("movie_box"))
        with self.assertRaises(RuntimeError):
            cg._apply_similarity_guard(content, "Movie Box")

    def test_generate_kit_row_does_not_block_same_app(self) -> None:
        import similarity_guard as sg

        catalog_path = Path(self._tmpdir.name) / "publish_catalog.json"
        self._orig_catalog = sg.CATALOG_PATH
        sg.CATALOG_PATH = catalog_path
        self.addCleanup(lambda: setattr(sg, "CATALOG_PATH", self._orig_catalog))
        sg.save_catalog(
            [
                {
                    "title": "Cinema HD - Is It Safe? The TRUTH",
                    "trend": "Cinema HD",
                    "app_id": "cinema_hd",
                    "opener": "a",
                    "cta": "b",
                    "source": "generate.kit",
                }
            ]
        )
        content = asafety.build_app_safety_package(asafety.lookup_app("cinema_hd"))
        out = cg._apply_similarity_guard(content, "Cinema HD")
        self.assertEqual(out["title"], content["title"])
        self.assertNotIn("cinema_hd", asafety.published_app_ids())


    def test_generate_topic_app_safety(self) -> None:
        with mock.patch("content_gen._apply_similarity_guard", side_effect=lambda c, t: c):
            content = cg.generate_topic(slot="morning")
        self.assertEqual(content["niche"], "app_safety")
        self.assertIn("Is It Safe? The TRUTH", content["title"])
        self.assertTrue(content.get("trend_topic"))


if __name__ == "__main__":
    unittest.main()
