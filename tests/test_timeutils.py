"""時間戳解析(app/timeutils.py)。

這組測試存在的原因是一個間歇性 bug:PostgREST 會把微秒尾端的 0 砍掉,
所以小數位數是變動的,而 Python 3.9/3.10 的 fromisoformat 只吃 3 或 6 位。
症狀是「大約十分之一的時間戳解析失敗」——安靜、隨機、難查。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.timeutils import days_since, hours_since, parse_timestamp


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-01T18:22:30.705255+00:00",  # 6 位,正常
        "2026-08-01T18:22:35.89848+00:00",   # 5 位,PostgREST 砍過尾零
        "2026-08-01T18:22:35.8+00:00",       # 1 位
        "2026-08-01T18:22:35+00:00",         # 沒有小數
        "2026-08-01T18:22:35.123456789+00:00",  # 9 位,奈秒
        "2026-08-01T18:22:35Z",              # Z 結尾
    ],
)
def test_各種小數位數都解析得出來(raw):
    parsed = parse_timestamp(raw)

    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.tzinfo is not None


def test_沒有時區資訊時當作_UTC():
    parsed = parse_timestamp("2026-08-01T18:22:35.89848")

    assert parsed.tzinfo == timezone.utc


def test_解析不了回_None():
    assert parse_timestamp("昨天") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp(12345) is None


def test_已經是_datetime_就直接用():
    moment = datetime(2026, 8, 1, tzinfo=timezone.utc)

    assert parse_timestamp(moment) == moment


def test_計算經過的時數與天數():
    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3, hours=1)
    raw = three_days_ago.isoformat()

    assert 72 < hours_since(raw) < 74
    assert days_since(raw) == 3
