#!/usr/bin/env python3
"""Canonical event-timestamp normalization, journald native time, 72h freshness."""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from astra.timestamps import (
    as_utc,
    canonical_now,
    fresh,
    from_journald,
    parse_leading,
    to_datetime,
)


class TestParseLeading(unittest.TestCase):
    DUBAI = ZoneInfo("Asia/Dubai")

    def test_naive_space_separated_uses_the_host_local_timezone(self):
        self.assertEqual(
            parse_leading("2026-09-12 12:00:00 WARNING disk full", timezone_=self.DUBAI),
            ("2026-09-12T12:00:00+04:00", "ok"),
        )

    def test_offset_and_fractional_forms_convert_to_host_local_timezone(self):
        self.assertEqual(
            parse_leading("2026-09-12T12:00:00,250+02:00 msg", timezone_=self.DUBAI),
            ("2026-09-12T14:00:00+04:00", "ok"),
        )
        self.assertEqual(
            parse_leading("2026-09-12 12:00:00.5-05:00 msg", timezone_=self.DUBAI),
            ("2026-09-12T21:00:00+04:00", "ok"),
        )
        self.assertEqual(
            parse_leading("2026-09-12 12:00:00Z plain", timezone_=self.DUBAI),
            ("2026-09-12T16:00:00+04:00", "ok"),
        )

    def test_missing_and_malformed_are_distinguished(self):
        self.assertEqual(parse_leading("plain diagnostic without level"), (None, "missing"))
        self.assertEqual(parse_leading(""), (None, "missing"))
        self.assertEqual(parse_leading("2026-13-45 99:99:99 broken"), (None, "malformed"))
        self.assertEqual(parse_leading("2026-09-12 25:00:00 bad hour"), (None, "malformed"))

    def test_timestamped_continuation_style_lines_are_not_leading_timestamps(self):
        # A traceback frame must never be mistaken for a timestamp.
        self.assertEqual(parse_leading('  File "m.py", line 3'), (None, "missing"))


class TestJournaldNative(unittest.TestCase):
    DUBAI = ZoneInfo("Asia/Dubai")

    def test_native_realtime_timestamp_uses_host_local_timezone(self):
        fields = {"__REALTIME_TIMESTAMP": "1789230000000000"}
        canonical, status = from_journald(fields, timezone_=self.DUBAI)
        self.assertEqual(status, "ok")
        self.assertEqual(canonical, "2026-09-12T20:20:00+04:00")
        self.assertEqual(
            as_utc(canonical),
            datetime.fromtimestamp(1789230000, tz=timezone.utc),
        )

    def test_missing_native_timestamp_reports_missing(self):
        self.assertEqual(from_journald({}), (None, "missing"))
        self.assertEqual(from_journald({"__REALTIME_TIMESTAMP": "junk"}), (None, "malformed"))


class TestFreshness(unittest.TestCase):
    NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)

    def test_exactly_72h_is_fresh_one_second_older_is_not(self):
        self.assertTrue(fresh("2026-09-10T00:00:00Z", self.NOW))
        self.assertFalse(fresh("2026-09-09T23:59:59Z", self.NOW))

    def test_fresh_accepts_offset_and_naive_forms(self):
        self.assertTrue(fresh("2026-09-12T02:00:00+02:00", self.NOW))
        self.assertTrue(fresh("2026-09-12 12:00:00", self.NOW))
        self.assertFalse(fresh("not-a-timestamp", self.NOW))

    def test_as_utc_and_canonical_now(self):
        self.assertEqual(as_utc("2026-09-12T12:00:00Z"), datetime(2026, 9, 12, 12, tzinfo=timezone.utc))
        self.assertEqual(as_utc(datetime(2026, 9, 12, 12)), datetime(2026, 9, 12, 12, tzinfo=timezone.utc))
        self.assertTrue(canonical_now(self.NOW), "2026-09-13T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
