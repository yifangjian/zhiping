"""記憶的可見性與可控性指令(docs/DESIGN.md 3.4)。

最重要的兩件事:
1. 使用者說要刪,就真的刪得掉(而且是二次確認,不會誤刪)
2. 一般閒聊不會被誤判成指令

測試資料全為虛構。
"""

import pytest

from app.services.commands import CommandHandler, PendingForget
from tests.fakes import FakeDB, make_runtime

pytestmark = pytest.mark.asyncio

USER = "U-fictional-test-user"

FICTIONAL_MEMORIES = [
    {"id": "m1", "category": "context", "content": "他在學排氣閥的原理", "importance": 5},
    {"id": "m2", "category": "preference", "content": "他喜歡吃泡麵", "importance": 2},
]


def make_handler(memories=None):
    runtime = make_runtime(db=FakeDB(memories=memories if memories is not None else FICTIONAL_MEMORIES))
    return CommandHandler(), runtime


async def test_你記得我什麼會依分類列出():
    handler, runtime = make_handler()

    reply = await handler.handle(runtime, USER, "你記得我什麼")

    # 記憶存的是第三人稱(給模型讀),列給使用者看要換成「你」
    assert "你在學排氣閥的原理" in reply
    assert "你喜歡吃泡麵" in reply
    assert "他在學" not in reply
    assert "目前處境" in reply
    # 要告訴使用者怎麼刪
    assert "忘記" in reply


async def test_沒有記憶時的回應():
    handler, runtime = make_handler(memories=[])

    reply = await handler.handle(runtime, USER, "你記得我什麼")

    assert "還沒記得什麼" in reply


async def test_忘記關鍵字會先列出再等確認():
    handler, runtime = make_handler()

    asked = await handler.handle(runtime, USER, "忘記 泡麵")

    assert "你喜歡吃泡麵" in asked
    assert "排氣閥" not in asked  # 不相符的不該被列出
    # 這時還沒有真的刪
    assert runtime.db.updates == []

    done = await handler.handle(runtime, USER, "好")
    assert "刪掉了" in done
    assert any(table == "memories" for table, _, _ in runtime.db.updates)


async def test_回不要就不刪():
    handler, runtime = make_handler()

    await handler.handle(runtime, USER, "忘記 泡麵")
    reply = await handler.handle(runtime, USER, "不用")

    assert "留著" in reply
    assert runtime.db.updates == []


async def test_確認前講別的就當作沒這回事():
    """待確認狀態不該綁架接下來的對話。"""
    handler, runtime = make_handler()

    await handler.handle(runtime, USER, "忘記 泡麵")
    reply = await handler.handle(runtime, USER, "算了先不講這個,今天好累")

    assert reply is None  # 交給一般對話
    assert runtime.db.updates == []

    # 而且刪除請求已經作廢,再回「好」也不會刪
    assert await handler.handle(runtime, USER, "好") is None




async def test_忘記全部需要二次確認():
    handler, runtime = make_handler()

    asked = await handler.handle(runtime, USER, "忘記全部")
    assert "確定" in asked
    assert runtime.db.updates == []

    done = await handler.handle(runtime, USER, "好")
    assert "都忘了" in done
    assert any(table == "memories" for table, _, _ in runtime.db.updates)


async def test_認得出真人的各種講法():
    """實機測試打臉了第一版:使用者打的是這些句子,一句都沒被辨識到,
    然後 LLM 接手宣稱「已經刪掉了」——什麼都沒發生。
    """
    real_phrasings = [
        "忘記泡麵",
        "忘掉泡麵",
        "忘記泡麵吧",
        "不要記得泡麵",
        "可以忘掉我說喜歡吃泡麵的事情嗎",
        "其實你在記憶裡可以完全把泡麵那條刪掉",
        "幫我把喜歡吃泡麵刪除",
    ]
    for text in real_phrasings:
        handler, runtime = make_handler()
        reply = await handler.handle(runtime, USER, text)
        assert reply is not None and "你喜歡吃泡麵" in reply, text


async def test_確認後連原始對話一起遮蔽():
    """只刪記憶不夠——使用者當初講那句話的對話還在短期脈絡裡,知平照樣講得出來。

    使用者的原話是「連寫出來都不該,而是徹底忘掉」。
    """
    handler, runtime = make_handler()

    await handler.handle(runtime, USER, "忘記泡麵")
    await handler.handle(runtime, USER, "好")

    hides = [
        (params, values)
        for table, params, values in runtime.db.updates
        if table == "conversations" and values.get("hidden") is True
    ]
    assert hides, "確認刪除後應該遮蔽含有該關鍵字的對話"
    assert "泡麵" in hides[0][0]["content"]


async def test_閒聊不會被誤判成刪除指令():
    """判斷依據改成「有沒有記憶真的相符」,而不是有沒有打空格。

    「忘記帶手套了」取出的關鍵字是「帶手套」,沒有任何記憶相符,
    所以交給一般對話,使用者不會收到「沒找到跟『帶手套』有關的記憶」這種怪回應。
    """
    handler, runtime = make_handler()

    for text in ["忘記帶手套了", "我忘記關艙門", "今天差點忘記吃飯", "忘記"]:
        assert await handler.handle(runtime, USER, text) is None, text

    assert runtime.db.updates == []


async def test_明確格式找不到時仍然給回應():
    """打了空格代表他知道自己在下指令,這時沉默會讓他以為系統壞了。"""
    handler, runtime = make_handler()

    reply = await handler.handle(runtime, USER, "忘記 咖啡")

    assert "沒找到" in reply


async def test_待確認的請求會過期():
    """使用者網路差可能隔很久才回話,但一句「好」不該在半小時後意外刪掉東西。"""
    handler, runtime = make_handler()

    handler._pending[USER] = PendingForget(
        memory_ids=["m1"], created_at=0.0  # 1970 年,鐵定過期
    )

    assert await handler.handle(runtime, USER, "好") is None
    assert runtime.db.updates == []


async def test_一般對話不受影響():
    handler, runtime = make_handler()

    assert await handler.handle(runtime, USER, "今天海上風浪很大") is None
    assert await handler.handle(runtime, USER, "") is None


async def test_抱怨知平忘記不是刪除指令():
    """實機踩到的:知平說「你現在都在台灣了」,使用者回「你忘了我在船上實習嗎?」
    結果它列出正確的記憶問要不要刪掉——他在糾正它,它卻要刪掉那條正確的記憶。
    """
    complaints = [
        "你忘了我在船上實習嗎?",
        "你是不是忘記我在船上了",
        "你怎麼忘了我討厭吃泡麵",
        "你又忘了喔",
        "妳忘了我跟你講過的事",
    ]
    for text in complaints:
        handler, runtime = make_handler()
        assert await handler.handle(runtime, USER, text) is None, text
        assert runtime.db.updates == []


async def test_有請求語氣的還是指令():
    """「你可以忘掉那件事嗎」跟「你忘了那件事嗎」意思相反,不能一起擋掉。"""
    handler, runtime = make_handler()

    reply = await handler.handle(runtime, USER, "你可以幫我忘掉泡麵那件事嗎")

    assert reply is not None and "你喜歡吃泡麵" in reply
