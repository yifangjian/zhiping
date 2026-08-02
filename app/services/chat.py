"""產生並送出一次回覆。

輸入是「等待窗結束後的一批訊息」(見 app/services/inbox.py),不是單一則訊息。
訊息已經在進等待窗之前就寫進資料庫了,這裡只負責產生回覆、送出、記錄。

**組裝順序(見 docs/DESIGN.md 3.5)**:

    system prompt(角色設定 + 記憶區塊)
      ↓
    最近 20 則對話
      ↓
    本次使用者訊息

長期記憶與短期脈絡是兩件事,少了任何一邊都會壞:
只有記憶沒有脈絡,使用者說「那個很難」時知平不知道「那個」是什麼;
只有脈絡沒有記憶,他每隔幾天就變回一個陌生人。

**時間預算(見 5.2、5.3)**:

    最後一則訊息抵達
      → 等待窗 3s
      → 撈記憶與脈絡(並行,約 0.3s)+ 產生回覆 最多 9s
      → 送出 reply
    ────────────────────
    從最後一則訊息算起約 12s

reply token 的有效期以秒計且 LINE 不公布確切秒數,所以這是一場賭博:賭得越久
reply 越可能失效、越可能得動用要付費的 push。因此 timeout 不是「保險」,是預算上限。
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.clients.openai_client import ChatResult
from app.prompts import (
    CONTEXT_GAP_HOURS,
    DAILY_LIMIT_REPLY,
    FALLBACK_REPLY,
    build_system_prompt,
    format_context_notice,
    format_memory_block,
)
from app.repositories import conversations, memories, usage_log, user_state
from app.services import (
    attachments,
    location,
    memory_extraction,
    messenger,
    notifier,
)
from app.services.formatting import sanitize_for_line
from app.timeutils import hours_since

if TYPE_CHECKING:  # 只為了型別註解,避免循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)


async def handle_batch(runtime: "Runtime", batch: List[Dict[str, Any]]) -> None:
    """處理一批已寫入資料庫的訊息,產生並送出一則回覆。"""
    line_user_id = batch[0]["line_user_id"]
    # 用最後一則的 replyToken:它最晚產生,存活機率最高(見 docs/DESIGN.md 5.5)
    reply_token = batch[-1]["reply_token"]
    user_text = merge_texts(batch)

    # 記憶指令走在一般對話前面,不經過 LLM(見 app/services/commands.py)
    command_reply = await _try_command(runtime, line_user_id, user_text)
    if command_reply is not None:
        await _handle_command_reply(runtime, batch, reply_token, command_reply)
        return

    # 驗收用(docs/DESIGN.md 5.7 第一項):設 DEBUG_REPLY_DELAY_SECONDS=30 可以模擬
    # 後端變慢,實測 reply token 過期後有沒有正確 fallback 到 push。正式環境保持 0。
    if runtime.settings.debug_reply_delay:
        logger.warning("DEBUG:人為延遲 %.0f 秒", runtime.settings.debug_reply_delay)
        await asyncio.sleep(runtime.settings.debug_reply_delay)

    # 使用者換地方了就先更新時區,再組 prompt——否則這一輪還是用舊時區判斷
    # 「早上」「晚上」(見 app/services/location.py)
    await _maybe_update_timezone(runtime, line_user_id, batch)

    # 每日回覆上限。單一使用者正常用不到,這是「出事時止血」——
    # 例如某個迴圈壞掉導致連環對話(見 docs/DESIGN.md 第 4 節)。
    if await _daily_limit_reached(runtime):
        await messenger.deliver(
            runtime, line_user_id, reply_token, DAILY_LIMIT_REPLY
        )
        return

    reply_text = await _generate_reply_safely(runtime, batch)
    delivered = await _deliver_and_record(
        runtime, line_user_id, reply_token, reply_text
    )
    if not delivered:
        return

    # 回覆已經送出去了,抽取慢一點沒關係——它絕不能擋在回覆路徑上(見 3.2)
    await memory_extraction.maybe_extract(runtime, line_user_id)


async def _try_command(
    runtime: "Runtime", line_user_id: str, text: str
) -> Optional[str]:
    try:
        return await runtime.commands.handle(runtime, line_user_id, text)
    except Exception:  # noqa: BLE001 — 指令壞掉不該讓整段對話沉默
        logger.exception("處理記憶指令時發生錯誤,改走一般對話")
        return None


async def _maybe_update_timezone(
    runtime: "Runtime", line_user_id: str, batch: List[Dict[str, Any]]
) -> None:
    """使用者換地方了就更新時區。失敗不影響回覆。

    兩個來源:傳位置訊息(準),或在對話裡提到(要猜)。位置訊息優先。
    """
    tz_name = None
    for message in batch:
        if message.get("kind") == "location":
            tz_name = location.from_location_message(message) or tz_name

    if tz_name is None:
        tz_name = location.detect_from_messages(
            m.get("text") or "" for m in batch
        )

    if not tz_name:
        return
    try:
        await user_state.set_timezone(runtime.db, line_user_id, tz_name)
    except Exception:  # noqa: BLE001
        logger.exception("更新時區失敗,這輪先用舊的")


async def _daily_limit_reached(runtime: "Runtime") -> bool:
    """今天的對話回覆是否已達上限。

    查不到用量時放行:讓使用者收不到回覆的代價,大於多回幾則的代價。
    這與 messenger 的 push 額度判斷是同一個取捨。
    """
    used = await usage_log.count_today(runtime.db, usage_log.KIND_CHAT)
    if used is None:
        return False

    limit = runtime.settings.daily_chat_limit
    if used < limit:
        return False

    logger.error("今日對話回覆已達上限 %s 則", limit)
    await notifier.notify_owner(
        runtime,
        notifier.KIND_UNEXPECTED,
        "今日對話已達上限 {} 則,後續訊息會被擋下。正常用量不該碰到這條線。".format(limit),
    )
    return True


async def _handle_command_reply(
    runtime: "Runtime",
    batch: List[Dict[str, Any]],
    reply_token: str,
    text: str,
) -> None:
    """送出記憶指令的回應。

    **指令往返不算對話內容**,所以這裡刻意不做兩件事:

    1. 不把回應寫進 conversations。「你記得我什麼」的回應是一份完整的記憶清單,
       若存進對話紀錄,下一次抽取就會把整份清單重新讀成新記憶——記憶會自我複製,
       連使用者剛刪掉的也會復活。
    2. 把使用者的指令本身標記為已抽取,理由相同:「忘記荷包蛋」這句話裡
       同樣帶著他想刪掉的內容。
    """
    await messenger.deliver(runtime, batch[0]["line_user_id"], reply_token, text)

    ids = [m["conversation_id"] for m in batch if m.get("conversation_id")]
    if not ids:
        return
    try:
        await conversations.mark_extracted(runtime.db, ids)
    except Exception:  # noqa: BLE001
        logger.exception("標記指令訊息為已抽取失敗")


async def _deliver_and_record(
    runtime: "Runtime", line_user_id: str, reply_token: str, text: str
) -> bool:
    delivered = await messenger.deliver(runtime, line_user_id, reply_token, text)
    if not delivered:
        # 送不出去就不寫進對話紀錄。使用者沒看到的話不算他們的對話——
        # 若留在紀錄裡,下次組短期脈絡時知平會以為自己講過了,
        # 開始引用對方根本沒讀到的內容。
        logger.error("回覆沒有送達,不寫入對話紀錄")
        return False

    # 使用者已經收到回覆,後面失敗只影響紀錄,不再打擾他
    try:
        await conversations.save_message(
            runtime.db,
            line_user_id=line_user_id,
            role=conversations.ROLE_ASSISTANT,
            content=text,
        )
    except Exception:  # noqa: BLE001
        logger.exception("寫入知平的回覆失敗")
    return True


async def _generate_reply_safely(
    runtime: "Runtime", batch: List[Dict[str, Any]]
) -> str:
    """產生回覆,並確保無論如何都有話可說。

    使用者面對沉默,比面對一句「剛剛沒接上」更糟(見 docs/DESIGN.md 5.6.3)。
    """
    timeout = runtime.settings.generation_timeout
    try:
        result = await asyncio.wait_for(
            _generate_reply(runtime, batch), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning("產生回覆超過 %.0f 秒,改送備援回覆", timeout)
        # 逾時不算 API 壞掉,可能只是這次比較慢,不計入連續失敗
        return FALLBACK_REPLY
    except Exception as exc:  # noqa: BLE001
        logger.exception("產生回覆時發生未預期的錯誤")
        # 連續失敗才通知:單次失敗在弱網路下很常見,重試就好(見 8.1)
        await notifier.notify_if_repeated(
            runtime, notifier.KIND_OPENAI_FAILURE, "{}: {}".format(
                type(exc).__name__, str(exc)[:200]
            )
        )
        return FALLBACK_REPLY

    notifier.record_success(notifier.KIND_OPENAI_FAILURE)

    # LINE 不渲染 Markdown,搜尋工具又會插滿引用連結——這一層是程式保證,
    # 不是靠模型自律(見 app/services/formatting.py)
    text = sanitize_for_line(result.text)
    if not text:
        logger.warning("LLM 回傳空內容,改送備援回覆")
        return FALLBACK_REPLY

    line_user_id = batch[0]["line_user_id"]
    await usage_log.record(
        runtime.db,
        usage_log.KIND_CHAT,
        line_user_id=line_user_id,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    if result.used_search:
        # 單獨記一筆才數得出「這個月查了幾次」。token 已經算在上面那筆裡了,
        # 這裡只是計次(見 docs/DESIGN.md 第 4 節)。
        await usage_log.record(
            runtime.db,
            usage_log.KIND_SEARCH,
            line_user_id=line_user_id,
            model=result.model,
        )
    return text


async def _generate_reply(
    runtime: "Runtime", batch: List[Dict[str, Any]]
) -> ChatResult:
    """組出 messages 並呼叫 LLM。"""
    line_user_id = batch[0]["line_user_id"]

    # 四件事彼此獨立,並行做完省下大量等待時間——附件下載與解析尤其慢
    memory_rows, history, state, user_content = await asyncio.gather(
        memories.fetch_active(runtime.db, line_user_id),
        conversations.fetch_context(
            runtime.db, line_user_id, before=batch[0].get("created_at")
        ),
        user_state.fetch(runtime.db, line_user_id),
        build_user_content(runtime, batch),
    )

    tz_name = (state or {}).get("timezone")

    # 記憶放在 system prompt(這裡是 Responses API 的 instructions),
    # 不放進 user message,避免使用者的訊息被記憶內容干擾(見 docs/DESIGN.md 3.1)
    instructions = build_system_prompt(
        local_time=user_state.local_time_string(tz_name),
        timezone=tz_name,
        memory_block=format_memory_block(memory_rows),
        context_notice=context_notice(history),
    )
    messages = build_context_messages(history)
    messages.append({"role": "user", "content": user_content})

    logger.info(
        "組出 messages:記憶 %s 條、脈絡 %s 則", len(memory_rows), len(history)
    )
    return await runtime.openai.converse(instructions=instructions, messages=messages)


def build_context_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把歷史對話轉成標準的 messages 格式(只有 user 與 assistant)。"""
    messages: List[Dict[str, Any]] = []
    for row in history:
        role = row.get("role")
        if role not in (conversations.ROLE_USER, conversations.ROLE_ASSISTANT):
            continue
        messages.append({"role": role, "content": row.get("content") or ""})
    return messages


