"""`memories` 與 `memory_audit` 表的存取。

讀取端有兩道上限(見 docs/DESIGN.md 3.1、3.3):筆數上限 30、字數上限 1500。
兩道都是必要的——記憶會隨時間無止盡累積,但 system prompt 的長度直接換算成
每則對話的成本與延遲。上限就是「記憶膨脹處理」的第一種作法。
"""

import logging
from typing import Any, Dict, List, Optional

from app.clients.supabase import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)

TABLE = "memories"
AUDIT_TABLE = "memory_audit"

CATEGORY_PREFERENCE = "preference"
CATEGORY_CONTEXT = "context"
CATEGORY_RELATIONSHIP = "relationship"
CATEGORY_EVENT = "event"

CATEGORIES = (
    CATEGORY_PREFERENCE,
    CATEGORY_CONTEXT,
    CATEGORY_RELATIONSHIP,
    CATEGORY_EVENT,
)

# 記憶區塊的上限。超過就從 importance 低的開始捨棄。
MAX_MEMORIES = 30
MAX_MEMORY_CHARS = 1500

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DEACTIVATE = "deactivate"

ACTOR_EXTRACT = "extract"
ACTOR_USER = "user"


async def fetch_active(
    db: SupabaseClient,
    line_user_id: str,
    limit: int = MAX_MEMORIES,
    max_chars: int = MAX_MEMORY_CHARS,
) -> List[Dict[str, Any]]:
    """撈取要放進 system prompt 的記憶。

    排序:importance 降序 → updated_at 降序。也就是「重要的優先,同樣重要的看誰新」。
    先在資料庫端限制筆數,再在應用層套字數上限。
    """
    rows = await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "is_active": "is.true",
            "select": "id,category,content,importance,updated_at",
            "order": "importance.desc,updated_at.desc",
            "limit": str(limit),
        },
    )

    selected: List[Dict[str, Any]] = []
    used_chars = 0
    for row in rows:
        length = len(row.get("content") or "")
        if used_chars + length > max_chars:
            # 已排序,所以後面的只會更不重要,直接停
            logger.info("記憶區塊達字數上限,採用 %s/%s 筆", len(selected), len(rows))
            break
        selected.append(row)
        used_chars += length

    return selected


async def fetch_all_active(
    db: SupabaseClient, line_user_id: str
) -> List[Dict[str, Any]]:
    """撈取全部 active 記憶,不套上限。

    給兩個地方用:抽取流程要拿現有記憶做去重與更新判斷、
    以及使用者輸入「你記得我什麼」時要看到全部,不能只看前 30 筆。
    """
    return await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "is_active": "is.true",
            "select": "id,category,content,importance,created_at,updated_at",
            "order": "category.asc,importance.desc,updated_at.desc",
        },
    )


async def search_active(
    db: SupabaseClient, line_user_id: str, keyword: str
) -> List[Dict[str, Any]]:
    """依關鍵字找記憶,給「忘記 <關鍵字>」用。"""
    rows = await fetch_all_active(db, line_user_id)
    lowered = keyword.strip().lower()
    return [r for r in rows if lowered in (r.get("content") or "").lower()]


async def fetch_user_forgotten(
    db: SupabaseClient, line_user_id: str, limit: int = 50
) -> List[str]:
    """使用者曾經明確要求忘記的內容。

    給抽取流程當作黑名單:對話紀錄裡還留著他當初講過的話,不擋的話下一次抽取
    會把同一件事再記一次,使用者會覺得「我明明叫你忘記」。這是使用者權益問題,
    不只是資料正確性問題(見 docs/DESIGN.md 3.4)。
    """
    rows = await db.select(
        AUDIT_TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "action": "eq.{}".format(ACTION_DEACTIVATE),
            "actor": "eq.{}".format(ACTOR_USER),
            "select": "before_content",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )
    return [row["before_content"] for row in rows if row.get("before_content")]


async def create(
    db: SupabaseClient,
    line_user_id: str,
    category: str,
    content: str,
    importance: int = 3,
    source_conversation_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    row = await db.insert(
        TABLE,
        {
            "line_user_id": line_user_id,
            "category": category,
            "content": content,
            "importance": importance,
            "source_conversation_id": source_conversation_id,
        },
    )
    if row:
        await _audit(
            db,
            line_user_id,
            row.get("id"),
            ACTION_CREATE,
            after_content=content,
        )
    return row


async def update(
    db: SupabaseClient,
    line_user_id: str,
    memory_id: str,
    content: str,
    importance: Optional[int] = None,
    before_content: Optional[str] = None,
) -> None:
    values: Dict[str, Any] = {"content": content, "updated_at": "now()"}
    if importance is not None:
        values["importance"] = importance

    await db.update(
        TABLE,
        {"id": "eq.{}".format(memory_id), "line_user_id": "eq.{}".format(line_user_id)},
        values,
    )
    await _audit(
        db,
        line_user_id,
        memory_id,
        ACTION_UPDATE,
        before_content=before_content,
        after_content=content,
    )


async def deactivate(
    db: SupabaseClient,
    line_user_id: str,
    memory_ids: List[str],
    actor: str = ACTOR_EXTRACT,
) -> int:
    """軟刪除。回傳實際被停用的筆數。"""
    if not memory_ids:
        return 0

    rows = await db.update(
        TABLE,
        {
            "id": "in.({})".format(",".join(memory_ids)),
            "line_user_id": "eq.{}".format(line_user_id),
            "is_active": "is.true",
        },
        {"is_active": False, "updated_at": "now()"},
    )

    for row in rows:
        await _audit(
            db,
            line_user_id,
            row.get("id"),
            ACTION_DEACTIVATE,
            before_content=row.get("content"),
            actor=actor,
        )
    return len(rows)


async def deactivate_all(db: SupabaseClient, line_user_id: str) -> int:
    """停用某使用者的全部記憶(「忘記全部」)。對話紀錄保留。"""
    rows = await db.update(
        TABLE,
        {"line_user_id": "eq.{}".format(line_user_id), "is_active": "is.true"},
        {"is_active": False, "updated_at": "now()"},
    )
    for row in rows:
        await _audit(
            db,
            line_user_id,
            row.get("id"),
            ACTION_DEACTIVATE,
            before_content=row.get("content"),
            actor=ACTOR_USER,
        )
    return len(rows)


async def _audit(
    db: SupabaseClient,
    line_user_id: str,
    memory_id: Optional[str],
    action: str,
    before_content: Optional[str] = None,
    after_content: Optional[str] = None,
    actor: str = ACTOR_EXTRACT,
) -> None:
    """寫異動紀錄。失敗不影響主流程——記憶本身已經改好了。"""
    try:
        await db.insert(
            AUDIT_TABLE,
            {
                "memory_id": memory_id,
                "line_user_id": line_user_id,
                "action": action,
                "before_content": before_content,
                "after_content": after_content,
                "actor": actor,
            },
            returning=False,
        )
    except SupabaseError as exc:
        logger.warning("寫入 memory_audit 失敗:%s", exc)
