#!/usr/bin/env python3
"""Unit tests for configurable daily upload cap."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_cap as dc  # noqa: E402


class TestDailyCap(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "daily_upload_log.json"
        self.log.write_text(json.dumps({"days": {}}), encoding="utf-8")
        self._orig = dc.LOG_PATH
        dc.LOG_PATH = self.log
        os.environ.pop("BYPASS_DAILY_UPLOAD_CAP", None)
        os.environ["DAILY_UPLOAD_CAP"] = "2"

    def tearDown(self) -> None:
        dc.LOG_PATH = self._orig
        os.environ.pop("DAILY_UPLOAD_CAP", None)
        os.environ.pop("BYPASS_DAILY_UPLOAD_CAP", None)
        self._tmp.cleanup()

    def test_under_cap_allows(self) -> None:
        info = dc.assert_under_daily_cap(source="unit")
        self.assertEqual(info["count"], 0)
        self.assertEqual(info["cap"], 2)

    def test_at_cap_raises(self) -> None:
        dc.record_daily_upload(title="a", source="t")
        dc.record_daily_upload(title="b", source="t")
        with self.assertRaises(dc.DailyCapExceeded) as ctx:
            dc.assert_under_daily_cap(source="unit")
        self.assertEqual(ctx.exception.count, 2)
        self.assertEqual(ctx.exception.cap, 2)

    def test_bypass_allows_when_capped(self) -> None:
        dc.record_daily_upload(title="a", source="t")
        dc.record_daily_upload(title="b", source="t")
        os.environ["BYPASS_DAILY_UPLOAD_CAP"] = "1"
        info = dc.assert_under_daily_cap(source="unit")
        self.assertTrue(info["bypass"])

    def test_cli_check_exit_codes(self) -> None:
        self.assertEqual(dc.main(["--check"]), 0)
        dc.record_daily_upload(title="a", source="t")
        dc.record_daily_upload(title="b", source="t")
        self.assertEqual(dc.main(["--check"]), 3)


if __name__ == "__main__":
    unittest.main()
