"""LINE Messaging API 用戶端。

刻意直接用 httpx 打 REST API,而不是用 line-bot-sdk:

1. 本專案的成敗取決於「回應要夠快」(見 docs/DESIGN.md 第 5 節),
   自己發 request 才能精確控制每一次呼叫的 timeout。
2. 我們只需要三個端點(reply / push / loading),SDK 的其餘部分用不到。
3. 驗簽只是 HMAC-SHA256,自己寫反而更好讀、也好測試。
"""

import base64
import hashlib
import hmac
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"

# LINE 單則文字訊息的長度上限
MAX_TEXT_LENGTH = 5000

# LINE Developers Console 按下「Verify」時,會送一個 replyToken 全為 0 的假事件。
# 對它呼叫 reply API 會失敗,直接略過即可。
VERIFY_REPLY_TOKEN = "0" * 32

# loading 動畫的顯示秒數。LINE 規定必須是 5 的倍數、最多 60。
# 抓 20 秒:比整個回覆流程的時間預算(約 12 秒)寬一點,又不會在失敗時卡太久。
LOADING_SECONDS = 20


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """驗證 webhook 的 x-line-signature。

    LINE 的簽章 = base64(HMAC-SHA256(channel_secret, raw_request_body))。
    必須用「原始 bytes」計算,不能用 parse 過的 JSON 重新序列化,否則空白與
    欄位順序的差異會導致驗簽失敗。
    """
    if not channel_secret or not signature:
        return False

    digest = hmac.new(
        channel_secret.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")

    # 用 compare_digest 避免時序攻擊
    return hmac.compare_digest(expected, signature)


class LineClient:
    """負責把訊息送回 LINE。

    reply 與 push 的差別攸關成本(見 docs/DESIGN.md 5.1):
    reply 免費但 token 只能用一次且數秒內過期;push 無時效但計入每月 200 則額度。
    因此所有方法都回傳成功與否,讓上層能做 reply 失敗 → push 備援的決策(Phase 2)。
    """

    def __init__(self, access_token: str, timeout: float = 3.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=LINE_API_BASE,
            headers={
                "Authorization": "Bearer {}".format(access_token),
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def reply(self, reply_token: str, text: str) -> bool:
        """用 webhook 帶回的 replyToken 回覆。免費,但 token 過期就失敗。"""
        if reply_token == VERIFY_REPLY_TOKEN:
            logger.info("略過 LINE console 的 webhook 驗證事件")
            return True

        return await self._post(
            "/message/reply",
            {"replyToken": reply_token, "messages": [_text_message(text)]},
            action="reply",
        )

    async def push(self, line_user_id: str, text: str) -> bool:
        """主動推送訊息。會計入每月免費額度,只在 reply 失敗時當備援用。"""
        return await self._post(
            "/message/push",
            {"to": line_user_id, "messages": [_text_message(text)]},
            action="push",
        )

    async def show_loading(
        self, line_user_id: str, seconds: int = LOADING_SECONDS
    ) -> bool:
        """在對話框顯示「輸入中」動畫。

        這是弱網路體驗的關鍵一環:使用者送出訊息後可能要等十秒才有回覆,
        沒有這個動畫他會以為訊息根本沒送出去(見 docs/DESIGN.md 5.2)。
        不計入訊息額度,所以可以放心呼叫。

        動畫會在收到回覆或時間到時自動消失。
        """
        return await self._post(
            "/chat/loading/start",
            {"chatId": line_user_id, "loadingSeconds": seconds},
            action="loading",
        )

    async def _post(self, path: str, payload: dict, action: str) -> bool:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            # 網路層失敗(逾時、連線中斷)也算失敗,交由上層決定要不要備援
            logger.warning("LINE %s request 失敗:%s", action, exc)
            return False

        if response.is_success:
            return True

        logger.warning(
            "LINE %s 回傳 %s:%s", action, response.status_code, response.text[:500]
        )
        return False


def _text_message(text: str) -> dict:
    """組成 LINE 的文字訊息物件,並確保不超過長度上限。"""
    trimmed = (text or "").strip()
    if not trimmed:
        trimmed = "(沒有內容)"
    if len(trimmed) > MAX_TEXT_LENGTH:
        trimmed = trimmed[: MAX_TEXT_LENGTH - 1] + "…"
    return {"type": "text", "text": trimmed}


def extract_follow_event(event: dict) -> Optional[dict]:
    """加好友事件。LINE 的 follow 事件也帶 replyToken,所以歡迎訊息是免費的。"""
    if event.get("type") != "follow":
        return None

    source = event.get("source") or {}
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")
    if not line_user_id or not reply_token:
        return None

    return {"line_user_id": line_user_id, "reply_token": reply_token}


def extract_text_event(event: dict) -> Optional[dict]:
    """從 webhook event 取出我們處理得了的文字訊息,取不到就回 None。

    只在這裡做欄位挖掘,讓後續流程拿到的是扁平、必要欄位都齊全的 dict。
    """
    if event.get("type") != "message":
        return None

    message = event.get("message") or {}
    if message.get("type") != "text":
        return None

    source = event.get("source") or {}
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")
    text = message.get("text")

    if not line_user_id or not reply_token or not text:
        return None

    return {
        "event_id": event.get("webhookEventId"),
        "line_user_id": line_user_id,
        "reply_token": reply_token,
        "text": text,
        "timestamp": event.get("timestamp"),
    }
