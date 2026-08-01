"""測試共用設定。

app.main 在 import 時就會讀環境變數,所以這裡必須在任何 import 之前把假的設定值
塞好。這些值全是虛構的,不是真實憑證。
"""

import os

TEST_CHANNEL_SECRET = "test-channel-secret"

os.environ.setdefault("LINE_CHANNEL_SECRET", TEST_CHANNEL_SECRET)
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

# 明確設成空的:app.config 會 load_dotenv(),開發機上真的有 .env,
# 不擋的話測試會讀到真實設定,結果依「跑測試的人有沒有設這個變數」而變。
os.environ.setdefault("OWNER_LINE_USER_ID", "")
os.environ.setdefault("DEBUG_REPLY_DELAY_SECONDS", "0")
