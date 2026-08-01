"""通知專案擁有者(docs/DESIGN.md 8.1)。

**為什麼需要**:使用者不會為了「機器人壞掉」主動抱怨,專案擁有者也不會每天檢查
系統狀態。沒有這個機制,故障可能好幾週後才被發現,而那段期間使用者是沒有陪伴的。

**防洪是必要的,不是加分項**:通知本身走 push,會吃掉每月 200 則的免費額度。
一個壞掉的迴圈可以在幾分鐘內把額度燒光——那時候連使用者的正常回覆都送不出去,
故障通知反而造成了更大的故障。所以同類事件一小時內只通知一次。
"""

import logging
import time
from typing import TYPE_CHECKING, Dict

from app.repositories import usage_log

if TYPE_CHECKING:  # 只為了型別註解,避免循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)

# 同一類事件的通知間隔
FLOOD_WINDOW_SECONDS = 3600

# 事件分類。分類要夠粗,才擋得住同一個問題的連環通知
KIND_PUSH_QUOTA = "push_quota"
KIND_OPENAI_FAILURE = "openai_failure"
KIND_DATABASE_FAILURE = "database_failure"
KIND_UNEXPECTED = "unexpected"

# 行程內的最後通知時間。重啟後會重置,那是可接受的——
# 重啟本身就代表狀況已經改變,值得再通知一次。
_last_sent: Dict[str, float] = {}


async def notify_owner(runtime: "Runtime", kind: str, message: str) -> bool:
    """通知專案擁有者。被防洪擋下或沒設定收件人時回傳 False。"""
    owner_id = runtime.settings.owner_line_user_id
    if not owner_id:
        logger.warning("沒有設定 OWNER_LINE_USER_ID,略過通知:%s", message)
        return False

    now = time.time()
    last = _last_sent.get(kind)
    if last is not None and now - last < FLOOD_WINDOW_SECONDS:
        logger.info("同類通知(%s)一小時內已送過,這次略過", kind)
        return False

    # 先記時間再送,避免送出過程中又有事件湧入造成重複通知
    _last_sent[kind] = now

    text = "[知平] {}".format(message)
    if not await runtime.line.push(owner_id, text):
        logger.error("通知擁有者失敗:%s", message)
        return False

    await usage_log.record(
        runtime.db, usage_log.KIND_NOTIFY, line_user_id=owner_id
    )
    logger.info("已通知擁有者:%s", message)
    return True


def reset_flood_control() -> None:
    """測試用:清掉防洪狀態。"""
    _last_sent.clear()
    _consecutive_failures.clear()


# --- 連續失敗追蹤 ---
#
# 規格 8.1 要的是「OpenAI API 連續失敗 3 次以上」才通知,不是每次失敗都通知。
# 單次失敗在弱網路下很常見,重試就好;連續失敗才代表真的壞了。
_consecutive_failures: Dict[str, int] = {}

FAILURE_THRESHOLD = 3


def record_failure(kind: str) -> int:
    """記一次失敗,回傳目前連續失敗次數。"""
    count = _consecutive_failures.get(kind, 0) + 1
    _consecutive_failures[kind] = count
    return count


def record_success(kind: str) -> None:
    """成功一次就把計數歸零——中間成功過就不算「連續」。"""
    _consecutive_failures.pop(kind, None)


async def notify_if_repeated(
    runtime: "Runtime", kind: str, detail: str
) -> bool:
    """記一次失敗,連續達門檻才通知擁有者。"""
    count = record_failure(kind)
    if count < FAILURE_THRESHOLD:
        logger.warning("%s 失敗第 %s 次(未達通知門檻)", kind, count)
        return False

    return await notify_owner(
        runtime, kind, "{} 連續失敗 {} 次。{}".format(kind, count, detail)
    )
