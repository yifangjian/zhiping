"""記憶抽取流程(docs/DESIGN.md 3.2)——本專案的技術核心之一。

**為什麼要有這個流程**:直接把整段對話史塞進 prompt 不可行,長度會無止盡成長。
抽取的意義是把「三十則閒聊」壓縮成「他在學排氣閥」這樣一句話,長期成本才不會爆炸。

**為什麼跑在背景**:抽取是第二次 LLM 呼叫。若放在回覆路徑上,使用者每則訊息
都要多等好幾秒,reply token 直接過期——那會把免費額度燒光(見第 5 節)。
所以它在回覆送出**之後**才跑,慢一點沒關係。

**為什麼要帶著現有記憶去問**:不帶的話,模型只會不斷產生新的相似記憶
(「他喜歡吃泡麵」「他愛吃泡麵」「泡麵是他的最愛」)。帶了 id 過去,它才能回答
「更新第 3 條」而不是「新增第 4 條」。矛盾的處理也是同理:處境會變,
「正在煩惱考試」在考完之後應該被停用,而不是永遠留著。

**觸發門檻**:累積 6 則以上才跑,避免每則訊息都多打一次 API。
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from app.prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_input
from app.repositories import conversations, memories, usage_log, user_state

if TYPE_CHECKING:  # 只為了型別註解,避免循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)

# 累積達這個則數才跑一次抽取(見 docs/DESIGN.md 第 4 節的成本控制)
EXTRACTION_THRESHOLD = 6

# 同一個使用者的抽取不要併發:兩次抽取看到同一批對話,會產生重複記憶。
# 行程內狀態,理由與 inbox.py 的等待窗相同(單一使用者、單一 instance)。
_locks: Dict[str, asyncio.Lock] = {}


async def maybe_extract(runtime: "Runtime", line_user_id: str) -> None:
    """若累積的對話夠多就跑一次抽取。這個函式不會往外丟例外。"""
    lock = _locks.setdefault(line_user_id, asyncio.Lock())
    if lock.locked():
        logger.info("上一次抽取還在跑,這次略過")
        return

    async with lock:
        try:
            await _extract(runtime, line_user_id)
        except Exception:  # noqa: BLE001 — 背景任務,沒有人接得住
            logger.exception("記憶抽取失敗")


async def _extract(runtime: "Runtime", line_user_id: str) -> None:
    pending = await conversations.count_unextracted(runtime.db, line_user_id)
    if pending < EXTRACTION_THRESHOLD:
        logger.debug("待抽取對話 %s 則,未達門檻 %s", pending, EXTRACTION_THRESHOLD)
        return

    # 每日抽取次數上限(見 docs/DESIGN.md 第 4 節)。超過就把對話留著,
    # 明天再處理——對話不會消失,extracted 還是 false,記憶晚一天到不影響體驗。
    used = await usage_log.count_today(runtime.db, usage_log.KIND_EXTRACT)
    if used is not None and used >= runtime.settings.daily_extract_limit:
        logger.warning(
            "今日抽取已達上限 %s 次,%s 則對話延到明天處理",
            runtime.settings.daily_extract_limit,
            pending,
        )
        return

    rows = await conversations.fetch_unextracted(runtime.db, line_user_id)
    if not rows:
        return

    existing = await memories.fetch_all_active(runtime.db, line_user_id)
    state = await user_state.fetch(runtime.db, line_user_id)
    current_profile = (state or {}).get("profile")
    # 使用者刪掉過的東西不能再被抽回來(見 memories.fetch_user_forgotten)
    forgotten = await memories.fetch_user_forgotten(runtime.db, line_user_id)

    lines = [
        "{}:{}".format(
            "使用者" if row.get("role") == conversations.ROLE_USER else "知平",
            row.get("content") or "",
        )
        for row in rows
    ]

    result = await runtime.openai.chat(
        [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_extraction_input(
                    lines, existing, forgotten, current_profile
                ),
            },
        ],
        model=runtime.settings.openai_extract_model,
        max_tokens=runtime.settings.openai_extract_max_tokens,
        json_mode=True,
        # 結構化任務,不需要創造力
        temperature=0.2,
    )

    await usage_log.record(
        runtime.db,
        usage_log.KIND_EXTRACT,
        line_user_id=line_user_id,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )

    payload = parse_extraction(result.text)
    if payload is None:
        # 解析失敗就不標記已處理,下次連同新對話一起再試一次
        logger.warning("抽取結果不是合法 JSON,保留這批對話待下次重試")
        return

    applied = await _apply(
        runtime, line_user_id, payload, existing, rows, current_profile
    )

    marked = await conversations.mark_extracted(
        runtime.db, [row["id"] for row in rows if row.get("id")]
    )
    logger.info(
        "記憶抽取完成:處理 %s 則對話,新增 %s、更新 %s、停用 %s、背景更新 %s",
        marked,
        applied["created"],
        applied["updated"],
        applied["deactivated"],
        applied["profile_changed"],
    )


async def _apply(
    runtime: "Runtime",
    line_user_id: str,
    payload: Dict[str, Any],
    existing: List[Dict[str, Any]],
    source_rows: List[Dict[str, Any]],
    current_profile: Optional[str] = None,
) -> Dict[str, Any]:
    """把抽取結果寫進資料庫。"""
    by_id = {memory.get("id"): memory for memory in existing}
    # 記憶來自這批對話,用最後一則當來源,方便事後追溯
    source_id = source_rows[-1].get("id") if source_rows else None

    created = 0
    for item in payload["new_memories"]:
        row = await memories.create(
            runtime.db,
            line_user_id=line_user_id,
            category=item["category"],
            content=item["content"],
            importance=item["importance"],
            source_conversation_id=source_id,
        )
        if row:
            created += 1

    updated = 0
    for item in payload["updates"]:
        before = by_id.get(item["id"])
        if before is None:
            # 模型有時會回傳不存在的 id,忽略比寫錯資料好
            logger.warning("抽取結果要更新不存在的記憶 %s,略過", item["id"])
            continue
        await memories.update(
            runtime.db,
            line_user_id=line_user_id,
            memory_id=item["id"],
            content=item["content"],
            importance=item.get("importance"),
            before_content=before.get("content"),
        )
        updated += 1

    # 背景描述只有在他的根本處境改變時才會被回傳,而且會整個取代舊的。
    # 這段內容會影響之後每一次對話,所以寫入前再驗一次是不是合規
    # (長度、有沒有混進行為規則),並留下異動紀錄。
    profile_changed = False
    new_profile = payload.get("profile")
    if new_profile and new_profile != current_profile:
        profile_changed = await user_state.set_profile(
            runtime.db,
            line_user_id,
            new_profile,
            actor="extract",
            before=current_profile,
        )

    valid_ids = [mid for mid in payload["deactivate"] if mid in by_id]
    deactivated = await memories.deactivate(
        runtime.db, line_user_id, valid_ids, actor=memories.ACTOR_EXTRACT
    )

    return {
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
        "profile_changed": profile_changed,
    }


def parse_extraction(raw: str) -> Optional[Dict[str, Any]]:
    """解析並清洗模型回傳的 JSON。

    模型的輸出不可信任,任何一項不合規就丟掉那一項,而不是讓整批失敗:
    - category 不在四種之內
    - importance 不是 1–5 的整數
    - content 是空的
    """
    text = (raw or "").strip()
    # 即使要求只輸出 JSON,模型偶爾還是會包上 ```json 圍欄
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    return {
        "new_memories": _clean_new(data.get("new_memories")),
        "updates": _clean_updates(data.get("updates")),
        "deactivate": _clean_ids(data.get("deactivate")),
        "profile": _clean_profile(data.get("profile")),
    }


def _clean_new(items: Any) -> List[Dict[str, Any]]:
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        content = (item.get("content") or "").strip()
        if category not in memories.CATEGORIES or not content:
            logger.warning("抽取結果有不合規的新記憶,略過:%r", item)
            continue
        cleaned.append(
            {
                "category": category,
                "content": content,
                "importance": _clean_importance(item.get("importance")),
            }
        )
    return cleaned


def _clean_updates(items: Any) -> List[Dict[str, Any]]:
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        memory_id = item.get("id")
        content = (item.get("content") or "").strip()
        if not memory_id or not content:
            continue
        cleaned.append(
            {
                "id": str(memory_id),
                "content": content,
                "importance": _clean_importance(item.get("importance")),
            }
        )
    return cleaned


def _clean_ids(items: Any) -> List[str]:
    seen: Set[str] = set()
    result = []
    for item in items or []:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _clean_profile(value: Any) -> Optional[str]:
    """背景描述的第一道清洗。真正的驗證在 user_state.set_profile,
    這裡只擋掉明顯不是字串或空白的情況。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_importance(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return min(5, max(1, number))
