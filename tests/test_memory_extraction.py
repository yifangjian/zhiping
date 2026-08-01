"""記憶抽取流程(docs/DESIGN.md 3.2)。

抽取的輸出要被程式 parse,而模型的輸出不可信任。這裡的重點是:
不合規的項目要被丟掉,而不是讓整批失敗或把髒資料寫進資料庫。

測試資料全為虛構。
"""

import json

from app.services.memory_extraction import (
    EXTRACTION_THRESHOLD,
    maybe_extract,
    parse_extraction,
)
from tests.fakes import FakeDB, FakeOpenAI, make_runtime


def valid_payload():
    return {
        "new_memories": [
            {"category": "context", "content": "他在學排氣閥的原理", "importance": 4}
        ],
        "updates": [{"id": "m1", "content": "他現在在越南靠港", "importance": 3}],
        "deactivate": ["m2"],
    }


def test_解析正常的輸出():
    parsed = parse_extraction(json.dumps(valid_payload()))

    assert parsed["new_memories"][0]["content"] == "他在學排氣閥的原理"
    assert parsed["updates"][0]["id"] == "m1"
    assert parsed["deactivate"] == ["m2"]


def test_容忍模型包上程式碼圍欄():
    raw = "```json\n{}\n```".format(json.dumps(valid_payload()))

    assert parse_extraction(raw)["deactivate"] == ["m2"]


def test_壞掉的_JSON_回_None():
    """回 None 代表這批對話不標記已處理,下次連同新對話再試一次。"""
    assert parse_extraction("我覺得他喜歡泡麵") is None
    assert parse_extraction("") is None


def test_不合規的分類會被丟掉而不是整批失敗():
    raw = json.dumps(
        {
            "new_memories": [
                {"category": "心情", "content": "他今天很累", "importance": 3},
                {"category": "event", "content": "他昨天靠港了", "importance": 3},
            ],
            "updates": [],
            "deactivate": [],
        }
    )

    parsed = parse_extraction(raw)

    assert len(parsed["new_memories"]) == 1
    assert parsed["new_memories"][0]["content"] == "他昨天靠港了"


def test_importance_超出範圍會被夾回_1_到_5():
    raw = json.dumps(
        {
            "new_memories": [
                {"category": "event", "content": "a", "importance": 99},
                {"category": "event", "content": "b", "importance": -3},
                {"category": "event", "content": "c", "importance": "高"},
            ],
            "updates": [],
            "deactivate": [],
        }
    )

    parsed = parse_extraction(raw)

    assert [m["importance"] for m in parsed["new_memories"]] == [5, 1, 3]


def test_缺欄位時不會爆炸():
    parsed = parse_extraction(json.dumps({"new_memories": None}))

    assert parsed == {"new_memories": [], "updates": [], "deactivate": []}


async def test_未達門檻不呼叫_API():
    """每則對話都抽取一次會讓成本失控(見 docs/DESIGN.md 第 4 節)。"""
    db = FakeDB()
    db.unextracted = [{"id": "c1", "role": "user", "content": "嗨"}]
    runtime = make_runtime(db=db)

    await maybe_extract(runtime, "U-test")

    assert runtime.openai.calls == []


async def test_達門檻就抽取並寫入記憶():
    db = FakeDB(memories=[{"id": "m1", "category": "context", "content": "舊的處境"}])
    db.unextracted = [
        {"id": "c{}".format(i), "role": "user", "content": "第 {} 句".format(i)}
        for i in range(EXTRACTION_THRESHOLD)
    ]
    runtime = make_runtime(
        db=db, openai=FakeOpenAI(response=json.dumps(valid_payload()))
    )

    await maybe_extract(runtime, "U-test")

    # 抽取用較便宜的模型、要求 JSON 輸出、溫度調低
    assert runtime.openai.options[0]["model"] == runtime.settings.openai_extract_model
    assert runtime.openai.options[0]["json_mode"] is True
    assert runtime.openai.options[0]["temperature"] < 0.5
    assert db.created_memories[0]["content"] == "他在學排氣閥的原理"
    # 更新既有記憶而不是再新增一條
    assert any(table == "memories" for table, _, _ in db.updates)
    # 處理過的對話要標記,避免下次重複抽取
    assert any(
        params.get("id", "").startswith("in.") and values.get("extracted") is True
        for table, params, values in db.updates
        if table == "conversations"
    )
    # 用量要記到 usage_log
    assert any(u["kind"] == "extract" for u in db.usage)


async def test_使用者刪掉過的內容會被列為不要再記錄():
    """對話紀錄裡還留著他當初講過的話,不擋的話同一件事會被再記一次。"""
    db = FakeDB()
    db.audits = [{"before_content": "他喜歡吃泡麵"}]
    db.unextracted = [
        {"id": "c{}".format(i), "role": "user", "content": "x"}
        for i in range(EXTRACTION_THRESHOLD)
    ]
    runtime = make_runtime(
        db=db, openai=FakeOpenAI(response=json.dumps(valid_payload()))
    )

    await maybe_extract(runtime, "U-test")

    prompt = runtime.openai.calls[0][-1]["content"]
    assert "使用者要求忘記的內容" in prompt
    assert "他喜歡吃泡麵" in prompt


async def test_抽取失敗不會影響使用者():
    db = FakeDB()
    db.unextracted = [
        {"id": "c{}".format(i), "role": "user", "content": "x"}
        for i in range(EXTRACTION_THRESHOLD)
    ]
    runtime = make_runtime(db=db, openai=FakeOpenAI(error=RuntimeError("boom")))

    # 不該往外丟例外——它跑在背景,沒有人接得住
    await maybe_extract(runtime, "U-test")

    assert db.created_memories == []


async def test_模型回傳不存在的_id_不會寫錯資料():
    db = FakeDB(memories=[])
    db.unextracted = [
        {"id": "c{}".format(i), "role": "user", "content": "x"}
        for i in range(EXTRACTION_THRESHOLD)
    ]
    payload = {
        "new_memories": [],
        "updates": [{"id": "不存在的id", "content": "亂改", "importance": 3}],
        "deactivate": ["也不存在"],
    }
    runtime = make_runtime(db=db, openai=FakeOpenAI(response=json.dumps(payload)))

    await maybe_extract(runtime, "U-test")

    assert not any(table == "memories" for table, _, _ in db.updates)
