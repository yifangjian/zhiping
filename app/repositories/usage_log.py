"""`usage_log` 表的存取。

最重要的功能是 count_pushes_this_month():每次 reply 失敗、準備改用 push 之前
都會先問它「這個月還有額度嗎」(見 docs/DESIGN.md 5.3)。

設計上的一個取捨:記錄用量失敗絕對不能影響使用者收訊息,所以 record() 把例外吞掉
只留日誌。反過來說,查詢額度失敗時要不要放行,見 count_pushes_this_month 的註解。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.clients.supabase import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)

TABLE = "usage_log"

KIND_CHAT = "chat"
KIND_EXTRACT = "extract"
KIND_PUSH = "push"
KIND_NOTIFY = "notify"
# 網路查詢只計次,token 已經算在同一次的 chat 那筆裡
KIND_SEARCH = "search"

# LINE 的免費額度以曆月計算。官方帳號註冊在台灣,所以用台北時間切月份。
BILLING_TIMEZONE = "Asia/Taipei"


async def record(
    db: SupabaseClient,
    kind: str,
    line_user_id: Optional[str] = None,
    model: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """記錄一次用量。失敗只留日誌,不往外丟。"""
    try:
        await db.insert(
            TABLE,
            {
                "line_user_id": line_user_id,
                "kind": kind,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            returning=False,
        )
    except SupabaseError as exc:
        logger.warning("寫入 usage_log 失敗(%s):%s", kind, exc)


async def count_pushes_this_month(db: SupabaseClient) -> Optional[int]:
    """統計本月已送出的 push 則數。查詢失敗回傳 None。

    回傳 None(而不是 0)是刻意的:呼叫端得以區分「還有額度」與「不知道有沒有額度」,
    自己決定要保守還是放行。

    這裡用「撈回 id 再數長度」而不是 PostgREST 的 count 標頭,因為每月上限就是幾百筆,
    多傳一點資料換取程式碼簡單是划算的。
    """
    try:
        rows = await db.select(
            TABLE,
            {
                "kind": "eq.{}".format(KIND_PUSH),
                "created_at": "gte.{}".format(_month_start_utc().isoformat()),
                "select": "id",
            },
        )
    except SupabaseError as exc:
        logger.warning("查詢本月 push 用量失敗:%s", exc)
        return None
    return len(rows)


async def count_today(db: SupabaseClient, kind: str) -> Optional[int]:
    """統計今天某個用途已經用了幾次。查詢失敗回傳 None。

    「今天」以台北時間為準。使用者會跨時區移動(見 docs/DESIGN.md 2.3),
    但每日上限是成本控制,成本是專案擁有者在付的,所以跟著擁有者的時區走。
    """
    try:
        rows = await db.select(
            TABLE,
            {
                "kind": "eq.{}".format(kind),
                "created_at": "gte.{}".format(_day_start_utc().isoformat()),
                "select": "id",
            },
        )
    except SupabaseError as exc:
        logger.warning("查詢今日 %s 用量失敗:%s", kind, exc)
        return None
    return len(rows)


def _day_start_utc() -> datetime:
    """今天零時(台北時間),換算成 UTC。"""
    local_now = _local_now()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.astimezone(timezone.utc)


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(BILLING_TIMEZONE))
    except Exception:  # pragma: no cover - 系統缺 tzdata 時退回 UTC
        logger.warning("取不到時區 %s,改用 UTC 計算日期界線", BILLING_TIMEZONE)
        return datetime.now(timezone.utc)


def _month_start_utc() -> datetime:
    """本月一號零時(台北時間),換算成 UTC。"""
    month_start = _local_now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return month_start.astimezone(timezone.utc)
