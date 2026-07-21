#!/usr/bin/env python3
"""Unit tests for trending-topic entity validation and pre-publish guards."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import content_gen as cg  # noqa: E402
from topic_validation import (  # noqa: E402
    EntityValidationError,
    assert_publishable_metadata,
    assert_publishable_title,
    contains_invalid_publish_text,
    is_valid_entity,
    require_valid_entity,
)


class TestEntityValidation(unittest.TestCase):
    def test_rejects_null_like_literals(self) -> None:
        for bad in (None, "", "None", "none", "NULL", "undefined", "n/a", "..."):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_entity(bad))

    def test_accepts_real_topic(self) -> None:
        self.assertTrue(is_valid_entity("OpenAI pricing update"))

    def test_require_valid_entity_raises(self) -> None:
        with self.assertRaises(EntityValidationError):
            require_valid_entity(None, source="unit_test", raw_upstream={"selected_topic": None})


class TestPublishGuard(unittest.TestCase):
    def test_blocks_none_in_rendered_title(self) -> None:
        self.assertTrue(contains_invalid_publish_text("None explained in 30 sec"))
        with self.assertRaises(EntityValidationError):
            assert_publishable_metadata("None explained in 30 sec", "Trending now: foo")

    def test_allows_clean_title(self) -> None:
        assert_publishable_metadata(
            "OpenAI pricing explained in 30 sec",
            "Trending now: OpenAI pricing update",
        )


class TestPipelineSkipsPublishOnNullEntity(unittest.TestCase):
    def test_fallback_title_never_uses_none_entity(self) -> None:
        title = cg._fallback_title_from_topic(None)
        self.assertNotIn("None", title)
        self.assertFalse(contains_invalid_publish_text(title))

    def test_sanitize_title_with_invalid_trend_does_not_emit_none(self) -> None:
        title = cg._sanitize_title("Shock Awaits: The Truth", trend="None")
        self.assertNotIn("None", title)
        assert_publishable_title(title)

    @patch("content_gen.call_llm")
    @patch("content_gen.pick_latest_topic")
    @patch("content_gen.time.sleep")
    def test_generate_topic_null_seed_uses_fallback_not_none_title(
        self, mock_sleep, mock_pick, mock_llm
    ) -> None:
        mock_pick.return_value = (None, None)
        mock_llm.side_effect = RuntimeError("forced model failure")

        topic = cg.generate_topic(slot="evening")

        self.assertNotIn("None", topic["title"])
        assert_publishable_title(topic["title"])
        mock_pick.assert_called_once()


class TestUploadGuard(unittest.TestCase):
    def test_upload_blocks_none_title(self) -> None:
        import upload as up

        with self.assertRaises(EntityValidationError):
            up.upload_video("fake-token", "missing.mp4", "None explained in 30 sec", "desc")


if __name__ == "__main__":
    unittest.main()
