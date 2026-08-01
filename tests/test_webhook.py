"""webhook 端點:驗簽把關,以及「立即回 200、工作丟背景」的行為。"""

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.test_line_client import sign, text_event


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def submitted(client):
    """攔截丟進等待窗的訊息,不讓測試真的去呼叫外部服務。"""
    received = []

    async def fake_submit(runtime, message):
        received.append(message)

    client.app.state.runtime.batcher.submit = fake_submit
    return received


def post_webhook(client, events, signature=None):
    body = json.dumps({"destination": "U-bot", "events": events}).encode()
    headers = {"x-line-signature": signature or sign(body)}
    return client.post("/line/webhook", content=body, headers=headers)


def test_健康檢查(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_簽章錯誤時拒絕(client, submitted):
    response = post_webhook(client, [text_event()], signature="not-a-signature")

    assert response.status_code == 400
    assert submitted == []  # 驗簽失敗就不該碰到任何處理流程


def test_文字訊息會被丟到背景處理(client, submitted):
    response = post_webhook(client, [text_event()])

    assert response.status_code == 200
    # TestClient 會在回應送出後執行背景任務
    assert len(submitted) == 1
    assert submitted[0]["text"] == "今天很累"


def test_不支援的事件型別不會中斷整批處理(client, submitted):
    response = post_webhook(
        client,
        [
            text_event(type="follow"),
            text_event(webhookEventId="02", message={"type": "text", "text": "在嗎"}),
        ],
    )

    assert response.status_code == 200
    assert [m["text"] for m in submitted] == ["在嗎"]


def test_一次送達多則訊息會全部交給等待窗合併(client, submitted):
    """使用者斷線後重新連上,LINE 會把累積的訊息一次送達(見 docs/DESIGN.md 5.5)。"""
    events = [
        text_event(webhookEventId="e{}".format(i), message={"type": "text", "text": str(i)})
        for i in range(5)
    ]

    response = post_webhook(client, events)

    assert response.status_code == 200
    assert len(submitted) == 5
