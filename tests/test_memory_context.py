"""記憶讀取與短期脈絡的組裝(docs/DESIGN.md 3.1、3.5)。

驗的是規格點名的那個典型失敗:使用者說「那個很難」,知平不知道「那個」是什麼,
因為上一輪對話沒進 context。

測試資料全為虛構。
"""

from datetime import datetime, timedelta, timezone

from app.prompts import format_memory_block, to_second_person
from app.repositories import memories as memories_repo
from app.services.chat import build_context_messages, context_notice, handle_batch
from tests.fakes import FakeDB, make_message, make_runtime


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


FICTIONAL_MEMORIES = [
    {"id": "m1", "category": "context", "content": "他在學排氣閥的原理", "importance": 5},
    {"id": "m2", "category": "preference", "content": "他討厭吃苦瓜", "importance": 2},
    {"id": "m3", "category": "relationship", "content": "他有一個姐姐", "importance": 4},
]


async def test_記憶會被組進_system_prompt():
    runtime = make_runtime(db=FakeDB(memories=FICTIONAL_MEMORIES))

    await handle_batch(runtime, [make_message(text="在幹嘛")])

    system_prompt = runtime.openai.instructions[0]
    assert "他在學排氣閥的原理" in system_prompt
    assert "他討厭吃苦瓜" in system_prompt
    # 記憶不能出現在 user message 裡,否則會干擾使用者說的話
    assert "排氣閥" not in runtime.openai.calls[0][-1]["content"]


async def test_短期脈絡以_messages_陣列帶入不塞進_system_prompt():
    history = [
        {"role": "assistant", "content": "那個閥門要先洩壓", "created_at": iso(0.2)},
        {"role": "user", "content": "我在看排氣閥的手冊", "created_at": iso(0.3)},
    ]
    runtime = make_runtime(db=FakeDB(history=history))

    await handle_batch(runtime, [make_message(text="那個很難")])

    messages = runtime.openai.calls[0]
    # 舊 → 新 → 本次訊息。角色設定在 instructions,不在這裡
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "我在看排氣閥的手冊"
    assert messages[-1]["content"] == "那個很難"


async def test_本次訊息不會在脈絡裡重複出現():
    """本次訊息已經寫進資料庫了,撈脈絡時要用 created_at 把它排除掉。"""
    db = FakeDB()
    runtime = make_runtime(db=db)
    message = make_message(text="那個很難")
    message["created_at"] = "2026-08-01T12:00:00+00:00"

    await handle_batch(runtime, [message])

    conversation_queries = [
        params for table, params in db.select_params if table == "conversations"
    ]
    assert conversation_queries[0]["created_at"] == "lt.2026-08-01T12:00:00+00:00"


def test_隔太久的對話會加上時間標註():
    """否則知平會把隔了三天的對話當成剛剛才講的。"""
    history = [{"role": "user", "content": "昨天好累", "created_at": iso(30)}]

    assert "昨天" in context_notice(history)
    # 標註是「關於這段對話的說明」,不該混進對話本身
    assert [m["role"] for m in build_context_messages(history)] == ["user"]


def test_剛剛的對話不加標註():
    history = [{"role": "user", "content": "剛下班", "created_at": iso(0.5)}]

    assert context_notice(history) is None


async def test_指令的回應不會被寫進對話紀錄():
    """「你記得我什麼」的回應是一份完整記憶清單。

    若存進 conversations,下次抽取會把整份清單重新讀成新記憶——記憶自我複製,
    連使用者剛刪掉的都會復活。這個 bug 在實機測試時真的發生過。
    """
    db = FakeDB(memories=FICTIONAL_MEMORIES)
    runtime = make_runtime(db=db)
    message = make_message(text="你記得我什麼")
    message["conversation_id"] = "conv-1"

    await handle_batch(runtime, [message])

    # 列給使用者看要換成「你」,不是存進資料庫的第三人稱
    assert "你在學排氣閥的原理" in runtime.line.replies[0][1]
    # 沒有任何 assistant 訊息被寫進對話紀錄
    assert db.conversations == []
    # 而且使用者的指令本身被標記為已抽取,免得指令內容又被抽一次
    assert any(
        table == "conversations" and values.get("extracted") is True
        for table, _, values in db.updates
    )
    # 指令不經過 LLM
    assert runtime.openai.calls == []


def test_列給使用者看時換成第二人稱():
    """記憶存第三人稱是給模型讀的。直接列給使用者會變成「他在學排氣閥」,像在講別人。"""
    assert to_second_person("使用者在學排氣閥的原理") == "你在學排氣閥的原理"
    # 舊格式(開頭是「他」)也要能轉
    assert to_second_person("他討厭吃苦瓜") == "你討厭吃苦瓜"


def test_句中指涉別人的他不會被換掉():
    """「使用者的室友很會煮菜,他常做東西給大家吃」——那個「他」是室友。

    這就是為什麼抽取時規定用「使用者」稱呼本人,而不是靠字串替換猜。
    """
    result = to_second_person("使用者的室友很會煮菜,他常做東西給大家吃")

    assert result == "你的室友很會煮菜,他常做東西給大家吃"


def test_記憶區塊依分類分組():
    block = format_memory_block(FICTIONAL_MEMORIES)

    assert "目前處境:" in block
    assert "喜好與習慣:" in block
    assert "重要的人:" in block
    assert "- 他在學排氣閥的原理" in block


def test_沒有記憶時記憶區塊是空的():
    assert format_memory_block([]) == ""


async def test_記憶區塊有字數上限():
    """記憶會無止盡累積,但 system prompt 的長度直接換算成成本與延遲。"""
    many = [
        {
            "id": "m{}".format(i),
            "category": "event",
            "content": "虛構的記憶內容" * 20,  # 每條約 140 字
            "importance": 3,
        }
        for i in range(30)
    ]
    db = FakeDB(memories=many)

    selected = await memories_repo.fetch_active(db, "U-test", max_chars=1500)

    assert 0 < len(selected) < 30
    assert sum(len(m["content"]) for m in selected) <= 1500
