"""`conversations` 表的存取。

這張表扮演三個角色:
1. 對話逐字紀錄 —— 短期脈絡從這裡撈(見 docs/DESIGN.md 3.5)。
2. webhook 事件去重的依據 —— `webhook_event_id` 上有 unique index(5.4)。
3. 記憶抽取的來源 —— `extracted` 標記哪些還沒被處理過(3.2)。

去重刻意交給資料庫的 unique 限制,而不是「先查再寫」:先查再寫在併發下有 race
condition(兩個重送幾乎同時進來,兩邊都查不到、兩邊都寫入),unique index 沒有
這個問題。
"""

import logging
from typing import Any, Dict, List, Optional

from app.clients.supabase import DuplicateRecordError, SupabaseClient

logger = logging.getLogger(__name__)

TABLE = "conversations"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# 短期脈絡的上限(見 docs/DESIGN.md 3.5)。與記憶區塊一樣,上限是成本與延遲的閘門。
MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 3000

# 抽取流程一次最多處理幾則,避免長時間斷線後累積太多、一次塞爆 prompt
MAX_EXTRACTION_BATCH = 40


async def save_message(
    db: SupabaseClient,
    line_user_id: str,
    role: str,
    content: str,
    webhook_event_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """寫入一則訊息。

    回傳 None 代表這個 webhook 事件已經處理過(重送),呼叫端應該直接放棄這次處理,
    否則使用者會收到重複回覆。
    """
    row = {
        "line_user_id": line_user_id,
        "role": role,
        "content": content,
    }
    if webhook_event_id:
        row["webhook_event_id"] = webhook_event_id

    try:
        return await db.insert(TABLE, row)
    except DuplicateRecordError:
        logger.info("webhook 事件 %s 已處理過,略過", webhook_event_id)
        return None


async def fetch_context(
    db: SupabaseClient,
    line_user_id: str,
    before: Optional[str] = None,
    limit: int = MAX_CONTEXT_MESSAGES,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> List[Dict[str, Any]]:
    """撈取短期對話脈絡,回傳依時間**正序**排列(舊 → 新)。

    `before` 傳入本次訊息的 created_at,把本次訊息排除在外——它已經寫進資料庫了,
    但要以「本次使用者訊息」的身分放在 messages 陣列最後,不能在脈絡裡出現兩次。

    資料庫端用 created_at 降序取前 N 筆(有索引、只掃需要的列),再在應用層反轉。
    字數超過上限時從**最舊的**開始捨棄——越近的對話對「那個」「剛剛講的」越關鍵。
    """
    params = {
        "line_user_id": "eq.{}".format(line_user_id),
        # 使用者要求忘記的內容不再進入脈絡(見 hide_containing)
        "hidden": "is.false",
        "select": "role,content,created_at",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if before:
        params["created_at"] = "lt.{}".format(before)

    rows = await db.select(TABLE, params)

    # rows 目前是新 → 舊。從最新的開始納入,額滿即止,最後再反轉成正序。
    selected: List[Dict[str, Any]] = []
    used_chars = 0
    for row in rows:
        length = len(row.get("content") or "")
        if used_chars + length > max_chars:
            break
        selected.append(row)
        used_chars += length

    return list(reversed(selected))


async def count_unextracted(db: SupabaseClient, line_user_id: str) -> int:
    """還沒被記憶抽取處理過的對話則數。"""
    rows = await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "extracted": "is.false",
            "select": "id",
        },
    )
    return len(rows)


async def fetch_unextracted(
    db: SupabaseClient, line_user_id: str, limit: int = MAX_EXTRACTION_BATCH
) -> List[Dict[str, Any]]:
    """撈取待抽取的對話,依時間正序(抽取要照著對話發生的順序讀)。"""
    return await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "extracted": "is.false",
            "select": "id,role,content,created_at",
            "order": "created_at.asc",
            "limit": str(limit),
        },
    )


async def hide_containing(
    db: SupabaseClient, line_user_id: str, keywords: List[str]
) -> int:
    """遮蔽含有這些關鍵字的對話,讓它們不再進入短期脈絡。

    使用者說「忘記荷包蛋」時,他要的是知平再也講不出這件事。只停用 memories 不夠——
    他當初講那句話的原始對話還在最近 20 則裡,知平照樣讀得到。

    刻意不做實體刪除:規格 3.4 要求對話紀錄保留,而且誤遮蔽時還救得回來。
    """
    hidden = 0
    for keyword in keywords:
        if len(keyword) < 2:
            # 單字關鍵字會誤傷太多對話,寧可漏
            continue
        rows = await db.update(
            TABLE,
            {
                "line_user_id": "eq.{}".format(line_user_id),
                "content": "ilike.*{}*".format(keyword),
                "hidden": "is.false",
            },
            {"hidden": True},
        )
        hidden += len(rows)
    return hidden


async def mark_extracted(db: SupabaseClient, conversation_ids: List[str]) -> int:
    """標記這些對話已被抽取流程處理過,避免下次重複處理。"""
    if not conversation_ids:
        return 0

    rows = await db.update(
        TABLE,
        {"id": "in.({})".format(",".join(conversation_ids))},
        {"extracted": True},
    )
    return len(rows)