def context_notice(history: List[Dict[str, Any]]) -> Optional[str]:
    """距離上一則對話很久的話,回傳一句要放進 system prompt 的標註。

    沒有這行,知平會把隔了三天的對話當成剛剛才講的,回起話來很怪
    (見 docs/DESIGN.md 3.5 第 3 點)。放在 system prompt 而不是插進 messages 陣列,
    是因為它是「關於這段對話的說明」,不是對話本身。
    """
    if not history:
        return None
    gap_hours = hours_since(history[-1].get("created_at"))
    if gap_hours is None or gap_hours < CONTEXT_GAP_HOURS:
        return None
    return format_context_notice(gap_hours)


def merge_texts(batch: List[Dict[str, Any]]) -> str:
    """把等待窗內的多則文字訊息合併成一段。

    用換行接起來,保留他分次送出的節奏——那本身就是語氣的一部分。
    刻意不加「訊息 1:」之類的標記,那會讓 LLM 用條列的方式回應。

    非文字訊息不在這裡處理,見 build_user_content。
    """
    return "\n".join(
        m.get("text") or "" for m in batch if m.get("kind", "text") == "text"
    ).strip()


async def build_user_content(
    runtime: "Runtime", batch: List[Dict[str, Any]]
) -> Any:
    """把這一批訊息組成模型的 user content。

    文字直接進去;圖片以 data URL 交給模型看;PDF / Word 抽出文字後
    當成一段附註;其餘型別(貼圖、語音、影片)寫成一句說明,讓模型自己
    用它的語氣回應——**絕不沉默**,那是最糟的處理方式。

    附件下載與解析都在這裡完成,而且各附件並行處理,不要一個一個等。
    """
    text_parts = [merge_texts(batch)] if merge_texts(batch) else []
    image_urls: List[str] = []

    attachment_messages = [
        m for m in batch if m.get("kind", "text") != "text"
    ]
    if attachment_messages:
        results = await asyncio.gather(
            *(_prepare_attachment(runtime, m) for m in attachment_messages),
            return_exceptions=True,
        )
        for message, result in zip(attachment_messages, results):
            if isinstance(result, Exception):
                logger.exception(
                    "處理附件失敗:%s", message.get("kind"), exc_info=result
                )
                text_parts.append("(他傳了一個東西,但你這邊打不開)")
                continue
            note, image_url = result
            if note:
                text_parts.append(note)
            if image_url:
                image_urls.append(image_url)

    text = "\n".join(part for part in text_parts if part).strip()

    if not image_urls:
        return text or "(他傳了訊息,但沒有文字內容)"

    # Responses API 的多模態格式。只有真的有圖片時才用這種形狀,
    # 純文字保持字串,讓歷史訊息與本次訊息的格式一致。
    content: List[Dict[str, Any]] = []
    if text:
        content.append({"type": "input_text", "text": text})
    for url in image_urls:
        content.append({"type": "input_image", "image_url": url})
    return content


