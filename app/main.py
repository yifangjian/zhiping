"""FastAPI 應用程式與 LINE webhook 端點。

這個檔案只做三件事:驗簽、把事件分類、把工作丟到背景。任何會花時間的事情都不
應該出現在這裡——LINE 若沒有在短時間內收到 HTTP 200 就會判定失敗並重送,
使用者會收到重複回覆(見 docs/DESIGN.md 5.4)。
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from app import __version__
from app.clients.line import (
    extract_follow_event,
    extract_text_event,
    verify_signature,
)
from app.config import load_settings
from app.runtime import Runtime
from app.services.onboarding import handle_follow

logger = logging.getLogger(__name__)

settings = load_settings()


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx 每次請求都會印一行 INFO,雜訊太多
    logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    if not settings.is_ready:
        logger.warning(
            "以不完整的設定啟動,webhook 將無法運作。缺少:%s",
            ", ".join(settings.missing),
        )

    app.state.runtime = Runtime.create(settings)
    logger.info("知平 %s 已啟動(模型:%s)", __version__, settings.openai_chat_model)
    try:
        yield
    finally:
        await app.state.runtime.aclose()
        logger.info("知平已關閉")


app = FastAPI(title="zhiping", version=__version__, lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """部署平台的存活檢查。刻意不呼叫外部服務,保持極輕量。"""
    return {"status": "ok", "version": __version__, "configured": settings.is_ready}


@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(default=""),
) -> Response:
    # 驗簽必須用原始 bytes,不能用 parse 過的 JSON 重組
    body = await request.body()
    if not verify_signature(settings.line_channel_secret, body, x_line_signature):
        logger.warning("webhook 驗簽失敗,拒絕請求")
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid payload")

    runtime: Runtime = request.app.state.runtime

    for event in payload.get("events", []):
        message = extract_text_event(event)
        if message is not None:
            # 丟背景處理,先讓這個 request 回 200。
            # 訊息不會立刻被回覆——batcher 會等一個 3 秒的窗,把連續送出的
            # 訊息合併成一則回覆(見 app/services/inbox.py)。
            background_tasks.add_task(runtime.batcher.submit, runtime, message)
            continue

        follow = extract_follow_event(event)
        if follow is not None:
            background_tasks.add_task(handle_follow, runtime, follow)
            continue

        # 其餘型別(貼圖、圖片、封鎖等)目前不處理,只留下紀錄
        logger.info("略過事件型別:%s", event.get("type"))

    # 回應內容 LINE 不看,重點是狀態碼與速度
    return Response(status_code=200, content="OK", media_type="text/plain")
