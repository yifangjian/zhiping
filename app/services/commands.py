"""記憶的可見性與可控性(docs/DESIGN.md 3.4)。

使用者有權知道系統記住了他什麼,並且能刪除。這不是附加功能——一個會記住你的
系統若沒有這個開關,使用者只能選擇「全都給它」或「都不要用」。

    你記得我什麼      依分類列出目前所有 active 記憶
    忘記 <關鍵字>     列出相符的記憶,確認後停用
    忘記全部          二次確認後全部停用(對話紀錄保留)

**指令比對要保守**。這些指令走在一般對話的前面,誤判的代價不對稱:
把閒聊當成指令(「忘記帶手套了」被當成刪除指令)會很突兀,
把指令當成閒聊只是知平用聊天的方式回應,還過得去。
所以「忘記」必須後面接空白或就是「忘記全部」,才算指令。

刪除一律是軟刪除,不做實體刪除,方便追溯與復原。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from app.prompts import CATEGORY_LABELS, to_second_person
from app.repositories import conversations, memories

if TYPE_CHECKING:  # 只為了型別註解,避免循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)

# 「你記得我什麼」的幾種常見說法。使用者不會每次都打得一模一樣。
RECALL_PHRASES = (
    "你記得我什麼",
    "你記得我什麼?",
    "你記得我什麼?",
    "你還記得我什麼",
    "你記得什麼",
    "你記得我哪些事",
)

FORGET_ALL_PHRASES = (
    "忘記全部",
    "全部忘記",
    "忘記所有",
    "忘記全部記憶",
    "忘掉全部",
    "全部忘掉",
    "把記憶全部刪掉",
    "刪掉全部記憶",
)

# 表達「想刪掉某件記憶」的詞。實機測試發現真人的講法五花八門:
#   「可以忘掉我說想吃荷包蛋的事情嗎」
#   「其實你在記憶裡可以完全把荷包蛋那條刪掉」
# 這些都不是以「忘記」開頭,第一版的開頭比對全部漏掉,結果 LLM 接手之後
# 還宣稱「已經刪掉了」——被告知已完成卻什麼都沒發生,比功能不存在更糟。
FORGET_INTENT_WORDS = (
    "忘記",
    "忘掉",
    "忘了",
    "刪掉",
    "刪除",
    "別記",
    "不要記",
    "不用記",
)

# 語尾助詞。「忘記荷包蛋吧」的關鍵字是「荷包蛋」不是「荷包蛋吧」。
TRAILING_PARTICLES = "吧嗎喔啦耶了呢好不。,!?!?、 　"

# 比對記憶內容時,長度足夠但沒有意義的共同片段。
# 沒有這層過濾,「忘掉那件事」會跟每一條以「他」開頭的記憶都相符。
MATCH_STOPWORDS = frozenset(
    {
        "他不", "他在", "他有", "他會", "他是", "他的", "他想", "他覺",
        "的事", "事情", "可以", "記得", "記憶", "那條", "這條", "完全",
        "什麼", "這個", "那個", "已經", "現在", "知道", "覺得", "我說",
        "一個", "不要", "不再", "還有", "然後", "因為", "所以",
    }
)

# 共同片段至少要這麼長才算相符
MIN_MATCH_LENGTH = 2

# 確認用的回應。刻意收得很窄——寧可再問一次,也不要誤刪。
CONFIRM_WORDS = ("是", "對", "好", "嗯", "確認", "要", "刪", "刪除", "確定", "yes", "y")
CANCEL_WORDS = ("不", "不用", "算了", "取消", "不要", "no", "n")

# 待確認的刪除請求超過這個秒數就失效。使用者網路差,可能隔很久才回話,
# 但也不能讓一句「好」在半小時後意外刪掉東西。
PENDING_TTL_SECONDS = 300


@dataclass
class PendingForget:
    """等待使用者確認的刪除請求。"""

    memory_ids: List[str]
    created_at: float
    # 用來遮蔽原始對話的關鍵字。只刪 memories 不夠——使用者當初講那句話的
    # 對話紀錄還在短期脈絡裡,知平照樣講得出來
    keywords: List[str] = field(default_factory=list)
    is_forget_all: bool = False

    def is_expired(self, now: float) -> bool:
        return now - self.created_at > PENDING_TTL_SECONDS


class CommandHandler:
    """記憶指令的辨識與執行。

    待確認狀態存在行程內,理由與 inbox.py 的等待窗相同:單一使用者、單一 instance。
    過期就當作沒發生過,使用者重打一次即可。
    """

    def __init__(self) -> None:
        self._pending: Dict[str, PendingForget] = {}

    async def handle(
        self, runtime: "Runtime", line_user_id: str, text: str
    ) -> Optional[str]:
        """若這句話是記憶指令,執行並回傳要送出的訊息;否則回傳 None 交給一般對話。"""
        message = (text or "").strip()
        if not message:
            return None

        # 先處理待確認的刪除——使用者的「好」只在這個情境下有特殊意義
        pending = self._take_pending(line_user_id)
        if pending is not None:
            answer = await self._resolve_pending(runtime, line_user_id, pending, message)
            if answer is not None:
                return answer
            # 不是確認也不是取消,當成一般對話,刪除請求就此作廢

        if message in RECALL_PHRASES:
            return await self._list_memories(runtime, line_user_id)

        if message in FORGET_ALL_PHRASES:
            return await self._ask_forget_all(runtime, line_user_id)

        if _has_forget_intent(message):
            return await self._ask_forget(runtime, line_user_id, message)

        return None

    # --- 你記得我什麼 ---

    async def _list_memories(self, runtime: "Runtime", line_user_id: str) -> str:
        rows = await memories.fetch_all_active(runtime.db, line_user_id)
        if not rows:
            return "目前還沒記得什麼。多聊幾次就會有了。"

        # 一行一條,保持簡短(使用者網路差)
        grouped: Dict[str, List[str]] = {}
        for row in rows:
            grouped.setdefault(row.get("category") or "", []).append(
                row.get("content") or ""
            )

        blocks = []
        for category, label in CATEGORY_LABELS.items():
            items = grouped.get(category)
            if not items:
                continue
            # 記憶存的是第三人稱,列給使用者看要換成對他說話的口吻
            lines = "\n".join("・{}".format(to_second_person(item)) for item in items)
            blocks.append("{}\n{}".format(label, lines))

        return "{}\n\n想刪掉哪條就打「忘記 <關鍵字>」。".format("\n\n".join(blocks))

    # --- 忘記 <關鍵字> ---

    async def _ask_forget(
        self, runtime: "Runtime", line_user_id: str, message: str
    ) -> Optional[str]:
        """找出使用者想忘記的記憶,列出來等確認。

        比對方式:拿每一條現有記憶去跟這句話找最長共同片段。
        「可以忘掉我說想吃荷包蛋的事情嗎」與「他不再想吃荷包蛋」的共同片段是
        「想吃荷包蛋」,夠長也不是廢話,就算相符。

        **找不到相符記憶時的處理分兩種**:

        - 明確格式(「忘記 <關鍵字>」有空白)→ 回一句「沒找到」,
          他知道自己下了指令,沉默會讓他以為系統壞了
        - 自然語句 → 回 None 交給一般對話。「忘記帶手套了」這種話本來就不是指令

        誤判在這裡是安全的:刪除一律二次確認,最壞情況只是多問一句。
        """
        rows = await memories.fetch_all_active(runtime.db, line_user_id)
        matched = _match_memories(message, rows)

        if not matched:
            keyword = _explicit_keyword(message)
            if keyword:
                return "沒找到跟「{}」有關的記憶。".format(keyword)
            return None

        self._pending[line_user_id] = PendingForget(
            memory_ids=[row["id"] for row, _ in matched],
            keywords=[fragment for _, fragment in matched],
            created_at=time.time(),
        )

        lines = "\n".join(
            "・{}".format(to_second_person(row.get("content") or ""))
            for row, _ in matched
        )
        return "這些要忘記嗎?\n{}\n\n回「好」我就徹底刪掉。".format(lines)

    # --- 忘記全部 ---

    async def _ask_forget_all(self, runtime: "Runtime", line_user_id: str) -> str:
        rows = await memories.fetch_all_active(runtime.db, line_user_id)
        if not rows:
            return "本來就沒記得什麼。"

        self._pending[line_user_id] = PendingForget(
            memory_ids=[], created_at=time.time(), is_forget_all=True
        )
        return "確定要我忘記全部 {} 件事嗎?回「好」就全部刪掉。".format(len(rows))

    # --- 確認流程 ---

    def _take_pending(self, line_user_id: str) -> Optional[PendingForget]:
        pending = self._pending.pop(line_user_id, None)
        if pending is None:
            return None
        if pending.is_expired(time.time()):
            logger.info("待確認的刪除請求已過期")
            return None
        return pending

    async def _resolve_pending(
        self,
        runtime: "Runtime",
        line_user_id: str,
        pending: PendingForget,
        message: str,
    ) -> Optional[str]:
        lowered = message.lower()

        if lowered in CANCEL_WORDS:
            return "好,那就留著。"

        if lowered not in CONFIRM_WORDS:
            return None  # 不是在回答確認,交給一般對話

        if pending.is_forget_all:
            count = await memories.deactivate_all(runtime.db, line_user_id)
            logger.info("使用者要求忘記全部,停用 %s 條記憶", count)
            return "都忘了。"

        count = await memories.deactivate(
            runtime.db, line_user_id, pending.memory_ids, actor=memories.ACTOR_USER
        )

        # 記憶刪掉還不夠:原始對話還在短期脈絡裡,不遮蔽的話知平照樣講得出來
        hidden = await conversations.hide_containing(
            runtime.db, line_user_id, pending.keywords
        )
        logger.info(
            "使用者要求忘記:停用 %s 條記憶,遮蔽 %s 則相關對話", count, hidden
        )
        return "刪掉了。" if count else "那些好像已經不在了。"


def _has_forget_intent(message: str) -> bool:
    """這句話有沒有在講「刪掉某件記憶」。

    只是初篩,真正決定是不是指令的是「有沒有記憶真的相符」(見 _ask_forget)。
    """
    return any(word in message for word in FORGET_INTENT_WORDS)


def _explicit_keyword(message: str) -> Optional[str]:
    """取出規格文件格式「忘記 <關鍵字>」裡的關鍵字,不是這個格式就回 None。

    只用來決定「找不到相符記憶時要不要出聲」——打了空白代表他知道自己在下指令。
    """
    for word in ("忘記", "忘掉"):
        if not message.startswith(word):
            continue
        rest = message[len(word) :]
        if rest and rest[0] in (" ", "　"):
            keyword = rest.strip().strip(TRAILING_PARTICLES)
            return keyword or None
    return None


def _match_memories(
    message: str, rows: List[Dict[str, Any]]
) -> List[Tuple[Dict[str, Any], str]]:
    """找出這句話講的是哪些記憶,回傳 [(記憶, 共同片段)]。

    用最長共同片段而不是斷詞:中文斷詞要嘛引進一個套件,要嘛自己寫一堆規則,
    而這裡要判斷的其實只是「使用者有沒有提到記憶裡的某個具體東西」。
    片段夠長又不是廢話,就足以判斷。

    例:「可以忘掉我說想吃荷包蛋的事情嗎」與記憶「他不再想吃荷包蛋」
        的最長共同片段是「想吃荷包蛋」,五個字,算相符。
    """
    matched = []
    for row in rows:
        content = row.get("content") or ""
        fragment = _longest_common_substring(message, content)
        if _is_meaningful_match(fragment):
            matched.append((row, fragment))
    return matched


def _is_meaningful_match(fragment: str) -> bool:
    """這個共同片段算不算「他提到了那件事」。

    記憶一律以第三人稱寫成、幾乎都由「他」開頭,所以「他叫」「他在」這種短片段
    是文法上的重疊,不是內容上的重疊——「我忘了他叫什麼」不該被當成刪除指令。
    """
    if len(fragment) < MIN_MATCH_LENGTH:
        return False
    if fragment in MATCH_STOPWORDS:
        return False
    if fragment.startswith("他") and len(fragment) <= 3:
        return False
    return True


def _longest_common_substring(left: str, right: str) -> str:
    """兩個字串的最長共同片段。字串都很短,用最直觀的 DP 就好。"""
    if not left or not right:
        return ""

    best_end = 0
    best_length = 0
    previous = [0] * (len(right) + 1)

    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best_length:
                    best_length = current[j]
                    best_end = i
        previous = current

    return left[best_end - best_length : best_end]
