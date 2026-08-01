"""reply / push 的決策邏輯(docs/DESIGN.md 5.3)。

這是專案的成本閘門:reply 免費、push 計入每月 200 則額度。
每一條分支都直接對應到錢。
"""

import dataclasses

import pytest

from app.clients.supabase import SupabaseError
from app.services.messenger import deliver
from tests.fakes import FakeDB, FakeLine, make_runtime

pytestmark = pytest.mark.asyncio


async def test_reply_成功就不動用_push():
    runtime = make_runtime()

    ok = await deliver(runtime, "U-test", "reply-token", "嗨")

    assert ok is True
    assert runtime.line.replies == [("reply-token", "嗨")]
    assert runtime.line.pushes == []
    assert runtime.db.usage == []  # 沒有消耗額度就沒有紀錄


async def test_reply_失敗改用_push_並記錄額度():
    runtime = make_runtime(line=FakeLine(reply_ok=False))

    ok = await deliver(runtime, "U-test", "expired-token", "嗨")

    assert ok is True
    assert runtime.line.pushes == [("U-test", "嗨")]
    assert [u["kind"] for u in runtime.db.usage] == ["push"]


async def test_本月額度用完就不再_push():
    """超額的 push 要付費,而額度用完通常代表系統有更根本的問題該修。"""
    settings = dataclasses.replace(make_runtime().settings, push_monthly_limit=5)
    runtime = make_runtime(
        line=FakeLine(reply_ok=False), db=FakeDB(push_count=5), settings=settings
    )

    ok = await deliver(runtime, "U-test", "expired-token", "嗨")

    assert ok is False
    assert runtime.line.pushes == []
    assert runtime.db.usage == []


async def test_額度還沒滿就照送():
    settings = dataclasses.replace(make_runtime().settings, push_monthly_limit=5)
    runtime = make_runtime(
        line=FakeLine(reply_ok=False), db=FakeDB(push_count=4), settings=settings
    )

    ok = await deliver(runtime, "U-test", "expired-token", "嗨")

    assert ok is True
    assert len(runtime.line.pushes) == 1


async def test_查不到用量時選擇放行():
    """讓使用者收不到訊息的代價,大於偶爾超出幾則的代價。"""
    runtime = make_runtime(
        line=FakeLine(reply_ok=False),
        db=FakeDB(select_error=SupabaseError("connection refused")),
    )

    ok = await deliver(runtime, "U-test", "expired-token", "嗨")

    assert ok is True
    assert len(runtime.line.pushes) == 1


async def test_沒有_reply_token_時直接走_push():
    """例如 Phase 7 要主動通知擁有者,本來就沒有 token 可用。"""
    runtime = make_runtime()

    ok = await deliver(runtime, "U-owner", None, "系統異常")

    assert ok is True
    assert runtime.line.replies == []
    assert runtime.line.pushes == [("U-owner", "系統異常")]


async def test_reply_與_push_都失敗():
    runtime = make_runtime(line=FakeLine(reply_ok=False, push_ok=False))

    ok = await deliver(runtime, "U-test", "expired-token", "嗨")

    assert ok is False
    assert runtime.db.usage == []  # 沒送出去就不該記帳
