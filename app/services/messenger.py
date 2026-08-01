"""送出訊息的決策邏輯(docs/DESIGN.md 5.3)。

整個專案的成本結構壓在這個檔案上:

    reply  —— 用 webhook 帶回的 replyToken,免費,但 token 一次性且數秒內過期
    push   —— 主動推送,無時效,但計入每月 200 則的免費額度

所以流程永遠是「先試 reply,失敗才 push」,而且 push 前要先確認本月額度。
push 用量長期偏高不代表要升級方案,代表後端太慢、reply 常常來不及——
那應該回頭優化速度。
"""

import logging
from typing import TYPE_CHECKING, Optional

from app.repositories import usage_log
from app.services import notifier

if TYPE_CHECKING:  # 只為了型別註解,避免 runtime ←→ services 的循環 import
    from app.runtime import Runtime

logger = logging.getLogger(__name__)


async def deliver(
    runtime: "Runtime",
    line_user_id: str,
    reply_token: Optional[str],
    text: str,
) -> bool:
    """把訊息送到使用者手上,回傳是否成功。

    先試 reply(免費);失敗才在額度允許的範圍內改用 push。
    """
    if reply_token and await runtime.line.reply(reply_token, text):
        return True

    if reply_token:
        logger.warning("reply 失敗,改走 push 備援")

    if not await _push_allowed(runtime):
        # 到這裡代表使用者不會收到這則訊息。這是刻意的取捨:
        # 超額的 push 要付費,而額度用完通常代表系統有更根本的問題該修。
        logger.error(
            "本月 push 已達上限 %s 則,放棄送出訊息給 %s",
            runtime.settings.push_monthly_limit,
            line_user_id,
        )
        return False

    if not await runtime.line.push(line_user_id, text):
        logger.error("push 也失敗,使用者沒有收到訊息")
        return False

    await usage_log.record(
        runtime.db, usage_log.KIND_PUSH, line_user_id=line_user_id
    )
    return True


async def _push_allowed(runtime: "Runtime") -> bool:
    used = await usage_log.count_pushes_this_month(runtime.db)

    if used is None:
        # 查不到用量時選擇放行:單一使用者的 push 量本來就低,
        # 讓他收不到訊息的代價,大於偶爾超出幾則的代價。
        logger.warning("查不到本月 push 用量,這次仍放行")
        return True

    limit = runtime.settings.push_monthly_limit
    if used >= limit:
        return False

    if used >= limit - 20:
        logger.warning("本月 push 已用 %s/%s 則,接近上限", used, limit)
        # 通知擁有者。push 用量高通常代表後端太慢、reply 常常來不及,
        # 該做的是回頭優化速度,而不是升級方案(見 docs/DESIGN.md 5.3)。
        await notifier.notify_owner(
            runtime,
            notifier.KIND_PUSH_QUOTA,
            "本月 push 已用 {}/{} 則。push 用量高通常代表 reply 常常來不及,"
            "建議先看後端速度。".format(used, limit),
        )

    return True
