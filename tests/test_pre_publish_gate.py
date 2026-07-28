#!/usr/bin/env python3
"""Unit tests for fail-closed pre-publish gate."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pre_publish_gate as ppg  # noqa: E402
import similarity_guard as sg  # noqa: E402


class TestPrePublishGate(unittest.TestCase):
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
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self._orig_catalog = sg.CATALOG_PATH
        self._orig_history = sg.TITLE_HISTORY_PATH
        self._orig_block = ppg.BLOCK_LOG_PATH
        sg.CATALOG_PATH = self.catalog
        sg.TITLE_HISTORY_PATH = Path(self._tmp.name) / "missing.json"
        ppg.BLOCK_LOG_PATH = Path(self._tmp.name) / "blocks.json"

    def tearDown(self) -> None:
        sg.CATALOG_PATH = self._orig_catalog
        sg.TITLE_HISTORY_PATH = self._orig_history
        ppg.BLOCK_LOG_PATH = self._orig_block
        self._tmp.cleanup()

    def test_blocks_null_title(self) -> None:
        with self.assertRaises(ppg.PrePublishBlocked) as ctx:
            ppg.run_pre_publish_gate(
                title="None explained in 30 sec",
                description="Something real about OpenAI",
                trend="OpenAI",
                source="unit_test",
            )
        self.assertEqual(ctx.exception.reason, "invalid_publish_metadata")

    def test_blocks_generic_topic(self) -> None:
        with self.assertRaises(ppg.PrePublishBlocked) as ctx:
            ppg.run_pre_publish_gate(
                title="internet debate going viral could go either way",
                description="Quick take.\nReply with the counter-argument.\n#Shorts",
                trend="internet debate going viral",
                opener="Wait, what's going on?",
                source="unit_test",
            )
        self.assertEqual(ctx.exception.reason, "no_named_entity")

    def test_blocks_similar_scaffold(self) -> None:
        with self.assertRaises(ppg.PrePublishBlocked) as ctx:
            ppg.run_pre_publish_gate(
                title="How Sony Xperia 1 VIII got here so fast",
                description="Focus: Sony Xperia 1 VIII.\n\nReply with the counter-argument.\n#Shorts",
                trend="Sony Xperia 1 VIII",
                opener="Totally different opener line here.",
                source="unit_test",
            )
        self.assertEqual(ctx.exception.reason, "similarity_reject")

    def test_allows_clean_named_distinct(self) -> None:
        result = ppg.run_pre_publish_gate(
            title="Sony Xperia 1 VIII under pressure",
            description="Sony Xperia 1 VIII — short context only.\n\nReply with the counter-argument.\n#Shorts",
            trend="Sony Xperia 1 VIII",
            opener="Straight facts on the Xperia drop.",
            source="unit_test",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["checks"]["named_entity"]["ok"])
        self.assertTrue(result["checks"]["similarity"]["ok"])

    def test_excludes_self_from_catalog(self) -> None:
        # Same title already in catalog must not self-block at publish time.
        result = ppg.run_pre_publish_gate(
            title="How iPhone 18 Pro got here so fast",
            description="iPhone 18 Pro context.\n\nCurious what you'd do — tell me.\n#Shorts",
            trend="iPhone 18 Pro",
            opener="A fresh distinct opener about the leak.",
            source="unit_test",
        )
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
