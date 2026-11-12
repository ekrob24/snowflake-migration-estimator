from datetime import datetime

import pytest

from sfestimate.parsers import parse_ts


@pytest.mark.parametrize("value, expected", [
    ("2026-01-05 10:11:12", datetime(2026, 1, 5, 10, 11, 12)),
    ("2026-01-05T10:11:12", datetime(2026, 1, 5, 10, 11, 12)),
    ("2026-01-05 10:11:12.123456", datetime(2026, 1, 5, 10, 11, 12, 123456)),
    ("2026-01-05T10:11:12Z", datetime(2026, 1, 5, 10, 11, 12)),
    ("2026-01-05T10:11:12.5Z", datetime(2026, 1, 5, 10, 11, 12, 500000)),
    ("  2026-01-05 10:11:12  ", datetime(2026, 1, 5, 10, 11, 12)),
])
def test_basic_formats(value, expected):
    assert parse_ts(value) == expected


def test_more_than_six_fractional_digits_truncated():
    assert parse_ts("2026-01-05 10:11:12.123456789") == datetime(2026, 1, 5, 10, 11, 12, 123456)
    assert parse_ts("2026-01-05T10:11:12.1234567Z") == datetime(2026, 1, 5, 10, 11, 12, 123456)


@pytest.mark.parametrize("value, expected", [
    ("2026-01-05 10:00:00+02:00", datetime(2026, 1, 5, 8, 0, 0)),
    ("2026-01-05T10:00:00-05:00", datetime(2026, 1, 5, 15, 0, 0)),
    ("2026-01-05 23:30:00.1234567-01:00", datetime(2026, 1, 6, 0, 30, 0, 123456)),
    ("2026-01-05 10:00:00+00:00", datetime(2026, 1, 5, 10, 0, 0)),
])
def test_offsets_become_naive_utc(value, expected):
    got = parse_ts(value)
    assert got == expected
    assert got.tzinfo is None


@pytest.mark.parametrize("value, expected", [
    ("2026-01-05 10:11:12 UTC", datetime(2026, 1, 5, 10, 11, 12)),
    ("2026-01-05 10:11:12.123456 UTC", datetime(2026, 1, 5, 10, 11, 12, 123456)),
    ("2026-01-05T10:11:12.1234567 UTC", datetime(2026, 1, 5, 10, 11, 12, 123456)),
])
def test_utc_suffix(value, expected):
    # BigQuery's CSV timestamp format.
    assert parse_ts(value) == expected
