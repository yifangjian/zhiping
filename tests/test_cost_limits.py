"""成本控制(docs/DESIGN.md 第 4 節)。

單一使用者的正常用量碰不到這些上限——它們是「出事時止血」用的。
所以測試重點在:上限生效時使用者不會面對沉默,而且擁有者會被通知。
"""

import dataclasses
import json

import pytest

from app.clients.supabase import SupabaseError
from app.prompts import DAILY_LIMIT_REPLY
from app.services import notifier
from app.services.chat import handle_batch
from app.services.memory_extraction import EXTRACTION_THRESHOLD, maybe_extract
from app.services.messenger import deliver
from app.services.notifier import notify_owner, reset_flood_control
from tests.fakes import FakeDB, FakeLine, FakeOpenAI, make_message, make_runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_flood_control():
    reset_flood_control()
    yield
    reset_flood_control()


def with_owner(settings, **overrides):
    return dataclasses.replace(
        settings, owner_line_user_id="U-fictional-owner", **overrides
    )


# --- 每日對話上限 ---


async def test_達每日上限時仍然回話():
    """使用者不該面對沉默,但也不解釋「額度」這種系統概念。"""
    base = make_runtime()
    runtime = make_runtime(
        db=FakeDB(usage_today=200),
        settings=with_owner(base.settings, daily_chat_limit=200),
    )

    await handle_batch(runtime, [make_message()])

    assert runtime.line.replies == [("reply-token", DAILY_LIMIT_REPLY)]
    assert runtime.openai.calls == []  # 沒有花錢呼叫 LLM


async def test_達每日上限會通知擁有者():
    base = make_runtime()
    runtime = make_runtime(
        db=FakeDB(usage_today=200),
        settings=with_owner(base.settings, daily_chat_limit=200),
    )

    await handle_batch(runtime, [make_message()])

    assert any("U-fictional-owner" == to for to, _ in runtime.line.pushes)


async def test_未達上限照常回覆():
    runtime = make_runtime(db=FakeDB(usage_today=199))

    await handle_batch(runtime, [make_message()])

    assert len(runtime.openai.calls) == 1


async def test_查不到用量時放行():
    """讓使用者收不到回覆的代價,大於多回幾則的代價。"""
    runtime = make_runtime(db=FakeDB(select_error=SupabaseError("db down")))

    await handle_batch(runtime, [make_message()])

    assert runtime.line.replies  # 還是有回話


# --- 每日抽取上限 ---


async def test_抽取達每日上限就延到明天():
    db = FakeDB(usage_today=20)
    db.unextracted = [
        {"id": "c{}".format(i), "role": "user", "content": "x"}
        for i in range(EXTRACTION_THRESHOLD)
    ]
    base = make_runtime()
    runtime = make_runtime(
        db=db,
        openai=FakeOpenAI(response=json.dumps({"new_memories": [], "updates": [], "deactivate": []})),
        settings=with_owner(base.settings, daily_extract_limit=20),
    )

    await maybe_extract(runtime, "U-test")

    assert runtime.openai.calls == []
    # 對話沒有被標記,明天會再處理
    assert not any(table == "conversations" for table, _, _ in db.updates)


# --- push 警戒線 ---


async def test_接近_push_上限會通知擁有者():
    base = make_runtime()
    runtime = make_runtime(
        line=FakeLine(reply_ok=False),
        db=FakeDB(push_count=165),
        settings=with_owner(base.settings, push_monthly_limit=180),
    )

    await deliver(runtime, "U-test", "expired-token", "嗨")

    recipients = [to for to, _ in runtime.line.pushes]
    assert "U-fictional-owner" in recipients  # 通知有送出
    assert "U-test" in recipients  # 使用者的訊息也照送


# --- 防洪 ---


async def test_同類通知一小時內只送一次():
    """通知本身會吃 push 額度。一個壞掉的迴圈可以在幾分鐘內把額度燒光。"""
    base = make_runtime()
    runtime = make_runtime(settings=with_owner(base.settings))

    first = await notify_owner(runtime, notifier.KIND_PUSH_QUOTA, "第一次")
    second = await notify_owner(runtime, notifier.KIND_PUSH_QUOTA, "第二次")

    assert first is True
    assert second is False
    assert len(runtime.line.pushes) == 1


async def test_不同類的通知不互相擋():
    base = make_runtime()
    runtime = make_runtime(settings=with_owner(base.settings))

    await notify_owner(runtime, notifier.KIND_PUSH_QUOTA, "額度")
    await notify_owner(runtime, notifier.KIND_OPENAI_FAILURE, "API 掛了")

    assert len(runtime.line.pushes) == 2


async def test_沒設定擁有者時不會爆炸():
    runtime = make_runtime()  # owner_line_user_id 未設定

    assert await notify_owner(runtime, notifier.KIND_UNEXPECTED, "測試") is False
    assert runtime.line.pushes == []
