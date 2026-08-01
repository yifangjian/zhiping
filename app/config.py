"""環境變數集中管理。

刻意不使用 pydantic-settings 之類的套件:設定項目不多,一個 dataclass 就夠讀懂,
也少一個相依套件。啟動時就把缺少的必要變數一次報出來,避免上線後才在 webhook 裡炸。
"""

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

try:
    # 本機開發時從 .env 讀取。部署平台(Railway 等)的環境變數優先,
    # load_dotenv 預設不覆蓋既有變數,所以兩邊可以共存。
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - 正式環境不一定需要這個套件
    pass


class ConfigError(RuntimeError):
    """必要環境變數缺漏。"""


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    # 環境變數在部署平台上常被填成空字串,一律視同未設定
    if value is not None:
        value = value.strip()
    return value or None


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("環境變數 {} 必須是整數,目前是 {!r}".format(name, raw))


@dataclass(frozen=True)
class Settings:
    # --- LINE ---
    line_channel_secret: str
    line_channel_access_token: str

    # --- OpenAI ---
    openai_api_key: str
    openai_chat_model: str = "gpt-4.1"
    openai_chat_max_tokens: int = 500
    # 記憶抽取是結構化任務,不需要最強的模型(見 docs/DESIGN.md 第 4 節)
    openai_extract_model: str = "gpt-4.1-mini"
    openai_extract_max_tokens: int = 800

    # --- Supabase ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # --- 維運 ---
    owner_line_user_id: Optional[str] = None
    log_level: str = "INFO"

    # --- 時間預算(秒)。詳見 docs/DESIGN.md 5.2、5.3、5.5 ---
    # 從最後一則訊息抵達算起:等待窗 3s + 產生回覆 9s ≒ 12s,
    # 這是 reply token 還可能有效的上限。單次 OpenAI 呼叫的 timeout 設得比
    # 產生回覆的總預算短,才留得下重試的空間。
    openai_timeout: float = 8.0
    supabase_timeout: float = 3.0
    line_timeout: float = 3.0
    debounce_seconds: float = 3.0
    generation_timeout: float = 9.0

    # --- 用量上限(見 docs/DESIGN.md 第 4 節)---
    # 輕用量方案每月 200 則免費 push,留 20 則緩衝當警戒線
    push_monthly_limit: int = 180
    # 每日對話回覆上限。單一使用者正常用不到,這是「出事時止血」用的
    daily_chat_limit: int = 200
    # 每日記憶抽取次數上限。超過的對話延到隔天處理,記憶晚一天到不影響體驗
    daily_extract_limit: int = 20

    # --- 驗收用,正式環境保持 0 ---
    # 人為延遲回覆,用來實測 reply token 過期後的 push 備援(見 docs/DESIGN.md 5.7)
    debug_reply_delay: float = 0.0

    # 缺漏的必要變數,啟動時列出來提醒(不直接 raise,方便 /health 仍可回應)
    missing: List[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return not self.missing


REQUIRED = [
    "LINE_CHANNEL_SECRET",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "OPENAI_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
]


def load_settings() -> Settings:
    missing = [name for name in REQUIRED if _get(name) is None]
    if missing:
        logger.error("缺少必要環境變數:%s(請參考 .env.example)", ", ".join(missing))

    return Settings(
        line_channel_secret=_get("LINE_CHANNEL_SECRET") or "",
        line_channel_access_token=_get("LINE_CHANNEL_ACCESS_TOKEN") or "",
        openai_api_key=_get("OPENAI_API_KEY") or "",
        openai_chat_model=_get("OPENAI_CHAT_MODEL") or "gpt-4.1",
        openai_chat_max_tokens=_get_int("OPENAI_CHAT_MAX_TOKENS", 500),
        openai_extract_model=_get("OPENAI_EXTRACT_MODEL") or "gpt-4.1-mini",
        openai_extract_max_tokens=_get_int("OPENAI_EXTRACT_MAX_TOKENS", 800),
        supabase_url=(_get("SUPABASE_URL") or "").rstrip("/"),
        supabase_service_role_key=_get("SUPABASE_SERVICE_ROLE_KEY") or "",
        owner_line_user_id=_get("OWNER_LINE_USER_ID"),
        log_level=(_get("LOG_LEVEL") or "INFO").upper(),
        debug_reply_delay=float(_get("DEBUG_REPLY_DELAY_SECONDS") or 0),
        missing=missing,
    )
