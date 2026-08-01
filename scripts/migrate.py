"""依序執行 migrations/ 底下的 SQL。

用法:
    python scripts/migrate.py

需要 .env 裡的 DATABASE_URL(Supabase 儀表板的 Connect → Session pooler → URI)。
這是開發用工具,不會被應用程式載入——正式流程走 PostgREST,不直連資料庫。

每個 migration 都寫成可重複執行(全部用 if not exists),所以這個腳本不做
「已套用過」的紀錄:重跑一次是安全的,也省掉一張 schema_migrations 表。
"""

import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"


def main() -> int:
    load_dotenv(ROOT / ".env")

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
        for path in files:
            sql = path.read_text(encoding="utf-8")
            # 一個檔案一個 transaction:中途失敗就整份回滾,不會留下半套 schema
            with conn.transaction():
                conn.execute(sql)
            print("已套用 {}".format(path.name))

    print("完成,共 {} 份 migration".format(len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
