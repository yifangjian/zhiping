"""把對話、記憶與使用者狀態匯出成 JSON。

    python scripts/backup.py

**輸出一定在專案資料夾之外**(預設 `~/zhiping-backups/`,可用 BACKUP_DIR 覆寫)。

這不是偏好問題,是安全設計(見 docs/DESIGN.md 8.3):專案資料夾是 git repo,
備份檔內含使用者完整對話紀錄,屬高度敏感個資。放在 repo 裡只靠 .gitignore 保護,
一次誤操作(`git add -f`、改錯 .gitignore、換一台電腦 clone 後忘了設定)就可能
推上 GitHub。放在 repo 外則**物理上不可能**被 commit。

腳本會拒絕寫進專案資料夾底下,即使使用者把 BACKUP_DIR 指過去。

給專案擁有者的提醒:備份檔在本機仍然是敏感資料。電腦送修、轉手或共用之前,
請先處理這些檔案。
"""

import datetime
import json
import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_DIR = "~/zhiping-backups"

TABLES = ["conversations", "memories", "user_state"]


def resolve_backup_dir() -> pathlib.Path:
    raw = os.getenv("BACKUP_DIR", "").strip() or DEFAULT_BACKUP_DIR
    target = pathlib.Path(raw).expanduser().resolve()

    # 就算設定寫錯也不讓備份落進 repo。這道檢查比 .gitignore 可靠,
    # 因為它擋的是「檔案根本不會出現在這裡」,而不是「出現了但希望不要被加入」。
    if target == ROOT or ROOT in target.parents or target.is_relative_to(ROOT):
        raise SystemExit(
            "錯誤:備份目錄 {} 在專案資料夾內。\n"
            "備份檔含完整對話紀錄,必須放在 repo 之外,"
            "否則一次誤操作就可能推上 GitHub。".format(target)
        )
    return target


def main() -> int:
    load_dotenv(ROOT / ".env")

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url or "[YOUR-PASSWORD]" in database_url:
        print("錯誤:.env 裡沒有可用的 DATABASE_URL", file=sys.stderr)
        return 1

    backup_dir = resolve_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today().isoformat()
    path = backup_dir / "zhiping-backup-{}.json".format(today)

    payload = {"exported_at": datetime.datetime.now().isoformat(), "tables": {}}
    counts = {}

    with psycopg.connect(database_url, connect_timeout=15) as conn:
        for table in TABLES:
            with conn.cursor() as cur:
                cur.execute("select * from {} order by 1".format(table))
                columns = [c.name for c in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            payload["tables"][table] = rows
            counts[table] = len(rows)

        # 記一筆,健康檢查靠它判斷「多久沒備份了」(見 8.3 的提醒機制)
        conn.execute(
            "insert into usage_log (kind, model) values ('backup', %s)", (today,)
        )

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)

    # 備份檔只有擁有者該讀得到
    os.chmod(path, 0o600)

    print("備份完成:{}".format(path))
    for table, count in counts.items():
        print("  {:15} {} 筆".format(table, count))
    print("\n提醒:這個檔案含完整對話紀錄,屬敏感個資。")
    print("電腦送修、轉手或共用前請先處理掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
