"""應用程式的執行期物件。

把三個外部用戶端集中在一個 Runtime 上,由 FastAPI 的 lifespan 建立與關閉。
用戶端共用連線池是有意義的:每次 webhook 都重建 httpx client 會多付一次 TLS
handshake,在衛星網路下那是實打實的幾百毫秒(見 docs/DESIGN.md 5.2 的時間預算)。

背景任務用「明確傳入 Runtime」而不是讀全域變數,是為了讓服務層好測試。
"""

from dataclasses import dataclass

from app.clients.line import LineClient
from app.clients.openai_client import OpenAIClient
from app.clients.supabase import SupabaseClient
from app.config import Settings
from app.services.commands import CommandHandler
from app.services.inbox import MessageBatcher


@dataclass
class Runtime:
    settings: Settings
    line: LineClient
    openai: OpenAIClient
    db: SupabaseClient
    # 等待窗與待確認的刪除請求都是行程內狀態,
    # 必須跟用戶端一樣活得跟應用程式一樣久
    batcher: MessageBatcher
    commands: CommandHandler

    @classmethod
    def create(cls, settings: Settings) -> "Runtime":
        return cls(
            settings=settings,
            line=LineClient(
                access_token=settings.line_channel_access_token,
                timeout=settings.line_timeout,
            ),
            openai=OpenAIClient(
                api_key=settings.openai_api_key,
                model=settings.openai_chat_model,
                max_tokens=settings.openai_chat_max_tokens,
                timeout=settings.openai_timeout,
            ),
            db=SupabaseClient(
                url=settings.supabase_url,
                service_role_key=settings.supabase_service_role_key,
                timeout=settings.supabase_timeout,
            ),
            batcher=MessageBatcher(window_seconds=settings.debounce_seconds),
            commands=CommandHandler(),
        )

    async def aclose(self) -> None:
        # 先停等待窗,免得關閉用戶端之後還有計時器醒來想用它們
        await self.batcher.aclose()
        await self.line.aclose()
        await self.openai.aclose()
        await self.db.aclose()
