"""訊息湧入的合併處理(docs/DESIGN.md 5.5)。

驗的是「使用者連送五則,只會收到一則回覆」這件事——那是 5.7 驗收清單裡的一項。
"""

import asyncio

import pytest

from app.clients.supabase import DuplicateRecordError
from tests.fakes import TEST_WINDOW, FakeDB, make_message, make_runtime

pytestmark = pytest.mark.asyncio

# 等待窗結束後還要留一點時間讓後續流程跑完
SETTLE = TEST_WINDOW * 6


async def test_連續五則訊息只收到一則回覆():
    runtime = make_runtime()

    for i in range(5):
        await runtime.batcher.submit(
            runtime,
            make_message(
                text="第 {} 句".format(i),
                event_id="event-{}".format(i),
                reply_token="token-{}".format(i),
            ),
        )
    await asyncio.sleep(SETTLE)

    assert len(runtime.line.replies) == 1
    assert len(runtime.openai.calls) == 1
    # 用最後一則的 replyToken
    assert runtime.line.replies[0][0] == "token-4"
    # 五則訊息都完整進了 LLM
    assert runtime.openai.calls[0][-1]["content"].count("\n") == 4


async def test_等待窗內有新訊息就重置計時():
    runtime = make_runtime()

    await runtime.batcher.submit(runtime, make_message(event_id="a"))
    # 在窗關掉之前再送一則
    await asyncio.sleep(TEST_WINDOW * 0.5)
    await runtime.batcher.submit(runtime, make_message(event_id="b"))

    # 若沒有重置,第一則此時已經被送出去了
    await asyncio.sleep(TEST_WINDOW * 0.7)
    assert runtime.line.replies == []

    await asyncio.sleep(SETTLE)
    assert len(runtime.line.replies) == 1


async def test_間隔夠久的訊息會分別回覆():
    runtime = make_runtime()

    await runtime.batcher.submit(runtime, make_message(event_id="a"))
    await asyncio.sleep(SETTLE)
    await runtime.batcher.submit(runtime, make_message(event_id="b"))
    await asyncio.sleep(SETTLE)

    assert len(runtime.line.replies) == 2


async def test_訊息一進來就亮_loading_不等等待窗():
    """使用者送出後最慢一秒要看到動畫,否則會以為訊息沒送出去。"""
    runtime = make_runtime()

    await runtime.batcher.submit(runtime, make_message())
    await asyncio.sleep(0)  # 讓 fire-and-forget 的 task 有機會執行

    assert runtime.line.loadings == ["U-fictional-test-user"]
    assert runtime.line.replies == []  # 回覆還在等待窗裡


async def test_重送的_webhook_事件不會進等待窗():
    runtime = make_runtime(db=FakeDB(error=DuplicateRecordError("unique violation")))

    await runtime.batcher.submit(runtime, make_message())
    await asyncio.sleep(SETTLE)

    assert runtime.line.replies == []
    assert runtime.openai.calls == []
    assert runtime.line.loadings == []  # 連 loading 都不用亮


async def test_不同使用者的等待窗互不影響():
    runtime = make_runtime()

    first = make_message(event_id="a")
    second = dict(make_message(event_id="b"), line_user_id="U-another-user")

    await runtime.batcher.submit(runtime, first)
    await runtime.batcher.submit(runtime, second)
    await asyncio.sleep(SETTLE)

    assert len(runtime.line.replies) == 2


async def test_關機時取消等待中的計時器():
    runtime = make_runtime()

    await runtime.batcher.submit(runtime, make_message())
    await runtime.batcher.aclose()
    await asyncio.sleep(SETTLE)

    assert runtime.line.replies == []
