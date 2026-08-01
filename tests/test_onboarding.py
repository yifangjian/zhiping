"""首次啟用訊息與錯誤通知(docs/DESIGN.md 8.1、8.4)。"""

import dataclasses

import pytest

from app.clients.line import extract_follow_event
from app.prompts import WELCOME_MESSAGE
from app.services import notifier
from app.services.notifier import (
    FAILURE_THRESHOLD,
    notify_if_repeated,
    record_success,
    reset_flood_control,
)
from app.services.onboarding import handle_follow
from tests.fakes import make_runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_state():
    reset_flood_control()
    yield
    reset_flood_control()


def follow_event(**overrides):
    event = {
        "type": "follow",
        "replyToken": "follow-token",
        "source": {"type": "user", "userId": "U-fictional-new-user"},
    }
    event.update(overrides)
    return event


async def test_加好友會送出自我介紹():
    runtime = make_runtime()

    await handle_follow(runtime, extract_follow_event(follow_event()))

    assert runtime.line.replies == [("follow-token", WELCOME_MESSAGE)]


async def test_自我介紹包含知情同意():
    """一個會長期記住你的系統,使用者有權在第一時間知道怎麼查、怎麼刪。"""
    assert "我會記得你說過的事" in WELCOME_MESSAGE
    assert "你記得我什麼" in WELCOME_MESSAGE
    assert "忘記" in WELCOME_MESSAGE
    # 一則講完,不分段送出(省額度,也符合弱網路情境)
    assert len(WELCOME_MESSAGE) < 150


async def test_自我介紹走_reply_不吃額度():
    """follow 事件也帶 replyToken,所以歡迎訊息是免費的。"""
    runtime = make_runtime()

    await handle_follow(runtime, extract_follow_event(follow_event()))

    assert runtime.line.pushes == []


async def test_非_follow_事件不會被誤認():
    assert extract_follow_event({"type": "message"}) is None
    # 群組事件沒有 userId
    assert extract_follow_event(follow_event(source={"type": "group"})) is None


async def test_連續失敗達門檻才通知():
    """單次失敗在弱網路下很常見,重試就好;連續失敗才代表真的壞了。"""
    base = make_runtime()
    runtime = make_runtime(
        settings=dataclasses.replace(
            base.settings, owner_line_user_id="U-fictional-owner"
        )
    )

    results = [
        await notify_if_repeated(runtime, notifier.KIND_OPENAI_FAILURE, "timeout")
        for _ in range(FAILURE_THRESHOLD)
    ]

    assert results[:-1] == [False] * (FAILURE_THRESHOLD - 1)
    assert results[-1] is True
    assert len(runtime.line.pushes) == 1


async def test_中間成功過就不算連續():
    base = make_runtime()
    runtime = make_runtime(
        settings=dataclasses.replace(
            base.settings, owner_line_user_id="U-fictional-owner"
        )
    )

    await notify_if_repeated(runtime, notifier.KIND_OPENAI_FAILURE, "timeout")
    await notify_if_repeated(runtime, notifier.KIND_OPENAI_FAILURE, "timeout")
    record_success(notifier.KIND_OPENAI_FAILURE)
    await notify_if_repeated(runtime, notifier.KIND_OPENAI_FAILURE, "timeout")

    assert runtime.line.pushes == []
