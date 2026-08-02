"""從對話中認出使用者換了地方(docs/DESIGN.md 2.3)。

**為什麼不用 function calling**:規格建議由 AI 透過 function calling 更新時區。
實作時改成在呼叫 LLM 之前先用規則判斷,理由是時間預算(見 5.2):

    function calling  → 第一次呼叫拿到 tool call → 執行 → 第二次呼叫拿回覆
    規則判斷          → 0ms,而且結果穩定、測得出來

「我到越南了」這種句子的變化其實很有限,不值得為它多付一次 API 往返——
那可能就是 reply token 過不過期的差別。若日後發現規則漏掉太多說法,
再換成 function calling 也不遲。

**判斷要保守**:誤判的代價是知平把時間講錯,比漏判嚴重。所以要同時滿足
「有地名」與「有『現在人在那裡』的語氣」,而且不能有過去式的線索。
"""

import logging
import re
from typing import Optional

from app.repositories.user_state import resolve_place

logger = logging.getLogger(__name__)

# 「我現在人在那裡」的線索
PRESENCE_CUES = (
    "現在在", "現在人在", "人在", "我在", "剛到", "到了", "抵達", "靠港",
    "靠了", "停在", "來到", "落地", "下船在", "這裡是", "目前在",
)

# 過去式或未來式的線索。有這些就不算「現在人在那裡」
NON_PRESENT_CUES = (
    "上次", "上一次", "之前", "以前", "去年", "當時", "那時", "曾經",
    "待會", "等等", "下次", "明天要", "打算", "預計", "應該會", "本來",
    "想去", "沒去", "還沒到",
)


def detect_timezone_update(text: str) -> Optional[str]:
    """從一句話判斷使用者是不是換地方了,回傳新的 IANA 時區。

    認不出來就回 None——寧可漏判,不要把時間講錯。
    """
    if not text:
        return None

    if any(cue in text for cue in NON_PRESENT_CUES):
        return None

    if not any(cue in text for cue in PRESENCE_CUES):
        return None

    tz_name = resolve_place(text)
    if tz_name:
        logger.info("從對話判斷使用者所在時區為 %s", tz_name)
    return tz_name


def from_location_message(message: dict) -> Optional[str]:
    """從 LINE 的位置訊息判斷時區。

    比從句子猜地名準得多——他人就在那裡,沒有「上次」「打算去」的歧義。

    兩段式:先拿地址字串比對已知地名(這樣拿得到正確的 IANA 時區,
    夏令時間之類的規則才會對);認不出來就退回用經度估算。

    經度估算是粗的:每 15 度一個時區,不管實際的國界與夏令時間。
    但在公海上本來就沒有更好的答案,而且誤差最多一小時——
    總比用台灣時間跟一個在大西洋上的人說「早安」好。
    """
    for field in ("address", "title"):
        value = message.get(field) or ""
        if value:
            found = resolve_place(value)
            if found:
                return found

    longitude = message.get("longitude")
    if longitude is None:
        return None

    try:
        offset = int(round(float(longitude) / 15.0))
    except (TypeError, ValueError):
        return None

    offset = max(-12, min(14, offset))
    if offset == 0:
        return "UTC"
    # Etc/GMT 的正負號是相反的:Etc/GMT-8 代表 UTC+8
    return "Etc/GMT{}{}".format("-" if offset > 0 else "+", abs(offset))


def detect_from_messages(texts) -> Optional[str]:
    """從一批訊息裡找最後一次的地點更新。

    取最後一次是刻意的:「早上在高雄,現在到釜山了」應該以釜山為準。
    """
    result = None
    for text in texts:
        found = detect_timezone_update(text)
        if found:
            result = found
    return result
