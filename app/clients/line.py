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

# 下載使用者傳來的圖片、影音、檔案要用另一個網域
LINE_DATA_BASE = "https://api-data.line.me/v2/bot"

# 附件大小上限。船上網路傳大檔本來就會失敗,而且解析大檔會吃掉時間預算。
# 超過就請他改用文字描述。
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

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

    async def get_content(self, message_id: str) -> Optional[bytes]:
        """下載使用者傳來的附件內容。太大或失敗就回 None。

        內容只留在記憶體裡,不落地成檔案——那是使用者的私人資料,
        少一個地方存就少一個外洩的可能。
        """
        url = "{}/message/{}/content".format(LINE_DATA_BASE, message_id)
        try:
            response = await self._client.get(url, timeout=15.0)
        except httpx.HTTPError as exc:
            logger.warning("下載附件失敗:%s", exc)
            return None

        if not response.is_success:
            logger.warning(
                "下載附件回傳 %s:%s", response.status_code, response.text[:200]
            )
            return None

        data = response.content
        if len(data) > MAX_ATTACHMENT_BYTES:
            logger.warning("附件 %s bytes 超過上限,放棄處理", len(data))
            return None
        return data

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


# 我們會處理的訊息型別。不在這裡面的(目前沒有)一律當作不支援,
# 但仍然會給使用者一個回應——沉默是最糟的處理方式。
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_FILE = "file"
KIND_LOCATION = "location"
KIND_STICKER = "sticker"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"


def extract_message_event(event: dict) -> Optional[dict]:
    """從 webhook event 取出訊息,取不到就回 None。

    只在這裡做欄位挖掘,讓後續流程拿到的是扁平、必要欄位都齊全的 dict,
    並且用 `kind` 標示型別,不用再回頭看 LINE 的原始結構。
    """
    if event.get("type") != "message":
        return None

    message = event.get("message") or {}
    source = event.get("source") or {}
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")

    # 群組訊息沒有 userId,也不是這個專案的使用情境
    if not line_user_id or not reply_token:
        return None

    base = {
        "event_id": event.get("webhookEventId"),
        "line_user_id": line_user_id,
        "reply_token": reply_token,
        "timestamp": event.get("timestamp"),
        "message_id": message.get("id"),
    }

    kind = message.get("type")

    if kind == KIND_TEXT:
        text = message.get("text")
        if not text:
            return None
        return dict(base, kind=KIND_TEXT, text=text)

    if kind in (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO):
        return dict(base, kind=kind, text="")

    if kind == KIND_FILE:
        return dict(
            base,
            kind=KIND_FILE,
            text="",
            file_name=message.get("fileName") or "檔案",
            file_size=message.get("fileSize") or 0,
        )

    if kind == KIND_LOCATION:
        return dict(
            base,
            kind=KIND_LOCATION,
            text="",
            address=message.get("address") or "",
            title=message.get("title") or "",
            latitude=message.get("latitude"),
            longitude=message.get("longitude"),
        )

    if kind == KIND_STICKER:
        return dict(base, kind=KIND_STICKER, text="")

    logger.info("不支援的訊息型別:%s", kind)
    return None
