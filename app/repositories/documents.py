"""`documents` 表的存取。

存的是抽出來的文字,不是原始檔案(理由見 migrations/008_documents.sql)。

**取用策略**:不是每次對話都把檔案內容塞進 prompt——那會讓每則訊息都多付
幾千個 token。只有在他這句話看起來是在問某份檔案時才撈,判斷方式與
「忘記 <關鍵字>」相同:先看有沒有指涉檔案的詞,再比對檔名。
"""

import logging
from typing import Any, Dict, List, Optional

from app.clients.supabase import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)

TABLE = "documents"

# 撈候選檔案時看最近幾份。同時在討論十份以上文件的情況不存在。
RECENT_LIMIT = 10

# 這些詞代表他可能在講某份檔案
REFERENCE_WORDS = (
    # 直接講到檔案
    "檔案", "檔", "文件", "word", "Word", "WORD", "pdf", "PDF", "docx",
    "那份", "這份", "那篇", "這篇", "剛剛傳", "我傳", "上次傳", "傳給你",
    "報告", "作業", "手冊", "說明書",
    # 對文件做的事
    "大綱", "摘要", "總結", "重點", "錯字", "潤稿", "改一下", "校對",
    "段落", "開頭", "結尾", "裡面寫", "裡面說",
)


async def create(
    db: SupabaseClient,
    line_user_id: str,
    file_name: str,
    content: str,
    source_conversation_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        return await db.insert(
            TABLE,
            {
                "line_user_id": line_user_id,
                "file_name": file_name,
                "content": content,
                "char_count": len(content),
                "source_conversation_id": source_conversation_id,
            },
        )
    except SupabaseError as exc:
        logger.error("寫入檔案內容失敗:%s", exc)
        return None


async def fetch_recent(
    db: SupabaseClient, line_user_id: str, limit: int = RECENT_LIMIT
) -> List[Dict[str, Any]]:
    return await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "is_active": "is.true",
            "select": "id,file_name,content,char_count,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


async def list_names(
    db: SupabaseClient, line_user_id: str
) -> List[Dict[str, Any]]:
    """給「你有我哪些檔案」用,不撈內容以免傳一堆用不到的資料。"""
    return await db.select(
        TABLE,
        {
            "line_user_id": "eq.{}".format(line_user_id),
            "is_active": "is.true",
            "select": "id,file_name,char_count,created_at",
            "order": "created_at.desc",
        },
    )


async def deactivate(
    db: SupabaseClient, line_user_id: str, document_ids: List[str]
) -> int:
    if not document_ids:
        return 0
    rows = await db.update(
        TABLE,
        {
            "id": "in.({})".format(",".join(document_ids)),
            "line_user_id": "eq.{}".format(line_user_id),
            "is_active": "is.true",
        },
        {"is_active": False},
    )
    return len(rows)


async def deactivate_all(db: SupabaseClient, line_user_id: str) -> int:
    rows = await db.update(
        TABLE,
        {"line_user_id": "eq.{}".format(line_user_id), "is_active": "is.true"},
        {"is_active": False},
    )
    return len(rows)


def mentions_document(text: str) -> bool:
    """這句話看起來是不是在問某份檔案。

    只是初篩,用來決定要不要多做一次查詢——不中的話就省下那次往返。
    """
    return any(word in (text or "") for word in REFERENCE_WORDS)


def pick_referenced(
    text: str, rows: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """從候選檔案裡挑出他在講的那份。

    先比對檔名(去掉副檔名):他說「來釣鯽魚吧那份」時,檔名裡的字會出現在句子裡。
    比對不到就用最近的一份——他說「那份檔案」時,通常就是指最後傳的那個。
    """
    if not rows:
        return None

    best = None
    best_length = 0
    for row in rows:
        stem = (row.get("file_name") or "").rsplit(".", 1)[0]
        if len(stem) < 2:
            continue
        # 檔名整個出現在句子裡,或句子裡有夠長的一段檔名
        for size in range(len(stem), 1, -1):
            if size <= best_length:
                break
            if any(stem[i : i + size] in text for i in range(len(stem) - size + 1)):
                best, best_length = row, size
                break

    return best or rows[0]
