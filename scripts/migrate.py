"""依序執行 migrations/ 底下尚未套用的 SQL。

    python scripts/migrate.py
    python scripts/migrate.py --baseline-through 007_backup_kind.sql

需要 .env 裡的 DATABASE_URL(Supabase 儀表板的 Connect → Session pooler → URI)。
這是開發用工具,不會被應用程式載入——正式流程走 PostgREST,不直連資料庫。

**為什麼需要記錄已套用的 migration**:第一版沒有記錄,理由是「每份 migration
都寫成可重複執行」。那個假設在第八份就破了:005 與 007 都在改 usage_log 的
check constraint,007 加了新的分類值。重跑 005 會把 constraint 改回舊版本,
而此時資料表裡已經有新分類的資料——直接違反約束,整個 migration 卡住。

「可重複執行的 SQL」擋得住重複建立,擋不住**後面的 migration 改了同一個東西**。
所以還是要記錄。

--baseline-through 給既有資料庫用:把該檔案(含)以前的 migration 標記為已套用
但不執行,之後的照常跑。第一次導入這套紀錄時會用到。
"""

import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"

TRACKING_TABLE_SQL = """
create table if not exists schema_migrations (
    filename    text primary key,
    applied_at  timestamptz not null default now()
)
"""


def main() -> int:
    load_dotenv(ROOT / ".env")

    baseline_through = None
    if "--baseline-through" in sys.argv:
        index = sys.argv.index("--baseline-through")
        if index + 1 >= len(sys.argv):
            print("錯誤:--baseline-through 後面要接檔名", file=sys.stderr)
            return 1
        baseline_through = sys.argv[index + 1]

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("錯誤:.env 裡沒有 DATABASE_URL", file=sys.stderr)
        return 1
    if "[YOUR-PASSWORD]" in database_url:
        print("錯誤:DATABASE_URL 裡的 [YOUR-PASSWORD] 還沒換成真的密碼", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("migrations/ 底下沒有 .sql 檔")
        return 0

    with psycopg.connect(database_url, connect_timeout=15) as conn:
        conn.execute(TRACKING_TABLE_SQL)
        applied = {
            row[0]
            for row in conn.execute("select filename from schema_migrations").fetchall()
        }

        baselined = 0
        executed = 0
        for path in files:
            if path.name in applied:
                continue

            if baseline_through and path.name <= baseline_through:
                # 標記為已套用但不執行:資料庫早就有這些變更了
                conn.execute(
                    "insert into schema_migrations (filename) values (%s)",
                    (path.name,),
                )
                print("標記為已套用(未執行)  {}".format(path.name))
                baselined += 1
                continue

            sql = path.read_text(encoding="utf-8")
            # 一個檔案一個 transaction:中途失敗就整份回滾,不會留下半套 schema
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "insert into schema_migrations (filename) values (%s)",
                    (path.name,),
                )
            print("已套用  {}".format(path.name))
            executed += 1

    if not executed and not baselined:
        print("沒有需要套用的 migration({} 份都已套用)".format(len(files)))
    else:
        print("完成:執行 {} 份,標記 {} 份".format(executed, baselined))
    return 0


if __name__ == "__main__":
    sys.exit(main())
