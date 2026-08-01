"""首次啟用流程(docs/DESIGN.md 8.4)。

使用者加好友時送一則自我介紹。內容有三點,第二點是**知情同意,不能省**:

    我會記得你說過的事,你隨時可以打「你記得我什麼」來看,也可以要我忘記

一個會長期記住你的系統,使用者有權在第一時間就知道它會記住、怎麼查、怎麼刪。
把這件事藏起來,等他自己發現,是很糟的設計。

訊息一則講完不分段:省額度(每月只有 200 則免費),也符合弱網路情境。
"""

import logging
from typing import TYPE_CHECKING, Any, Dict

from app.prompts import WELCOME_MESSAGE
from app.services import messenger

if TYPE_CHECKING:  # 只為了型別註解,避免循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)


async def handle_follow(runtime: "Runtime", event: Dict[str, Any]) -> None:
    """送出加好友的自我介紹。不往外丟例外——它跑在背景任務裡。"""
    line_user_id = event["line_user_id"]
    try:
        await messenger.deliver(
            runtime, line_user_id, event["reply_token"], WELCOME_MESSAGE
        )
        logger.info("已送出首次啟用訊息")
    except Exception:  # noqa: BLE001
        logger.exception("送出首次啟用訊息失敗")
