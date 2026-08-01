"""產生與送出回覆(app/services/chat.py)。

重點:失敗時使用者不會面對沉默、超時要有上限、以及回覆與紀錄的先後順序。
"""

import asyncio
import dataclasses

import pytest

from app.clients.supabase import SupabaseError
from app.prompts import FALLBACK_REPLY
from app.services.chat import handle_batch, merge_texts
from tests.fakes import (
    DEFAULT_REPLY,
    FakeDB,
    FakeLine,
    FakeOpenAI,
    make_message,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


async def test_正常流程會回覆並記錄():
    runtime = make_runtime()

    await handle_batch(runtime, [make_message()])

    assert runtime.line.replies == [("reply-token", DEFAULT_REPLY)]
    assert [(r["role"], r["content"]) for r in runtime.db.conversations] == [
        ("assistant", DEFAULT_REPLY)
    ]
    # 使用者訊息在進等待窗之前就寫過了,這裡只寫知平的回覆


async def test_記錄_token_用量():
    runtime = make_runtime()

    await handle_batch(runtime, [make_message()])

    assert runtime.db.usage == [
        {
            "line_user_id": "U-fictional-test-user",
            "kind": "chat",
            "model": "fake-model",
            "prompt_tokens": 100,
            "completion_tokens": 20,
        }
    ]


async def test_LLM_失敗時送出友善回覆():
    runtime = make_runtime(openai=FakeOpenAI(error=RuntimeError("openai down")))

    await handle_batch(runtime, [make_message()])

    assert runtime.line.replies == [("reply-token", FALLBACK_REPLY)]


async def test_超過時間預算就放棄改送備援回覆():
    """慢的回覆比沒有回覆更糟——reply token 會過期,備援的 push 要付費。"""
    settings = dataclasses.replace(make_runtime().settings, generation_timeout=0.05)
    runtime = make_runtime(openai=FakeOpenAI(delay=5.0), settings=settings)

    started = asyncio.get_event_loop().time()
    await handle_batch(runtime, [make_message()])
    elapsed = asyncio.get_event_loop().time() - started

    assert runtime.line.replies == [("reply-token", FALLBACK_REPLY)]
    assert elapsed < 1.0  # 不會傻等那 5 秒


async def test_LLM_回傳空字串也要有話說():
    runtime = make_runtime(openai=FakeOpenAI(response="   "))

    await handle_batch(runtime, [make_message()])

    assert runtime.line.replies == [("reply-token", FALLBACK_REPLY)]


async def test_寫入回覆失敗不影響已送出的回覆():
    runtime = make_runtime(db=FakeDB(error=SupabaseError("timeout")))

    await handle_batch(runtime, [make_message()])

    assert runtime.line.replies == [("reply-token", DEFAULT_REPLY)]


async def test_送不出去的回覆不寫進對話紀錄():
    """使用者沒看到的內容若留在紀錄裡,下次組脈絡時知平會引用他沒讀過的話。"""
    runtime = make_runtime(line=FakeLine(reply_ok=False, push_ok=False))

    await handle_batch(runtime, [make_message()])

    assert runtime.db.conversations == []


async def test_合併多則訊息只呼叫一次_LLM_並用最後一則的_token():
    runtime = make_runtime()
    batch = [
        make_message(text="欸", event_id="01", reply_token="token-1"),
        make_message(text="你在嗎", event_id="02", reply_token="token-2"),
        make_message(text="剛剛網路斷了", event_id="03", reply_token="token-3"),
    ]

    await handle_batch(runtime, batch)

    assert len(runtime.openai.calls) == 1
    assert len(runtime.line.replies) == 1
    # 最新的 replyToken 存活機率最高
    assert runtime.line.replies[0][0] == "token-3"

    user_message = runtime.openai.calls[0][-1]
    assert user_message["content"] == "欸\n你在嗎\n剛剛網路斷了"


async def test_system_prompt_有帶進去():
    runtime = make_runtime()

    await handle_batch(runtime, [make_message()])

    # 角色設定走 Responses API 的 instructions,不佔 messages 陣列
    assert "你叫知平" in runtime.openai.instructions[0]
    assert runtime.openai.calls[0][-1]["content"] == "今天很累"


async def test_合併時不加標記():
    """加上「訊息 1:」之類的標記會讓 LLM 用條列的方式回應。"""
    merged = merge_texts([make_message(text="a"), make_message(text="b")])
    assert merged == "a\nb"
