"""測試用的假用戶端。

刻意做得很笨:只記錄被呼叫了什麼、回傳預先設定好的結果。
測試要驗的是「什麼情況下該做什麼決定」,不是 HTTP 細節。
"""

import asyncio
from typing import Any, Dict, List, Optional

from app.clients.openai_client import ChatResult
from app.config import load_settings
from app.runtime import Runtime
from app.services.commands import CommandHandler
from app.services.inbox import MessageBatcher

DEFAULT_REPLY = "欸真的假的,今天特別忙?"

# 測試用的等待窗要夠短,否則每個測試都要真的等 3 秒
TEST_WINDOW = 0.05


def make_message(text="今天很累", event_id="01ABCDEF", reply_token="reply-token"):
    return {
        "event_id": event_id,
        "line_user_id": "U-fictional-test-user",
        "reply_token": reply_token,
        "text": text,
        "timestamp": 1_700_000_000_000,
    }


class FakeLine:
    def __init__(self, reply_ok: bool = True, push_ok: bool = True) -> None:
        self.replies: List[tuple] = []
        self.pushes: List[tuple] = []
        self.loadings: List[str] = []
        self._reply_ok = reply_ok
        self._push_ok = push_ok

    async def reply(self, reply_token: str, text: str) -> bool:
        self.replies.append((reply_token, text))
        return self._reply_ok

    async def push(self, line_user_id: str, text: str) -> bool:
        self.pushes.append((line_user_id, text))
        return self._push_ok

    async def show_loading(self, line_user_id: str, seconds: int = 20) -> bool:
        self.loadings.append(line_user_id)
        return True


class FakeOpenAI:
    def __init__(
        self,
        response: str = DEFAULT_REPLY,
        error: Optional[Exception] = None,
        delay: float = 0.0,
    ) -> None:
        self.response = response
        self.error = error
        self.delay = delay
        self.calls: List[List[Dict[str, Any]]] = []
        self.options: List[Dict[str, Any]] = []
        self.instructions: List[str] = []
        self.used_search = False

    async def converse(
        self, instructions, messages, allow_search=True, max_tokens=None
    ) -> ChatResult:
        """對應 OpenAIClient.converse。對話走這條,記憶抽取走 chat。"""
        self.instructions.append(instructions)
        self.calls.append(messages)
        self.options.append({"allow_search": allow_search, "max_tokens": max_tokens})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return ChatResult(
            text=self.response,
            model="fake-model",
            prompt_tokens=100,
            completion_tokens=20,
            used_search=self.used_search,
        )

    async def chat(
        self,
        messages,
        model=None,
        max_tokens=None,
        json_mode=False,
        temperature=0.8,
    ) -> ChatResult:
        self.calls.append(messages)
        self.options.append(
            {
                "model": model,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "temperature": temperature,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return ChatResult(
            text=self.response,
            model=model or "fake-model",
            prompt_tokens=100,
            completion_tokens=20,
        )


class FakeDB:
    """記下所有寫入,並可指定第幾次 insert 要丟例外。

    push_count 模擬 usage_log 裡本月已送出的 push 則數。
    """

    def __init__(
        self,
        error: Optional[Exception] = None,
        error_on_call: int = 1,
        push_count: int = 0,
        usage_today: int = 0,
        select_error: Optional[Exception] = None,
        memories: Optional[List[Dict[str, Any]]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.usage: List[Dict[str, Any]] = []
        self.audits: List[Dict[str, Any]] = []
        self.created_memories: List[Dict[str, Any]] = []
        self.updates: List[tuple] = []
        self.error = error
        self.error_on_call = error_on_call
        self.push_count = push_count
        # 今天已經用掉的次數,用來測每日上限
        self.usage_today = usage_today
        self.select_error = select_error
        self.memories = memories or []
        # 短期脈絡。fetch_context 的資料庫端是 created_at 降序,所以這裡也照著給
        self.history = history or []
        self.unextracted: List[Dict[str, Any]] = []
        # 記下每次查詢的參數,測試才驗得到「本次訊息有沒有被排除在脈絡外」
        self.select_params: List[tuple] = []

    async def insert(self, table: str, row: Dict[str, Any], returning: bool = True):
        if table == "usage_log":
            self.usage.append(row)
            return None
        if table == "memory_audit":
            self.audits.append(row)
            return None
        if table == "memories":
            self.created_memories.append(row)
            return dict(row, id="new-memory-{}".format(len(self.created_memories)))

        self.rows.append(row)
        if self.error and len(self.rows) == self.error_on_call:
            raise self.error
        return dict(row, id="fake-id", created_at="2026-08-01T00:00:00+00:00")

    async def select(self, table: str, params: Dict[str, Any]):
        self.select_params.append((table, params))
        if self.select_error:
            raise self.select_error
        if table == "usage_log":
            # kind=push 的查詢問的是「本月幾則」,其餘問的是「今天幾次」
            if params.get("kind") == "eq.push":
                return [{"id": "push-{}".format(i)} for i in range(self.push_count)]
            return [{"id": "u-{}".format(i)} for i in range(self.usage_today)]
        if table == "memories":
            return list(self.memories)
        if table == "memory_audit":
            return list(self.audits)
        if table == "conversations":
            if params.get("extracted") == "is.false":
                return list(self.unextracted)
            return list(self.history)
        return []

    async def update(self, table: str, params: Dict[str, Any], values: Dict[str, Any]):
        self.updates.append((table, params, values))
        if table == "memories":
            # 模擬 PostgREST 回傳被更新的列
            return [dict(m) for m in self.memories]
        return []

    @property
    def conversations(self) -> List[Dict[str, Any]]:
        return self.rows


def make_runtime(line=None, openai=None, db=None, settings=None) -> Runtime:
    return Runtime(
        settings=settings or load_settings(),
        line=line or FakeLine(),
        openai=openai or FakeOpenAI(),
        db=db or FakeDB(),
        batcher=MessageBatcher(window_seconds=TEST_WINDOW),
        commands=CommandHandler(),
    )
