"""LINE 驗簽與事件解析。

驗簽是唯一擋在公開端點前面的東西,值得測仔細一點。
"""

import base64
import hashlib
import hmac

from app.clients.line import (
    MAX_TEXT_LENGTH,
    _text_message,
    extract_text_event,
    verify_signature,
)

SECRET = "test-channel-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_接受正確簽章():
    body = b'{"events":[]}'
    assert verify_signature(SECRET, body, sign(body)) is True


def test_拒絕被竄改的內容():
    body = b'{"events":[]}'
    signature = sign(body)
    assert verify_signature(SECRET, b'{"events":[1]}', signature) is False


def test_拒絕錯誤的密鑰():
    body = b'{"events":[]}'
    assert verify_signature(SECRET, body, sign(body, "wrong-secret")) is False


def test_拒絕空簽章或未設定密鑰():
    body = b'{"events":[]}'
    assert verify_signature(SECRET, body, "") is False
    assert verify_signature("", body, sign(body)) is False


def text_event(**overrides) -> dict:
    event = {
        "type": "message",
        "webhookEventId": "01ABCDEF",
        "replyToken": "reply-token",
        "source": {"type": "user", "userId": "U-test-user"},
        "message": {"type": "text", "text": "今天很累"},
        "timestamp": 1_700_000_000_000,
    }
    event.update(overrides)
    return event


def test_解析文字訊息事件():
    parsed = extract_text_event(text_event())
    assert parsed == {
        "event_id": "01ABCDEF",
        "line_user_id": "U-test-user",
        "reply_token": "reply-token",
        "text": "今天很累",
        "timestamp": 1_700_000_000_000,
    }


def test_略過非文字訊息():
    assert extract_text_event(text_event(message={"type": "sticker"})) is None
    assert extract_text_event(text_event(type="follow")) is None


def test_略過缺少必要欄位的事件():
    # 群組事件沒有 userId
    assert extract_text_event(text_event(source={"type": "group"})) is None
    assert extract_text_event(text_event(replyToken=None)) is None


def test_過長的訊息會被截斷():
    message = _text_message("欸" * (MAX_TEXT_LENGTH + 100))
    assert len(message["text"]) == MAX_TEXT_LENGTH
    assert message["text"].endswith("…")


def test_空訊息不會送出空字串():
    assert _text_message("   ")["text"] == "(沒有內容)"
