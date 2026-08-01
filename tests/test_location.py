"""從對話認出使用者換了地方(docs/DESIGN.md 2.3)。

誤判的代價是知平把時間講錯——他半夜下班說「累死了」得到一句「早安啊」。
所以這裡寧可漏判也不要誤判,測試也照這個方向寫。
"""

import pytest

from app.repositories import user_state
from app.services.location import detect_from_messages, detect_timezone_update


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我現在在越南", "Asia/Ho_Chi_Minh"),
        ("剛到釜山", "Asia/Seoul"),
        ("人在橫濱 好冷", "Asia/Tokyo"),
        ("今天靠港高雄", "Asia/Taipei"),
        ("我在上海這邊", "Asia/Shanghai"),
        ("抵達新加坡了", "Asia/Singapore"),
    ],
)
def test_認得出現在人在哪裡(text, expected):
    assert detect_timezone_update(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "上次在越南買的",          # 過去式
        "之前去過日本",
        "下次應該會到釜山",         # 未來式
        "打算去新加坡玩",
        "越南菜好吃",              # 只有地名,沒有「人在那裡」的語氣
        "韓國那邊的船公司",
        "今天好累",                # 兩者都沒有
        "",
    ],
)
def test_這些都不該改時區(text):
    assert detect_timezone_update(text) is None


def test_一批訊息取最後一次的地點():
    """「早上在高雄,現在到釜山了」應該以釜山為準。"""
    texts = ["早上還在高雄", "開了一整天", "剛到釜山"]

    assert detect_from_messages(texts) == "Asia/Seoul"


def test_地名比對取最長的():
    """避免短地名先攔截掉長地名。"""
    assert user_state.resolve_place("我在台中") == "Asia/Taipei"


def test_當地時間帶有時段描述():
    """模型看到 05:12 不一定意識到那是什麼時刻,看到「清晨」就知道他可能剛值完夜班。"""
    text = user_state.local_time_string("Asia/Taipei")

    assert "週" in text
    assert any(period in text for period in
               ["深夜", "清晨", "上午", "中午", "下午", "晚上"])


def test_時區壞掉時不會爆炸():
    assert user_state.is_valid_timezone("Asia/Taipei") is True
    assert user_state.is_valid_timezone("Mars/Olympus") is False
    assert user_state.is_valid_timezone("") is False
    # 壞掉的時區退回預設,不能讓整個回覆流程掛掉
    assert user_state.local_time_string("Mars/Olympus")
