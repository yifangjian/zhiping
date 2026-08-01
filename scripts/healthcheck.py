"""每日健康檢查(docs/DESIGN.md 8.2)。

    python scripts/healthcheck.py

**為什麼需要**:專案擁有者不會每天檢查系統狀態,而使用者個性上不會為了
「機器人壞掉」主動抱怨或求助。沒有這個檢查,故障可能好幾週後才被發現,
而那段期間他是沒有這個陪伴的。

檢查四件事:
  1. OpenAI API 可用
  2. Supabase 連線正常
  3. 當日對話數
  4. 連續 7 天沒有任何對話 → 通知擁有者
  加上 8.3 的提醒:超過 30 天沒備份也一併通知。

第 4 項的用意:沒有對話可能是系統壞了,也可能是使用者狀態不好或網路長期中斷。
兩種情況擁有者都會想知道。所以通知內容**保持中性、只陳述事實**,不做任何推測。

部署方式:用平台的排程功能(Railway cron、GitHub Actions、crontab 都可以)
每天跑一次。例如每天早上九點:
    0 9 * * * cd /path/to/zhiping && .venv/bin/python scripts/healthcheck.py
"""

import asyncio
import datetime
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from app.config import load_settings  # noqa: E402
from app.repositories import usage_log  # noqa: E402
from app.runtime import Runtime  # noqa: E402
from app.services import notifier  # noqa: E402
from app.timeutils import days_since as _days_since  # noqa: E402

QUIET_DAYS_THRESHOLD = 7
BACKUP_STALE_DAYS = 30


async def main() -> int:
    runtime = Runtime.create(load_settings())
    problems = []
    lines = []

    try:
        # 1. Supabase
        try:
            await runtime.db.select("conversations", {"select": "id", "limit": "1"})
            lines.append("Supabase:正常")
        except Exception as exc:  # noqa: BLE001
            lines.append("Supabase:失敗 {}".format(exc))
            problems.append("Supabase 連線失敗")

        # 2. OpenAI(用最小的呼叫,不浪費錢)
        try:
            result = await runtime.openai.chat(
                [{"role": "user", "content": "ping"}], max_tokens=5
            )
            lines.append("OpenAI:正常({})".format(result.model))
        except Exception as exc:  # noqa: BLE001
            lines.append("OpenAI:失敗 {}".format(exc))
            problems.append("OpenAI API 失敗")

        # 3. 當日對話數
        today_chats = await usage_log.count_today(runtime.db, usage_log.KIND_CHAT)
        lines.append("今日對話:{} 則".format(
            "查不到" if today_chats is None else today_chats))

        # 4. 連續幾天沒有對話
        quiet_days = await _days_since_last_conversation(runtime)
        if quiet_days is not None:
            lines.append("距離最後一次對話:{} 天".format(quiet_days))
            if quiet_days >= QUIET_DAYS_THRESHOLD:
                # 中性陳述,不做推測——可能是系統壞了,也可能是他狀態不好
                problems.append("已 {} 天沒有對話紀錄".format(quiet_days))

        # 5. 多久沒備份
        backup_days = await _days_since_last_backup(runtime)
        if backup_days is None:
            lines.append("備份:沒有紀錄")
            problems.append("從來沒有備份過")
        else:
            lines.append("距離最後一次備份:{} 天".format(backup_days))
            if backup_days >= BACKUP_STALE_DAYS:
                problems.append("已 {} 天沒有備份".format(backup_days))

        report = "\n".join(lines)
        print(report)

        if problems:
            print("\n需要注意:")
            for problem in problems:
                print("  - {}".format(problem))
            await notifier.notify_owner(
                runtime,
                notifier.KIND_UNEXPECTED,
                "每日健康檢查:\n{}".format("\n".join(problems)),
            )
        else:
            print("\n一切正常")
    finally:
        await runtime.aclose()

    return 1 if problems else 0


async def _days_since_last_conversation(runtime):
    rows = await runtime.db.select(
        "conversations",
        {"select": "created_at", "order": "created_at.desc", "limit": "1"},
    )
    return _days_since(rows[0]["created_at"]) if rows else None


async def _days_since_last_backup(runtime):
    rows = await runtime.db.select(
        "usage_log",
        {
            "kind": "eq.backup",
            "select": "created_at",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return _days_since(rows[0]["created_at"]) if rows else None




if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
