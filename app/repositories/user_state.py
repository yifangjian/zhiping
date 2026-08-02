"""`user_state` 表的存取。

目前只存時區。獨立成一張表而不是塞進 memories,是因為時區不是「關於他的記憶」,
而是「解讀他訊息所需的環境資訊」——它要參與每一次回覆的組裝,而且只有一個值,
不該跟幾十條記憶混在一起排序。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.clients.supabase import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)

TABLE = "user_state"

# 使用者說不出「Asia/Ho_Chi_Minh」這種字串,他會說「我在越南」。
# 這張對照表涵蓋他航線上的國家與主要港口(見 docs/DESIGN.md 2.3)。
PLACE_TIMEZONES = {
    "台灣": "Asia/Taipei",
    "臺灣": "Asia/Taipei",
    "高雄": "Asia/Taipei",
    "基隆": "Asia/Taipei",
    "台中": "Asia/Taipei",
    "中國": "Asia/Shanghai",
    "大陸": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "寧波": "Asia/Shanghai",
    "青島": "Asia/Shanghai",
    "深圳": "Asia/Shanghai",
    "香港": "Asia/Hong_Kong",
    "越南": "Asia/Ho_Chi_Minh",
    "胡志明": "Asia/Ho_Chi_Minh",
    "海防": "Asia/Ho_Chi_Minh",
    "日本": "Asia/Tokyo",
    "東京": "Asia/Tokyo",
    "橫濱": "Asia/Tokyo",
    "神戶": "Asia/Tokyo",
    "大阪": "Asia/Tokyo",
    "韓國": "Asia/Seoul",
    "釜山": "Asia/Seoul",
    "首爾": "Asia/Seoul",
    "仁川": "Asia/Seoul",
    "新加坡": "Asia/Singapore",
    "馬來西亞": "Asia/Kuala_Lumpur",
    "菲律賓": "Asia/Manila",
    "泰國": "Asia/Bangkok",
    "印尼": "Asia/Jakarta",
}

DEFAULT_TIMEZONE = "Asia/Taipei"


HISTORY_TABLE = "profile_history"

# 背景描述的長度上限。它每次對話都會進 prompt,而且會自動更新——
# 沒有上限的話會慢慢長成一篇小說,把記憶和脈絡的空間吃掉。
MAX_PROFILE_CHARS = 400

# 背景是**事實描述**,不是行為規則。自動更新若寫進這類句子,
# 等於讓抽取流程有機會改寫知平的行為(語氣、心理邊界),那條線不能開。
INSTRUCTION_MARKERS = (
    "你應該", "你必須", "你要", "請你", "不要說", "不准", "務必",
    "回答時", "回覆時", "語氣", "口吻",
)


async def fetch(db: SupabaseClient, line_user_id: str) -> Optional[Dict[str, Any]]:
    rows = await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "select": "line_user_id,timezone,timezone_updated_at,profile",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def is_valid_profile(text: str) -> bool:
    """背景描述是否合規:有內容、不過長、而且不是在下指令。"""
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > MAX_PROFILE_CHARS:
        return False
    return not any(marker in cleaned for marker in INSTRUCTION_MARKERS)


async def set_profile(
    db: SupabaseClient,
    line_user_id: str,
    profile: str,
    actor: str = "extract",
    before: Optional[str] = None,
) -> bool:
    """更新背景描述,並留下異動紀錄。

    背景會影響之後每一次對話,改錯了要查得出來是什麼時候、改成什麼,
    所以每次變動都寫進 profile_history。
    """
    cleaned = (profile or "").strip()
    if not is_valid_profile(cleaned):
        logger.warning("拒絕不合規的背景描述(過長或含行為指令)")
        return False

    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.upsert(
            TABLE,
            {
                "line_user_id": line_user_id,
                "profile": cleaned,
                "profile_updated_at": now,
                "updated_at": now,
            },
            on_conflict="line_user_id",
        )
        await db.insert(
            HISTORY_TABLE,
            {
                "line_user_id": line_user_id,
                "before_text": before,
                "after_text": cleaned,
                "actor": actor,
            },
            returning=False,
        )
    except SupabaseError as exc:
        logger.error("更新背景描述失敗:%s", exc)
        return False

    logger.info("背景描述已更新(%s):%s", actor, cleaned[:40])
    return True


async def set_timezone(
    db: SupabaseClient, line_user_id: str, tz_name: str
) -> bool:
    """更新使用者所在時區。時區字串不合法就拒絕寫入。"""
    if not is_valid_timezone(tz_name):
        logger.warning("拒絕寫入不合法的時區:%r", tz_name)
        return False

    now = datetime.now(timezone.utc).isoformat()
    values = {
        "line_user_id": line_user_id,
        "timezone": tz_name,
        "timezone_updated_at": now,
        "updated_at": now,
    }

    try:
        # 單筆資料,用 upsert 省掉「先查再決定 insert 或 update」的往返
        await db.upsert(TABLE, values, on_conflict="line_user_id")
    except SupabaseError as exc:
        logger.error("更新時區失敗:%s", exc)
        return False

    logger.info("使用者時區更新為 %s", tz_name)
    return True


def resolve_place(text: str) -> Optional[str]:
    """從一句話裡認出地點,回傳對應的 IANA 時區。認不出來回 None。

    比對用「最長的地名優先」,避免「台中」被「台」之類的短詞先攔截。
    """
    matches = [place for place in PLACE_TIMEZONES if place in text]
    if not matches:
        return None
    longest = max(matches, key=len)
    return PLACE_TIMEZONES[longest]


def is_valid_timezone(tz_name: str) -> bool:
    if not tz_name:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
        return True
    except Exception:
        return False


def local_time_string(tz_name: Optional[str]) -> str:
    """回傳當地時間的描述,例如「2026-08-02(週日)清晨 05:12」。

    附上「清晨」「深夜」這種詞是刻意的:模型看到 05:12 不一定會意識到那是
    什麼樣的時刻,但看到「清晨」就知道對方可能剛值完夜班。
    """
    zone = tz_name or DEFAULT_TIMEZONE
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(zone))
    except Exception:
        now = datetime.now(timezone.utc)
        zone = "UTC"

    weekday = "一二三四五六日"[now.weekday()]
    return "{}(週{}){} {}".format(
        now.strftime("%Y-%m-%d"), weekday, _period_of_day(now.hour), now.strftime("%H:%M")
    )


def _period_of_day(hour: int) -> str:
    if hour < 5:
        return "深夜"
    if hour < 8:
        return "清晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"
