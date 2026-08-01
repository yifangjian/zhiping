"""時間戳解析。

**為什麼需要自己寫**:PostgREST 回傳的 timestamptz 會把微秒尾端的 0 砍掉,
所以小數位數是變動的:

    2026-08-01T18:22:30.705255+00:00   6 位
    2026-08-01T18:22:35.89848+00:00    5 位  ← datetime.fromisoformat 在 3.9/3.10 會丟 ValueError

Python 3.11 之後 fromisoformat 才接受任意位數。本專案的執行環境是 3.11+,但開發機
是系統內建的 3.9,而這種 bug 的症狀是「大約十分之一的時間戳解析失敗」——
間歇、安靜、難查。與其依賴執行環境的版本,不如自己補齊位數。
"""

import re
from datetime import datetime, timezone
from typing import Any, Optional

# 小數秒的部分,位數不固定
FRACTION_PATTERN = re.compile(r"\.(\d+)")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """把資料庫回傳的時間字串轉成帶時區的 datetime。解析不了回 None。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None

    text = value.strip().replace("Z", "+00:00")
    # 把小數秒補成 6 位(多的截斷、少的補零),3.9 的 fromisoformat 才吃得下
    text = FRACTION_PATTERN.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hours_since(value: Any) -> Optional[float]:
    """距離某個時間點過了幾小時。解析不了回 None。"""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def days_since(value: Any) -> Optional[int]:
    """距離某個時間點過了幾天。解析不了回 None。"""
    hours = hours_since(value)
    return None if hours is None else int(hours // 24)
