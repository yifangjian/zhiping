"""OpenAI 用戶端封裝。

只暴露「給一串 messages、回一段文字」這一件事,把模型名稱、token 上限、重試策略
都關在這裡,之後要換模型或加 function calling(Phase 4)只改這個檔案。
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    """回覆內容與這次呼叫的用量。用量要寫進 usage_log 做成本追蹤。"""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 這次有沒有動用網路查詢。一次搜尋的 input token 約是一般對話的 40 倍,
    # 要單獨記下來才看得出成本跑到哪去(見 docs/DESIGN.md 第 4 節)
    used_search: bool = False

# 弱網路環境的重試策略(見 docs/DESIGN.md 5.6.2):
# SDK 內建指數退避,重試 2 次。注意重試會吃掉時間預算,
# Phase 2 會在整個回覆流程外再包一層總體 timeout(建議 12s)。
MAX_RETRIES = 2


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout: float = 8.0,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key, timeout=timeout, max_retries=MAX_RETRIES
        )
        self._model = model
        self._max_tokens = max_tokens

    async def aclose(self) -> None:
        await self._client.close()

    async def converse(
        self,
        instructions: str,
        messages: List[Dict[str, Any]],
        allow_search: bool = True,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        """對話用的呼叫,可以讓模型自己決定要不要查資料。

        走 Responses API 而不是 Chat Completions,因為 web search 是 OpenAI 託管的
        工具——模型判斷需要就自己查完再回答,一次往返解決。若改用 function calling
        自己接搜尋,一題要三次往返,以本專案的時間預算(見 5.2)根本來不及。

        **搜尋的取捨**:規格 5.2 建議給搜尋一個獨立的 6 秒 timeout,但託管工具是在
        OpenAI 那端執行的,我們切不進去。能控制的只有整體 timeout(見 chat.py),
        以及在 system prompt 裡講清楚什麼時候才該查——實測閒聊 2.3 秒不查、
        知識性問題 4.7 秒有查,判斷是準的。

        不使用串流:串流在衛星網路下中斷率高,LINE 也無法逐字顯示(5.6.1)。
        """
        response = await self._client.responses.create(
            model=self._model,
            instructions=instructions,
            input=messages,
            tools=[{"type": "web_search"}] if allow_search else [],
            max_output_tokens=max_tokens or self._max_tokens,
            temperature=0.8,
        )

        used_search = any(item.type == "web_search_call" for item in response.output)
        usage = getattr(response, "usage", None)

        result = ChatResult(
            text=(response.output_text or "").strip(),
            model=response.model,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            used_search=used_search,
        )
        logger.info(
            "OpenAI converse 完成:model=%s input=%s output=%s 查詢=%s",
            result.model,
            result.prompt_tokens,
            result.completion_tokens,
            used_search,
        )
        return result

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        temperature: float = 0.8,
    ) -> ChatResult:
        """結構化任務用的呼叫(目前只有記憶抽取)。

        抽取的輸出要被程式 parse,不是給人看的,所以用 Chat Completions 的
        json_object 模式、溫度調低,也不需要任何工具。
        """
        extra: Dict[str, Any] = {}
        if json_mode:
            extra["response_format"] = {"type": "json_object"}

        response = await self._client.chat.completions.create(
            model=model or self._model,
            messages=messages,
            max_tokens=max_tokens or self._max_tokens,
            # 陪伴型對話要自然、不要每次都同一句,溫度稍高一點
            temperature=temperature,
            **extra,
        )

        usage = getattr(response, "usage", None)
        content = response.choices[0].message.content or ""

        result = ChatResult(
            text=content.strip(),
            model=response.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
        logger.info(
            "OpenAI chat 完成:model=%s prompt_tokens=%s completion_tokens=%s",
            result.model,
            result.prompt_tokens,
            result.completion_tokens,
        )
        return result