async def _prepare_attachment(
    runtime: "Runtime", message: Dict[str, Any]
) -> tuple:
    """處理單一附件,回傳 (要加進 prompt 的說明, 圖片 data URL)。"""
    kind = message.get("kind")

    if kind == "location":
        place = message.get("title") or message.get("address") or "某個地方"
        return "(他傳了一個位置:{})".format(place), None

    if kind == "sticker":
        return "(他傳了一個貼圖,你看不到是哪一個,但那通常代表一種情緒)", None

    if kind == "audio":
        return "(他傳了一段語音。你聽不到內容,請他打字或直接接話)", None

    if kind == "video":
        return "(他傳了一段影片。你看不到內容,請他描述一下)", None

    message_id = message.get("message_id")
    if not message_id:
        return "(他傳了一個東西,但取不到內容)", None

    data = await runtime.line.get_content(message_id)
    if data is None:
        return (
            "(他傳了一個檔案,但太大或下載失敗。請他改用文字說明或拍照)",
            None,
        )

    if kind == "image":
        # LINE 的圖片訊息一律是 JPEG
        return None, attachments.to_data_url(data, "image/jpeg")

    if kind == "file":
        file_name = message.get("file_name") or "檔案"
        text, note = attachments.extract_document_text(data, file_name)
        if text is None:
            return "(他傳了檔案「{}」,但你讀不了。原因:{})".format(
                file_name, note
            ), None
        header = "(他傳了檔案「{}」,內容如下。{})".format(file_name, note).replace(
            ",)", ")"
        )
        return "{}\n---\n{}\n---".format(header, text), None

    return "(他傳了 {},你處理不了)".format(kind), None


