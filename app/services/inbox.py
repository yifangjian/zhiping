"""訊息湧入的合併處理(docs/DESIGN.md 5.5)。

**要解決的情境**:使用者在船上斷線好幾個小時,重新連上後 LINE 會把期間累積的訊息
一次全部送達。若逐則處理,知平會連續吐出好幾則回覆——在弱網路下體驗很差,也白白
消耗額度。使用者一次打三句話當一個意思講,也是同樣的問題。

**作法**:收到訊息先寫進資料庫,然後開一個 3 秒的等待窗;窗內若又有同一使用者的
訊息進來就重置計時。等待結束後把這段期間的訊息合併成一次 LLM 呼叫,只回一則。

**兩個關鍵細節**:

1. 回覆時用**最後一則**訊息的 replyToken。token 以秒計時效,最新的那個存活機率最高。
2. loading 動畫在收到訊息的當下就送出,不等等待窗結束。否則使用者會先盯著三秒的
   靜止畫面,那正是我們想避免的「不知道有沒有送出去」的感覺。

**已知限制**:等待窗是行程內狀態(記憶體裡的 dict),多個 instance 各自有一份。
本專案是單一使用者、單一 instance,這個取捨划算——換成 Redis 會為了用不到的擴充性
多一個相依服務。若日後真的要水平擴充,這裡要換成共用儲存。
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Set

from app.repositories import conversations
from app.services import attachments
from app.services.chat import handle_batch

if TYPE_CHECKING:  # 只為了型別註解,避免循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)


class MessageBatcher:
    """把同一使用者在短時間內送出的多則訊息,合併成一次處理。"""

    def __init__(self, window_seconds: float = 3.0) -> None:
        self._window = window_seconds
        # line_user_id -> 等待中的訊息
        self._pending: Dict[str, List[Dict[str, Any]]] = {}
        # line_user_id -> 倒數計時的 task
        self._timers: Dict[str, asyncio.Task] = {}
        # 保留 fire-and-forget task 的參照,否則可能被 GC 中途回收
        self._background: Set[asyncio.Task] = set()
        # 事件迴圈是單執行緒的,但 _pending / _timers 的讀寫跨越了 await,
        # 仍可能交錯。用一把鎖把它們的變動包起來。
        self._lock = asyncio.Lock()

    async def submit(self, runtime: "Runtime", message: Dict[str, Any]) -> None:
        """收下一則訊息:寫入資料庫、亮起 loading、加入等待窗。"""
        line_user_id = message["line_user_id"]

        # 先寫入。webhook_event_id 的 unique index 會擋掉 LINE 的重送,
        # 拿到 None 代表這則已經處理過(見 docs/DESIGN.md 5.4)。
        try:
            saved = await conversations.save_message(
                runtime.db,
                line_user_id=line_user_id,
                role=conversations.ROLE_USER,
                # 非文字訊息也要留紀錄,否則之後撈脈絡會看到莫名的空白
                content=attachments.describe(message),
                webhook_event_id=message.get("event_id"),
            )
        except Exception:  # noqa: BLE001 — 背景任務的最後一道防線
            logger.exception("寫入使用者訊息失敗,這則訊息不進等待窗")
            return

        if saved is None:
            return

        # 記下寫入時間。組短期脈絡時要用它把「本次訊息」排除在歷史之外,
        # 否則同一句話會在 messages 陣列裡出現兩次(見 3.5)。
        message["created_at"] = saved.get("created_at")
        message["conversation_id"] = saved.get("id")

        # 不等 loading 的結果,它只是體驗上的即時回饋,不該佔用時間預算
        self._fire_and_forget(runtime.line.show_loading(line_user_id))

        async with self._lock:
            self._pending.setdefault(line_user_id, []).append(message)
            pending_count = len(self._pending[line_user_id])

            # 重置計時:只要還有新訊息進來,就再等一個完整的等待窗
            timer = self._timers.get(line_user_id)
            if timer is not None:
                timer.cancel()
            self._timers[line_user_id] = asyncio.create_task(
                self._flush_after_window(runtime, line_user_id)
            )

        if pending_count > 1:
            logger.info("等待窗內累積 %s 則訊息,重置計時", pending_count)

    async def _flush_after_window(self, runtime: "Runtime", line_user_id: str) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            # 有新訊息進來,由新的計時器接手
            return

        async with self._lock:
            batch = self._pending.pop(line_user_id, [])
            self._timers.pop(line_user_id, None)

        if not batch:
            return

        try:
            await handle_batch(runtime, batch)
        except Exception:  # noqa: BLE001 — 沒有人接得住背景任務的例外
            logger.exception("處理合併後的訊息時發生未預期的錯誤")

    def _fire_and_forget(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def aclose(self) -> None:
        """關機時取消所有等待中的計時器。

        等待窗裡的訊息已經寫進資料庫了,只是還沒被回覆。這是取捨:
        關機時強行回覆反而可能在 reply token 過期後燒掉 push 額度。
        """
        async with self._lock:
            timers = list(self._timers.values())
            dropped = sum(len(v) for v in self._pending.values())
            self._timers.clear()
            self._pending.clear()

        for timer in timers:
            timer.cancel()

        if dropped:
            logger.warning("關機時捨棄 %s 則尚未回覆的訊息", dropped)
