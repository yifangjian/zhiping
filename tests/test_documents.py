"""檔案內容的短期記憶(app/services/documents.py)。

這組測試對應一個實機發現的 bug:

    他:[檔案:某某.docx]
    知平:這段故事蠻細膩的…              ← 讀到了
    他:你可以幫我整理那份 word 的大綱嗎
    知平:主要內容我大致抓一下重點…       ← 這是編的

檔案內容只存在於收到檔案的那一輪,追問時模型手上只有檔名,就拿自己上一則的
摘要去編。使用者一眼就看得出不對。

測試資料全為虛構。
"""

import io
import time

from app.services.chat import build_user_content
from app.services.documents import MAX_RECALL_CHARS, TTL_SECONDS, DocumentCache
from tests.fakes import make_message, make_runtime
from tests.test_attachments import ContentLine, attachment, make_docx

USER = "U-fictional-test-user"


def test_存進去就拿得回來():
    cache = DocumentCache()
    cache.put(USER, "報告.docx", "掃氣箱著火的處理程序")

    note = cache.recall_note(USER)

    assert "報告.docx" in note
    assert "掃氣箱著火的處理程序" in note


def test_沒存過就回_None():
    assert DocumentCache().recall_note(USER) is None


def test_過期就不再帶入():
    """追問幾乎都發生在傳檔後幾分鐘內,帶太久只是白花 token。"""
    cache = DocumentCache()
    cache.put(USER, "報告.docx", "內容")
    cache._documents[USER].stored_at = time.time() - TTL_SECONDS - 1

    assert cache.recall_note(USER) is None


def test_帶回時有長度上限():
    cache = DocumentCache()
    cache.put(USER, "手冊.pdf", "很長的內容" * 2000)

    note = cache.recall_note(USER)

    assert len(note) < MAX_RECALL_CHARS + 200


def test_不同使用者互不干擾():
    cache = DocumentCache()
    cache.put(USER, "甲.docx", "甲的內容")
    cache.put("U-another", "乙.docx", "乙的內容")

    assert "甲的內容" in cache.recall_note(USER)
    assert "乙的內容" in cache.recall_note("U-another")


async def test_傳檔之後追問接得上():
    """這就是那個 bug 的迴歸測試。"""
    data = make_docx(["小時候跟著家人去釣鯽魚", "後來就很少去了"])
    runtime = make_runtime(line=ContentLine(content=data))

    # 第一輪:傳檔案
    await build_user_content(
        runtime, [attachment("file", file_name="來釣鯽魚吧.docx")]
    )

    # 第二輪:只有文字,沒有再傳檔案
    content = await build_user_content(
        runtime, [make_message(text="你可以幫我整理那份 word 的大綱嗎")]
    )

    assert "來釣鯽魚吧.docx" in content
    assert "釣鯽魚" in content  # 內容真的帶回來了,不用靠猜


async def test_又傳新檔案時不會混到舊的():
    runtime = make_runtime(line=ContentLine(content=make_docx(["第一份的內容"])))
    await build_user_content(runtime, [attachment("file", file_name="第一份.docx")])

    runtime.line.content = make_docx(["第二份的內容"])
    content = await build_user_content(
        runtime, [attachment("file", file_name="第二份.docx")]
    )

    assert "第二份的內容" in content
    # 新檔案這一輪不該把舊的也塞進去
    assert "第一份的內容" not in content


async def test_讀不了的檔案不會被記住():
    """讀不了就沒有內容可帶,別讓後續回合以為手上有東西。"""
    runtime = make_runtime(line=ContentLine(content=b"not a real docx"))

    await build_user_content(runtime, [attachment("file", file_name="壞的.docx")])

    assert runtime.documents.recall_note(USER) is None


# --- 存進資料庫、可查看、可刪除 ---


async def test_檔案內容會存進資料庫():
    """行程內快取只是省一次查詢,真正的保存在資料庫——伺服器重啟不該讓他的作業消失。"""
    runtime = make_runtime(line=ContentLine(content=make_docx(["實習報告內容"])))

    await build_user_content(runtime, [attachment("file", file_name="報告.docx")])

    saved = [r for r in runtime.db.rows if r.get("file_name")]
    assert saved and saved[0]["file_name"] == "報告.docx"
    assert "實習報告內容" in saved[0]["content"]


async def test_快取過期後仍能從資料庫找回():
    from app.services.chat import _recall_document

    runtime = make_runtime(line=ContentLine(content=make_docx(["釣鯽魚的故事"])))
    await build_user_content(runtime, [attachment("file", file_name="來釣鯽魚吧.docx")])

    # 模擬重啟或過期:清掉行程內快取
    runtime.documents.clear(USER)
    runtime.db.documents = [
        {"id": "d1", "file_name": "來釣鯽魚吧.docx", "content": "釣鯽魚的故事", "char_count": 6}
    ]

    note = await _recall_document(runtime, USER, "幫我整理那份 word 的大綱")

    assert note is not None and "釣鯽魚的故事" in note


async def test_沒提到檔案就不多做一次查詢():
    """每則訊息都查一次資料庫是白花時間,而時間就是 reply token。"""
    from app.services.chat import _recall_document

    runtime = make_runtime()
    before = len(runtime.db.select_params)

    note = await _recall_document(runtime, USER, "今天好累")

    assert note is None
    assert len(runtime.db.select_params) == before


# --- 不要一直把檔案內容拖出來講 ---


async def test_沒提到檔案就不再帶入內容():
    """實機發現:傳檔之後 30 分鐘內每則訊息都被塞進整份檔案,
    使用者說想聊別的,知平還是一直繞回去講那份檔案。
    """
    from app.services.chat import _recall_document

    runtime = make_runtime(line=ContentLine(content=make_docx(["釣鯽魚的故事"])))
    await build_user_content(runtime, [attachment("file", file_name="來釣鯽魚吧.docx")])

    # 傳檔後的下一句還在寬限內,會帶
    assert await _recall_document(runtime, USER, "嗯嗯") is not None
    # 再下一句就不帶了
    assert await _recall_document(runtime, USER, "我們聊點別的吧") is None
    assert await _recall_document(runtime, USER, "今天機艙好熱") is None


async def test_明講到檔案就還是帶得回來():
    from app.services.chat import _recall_document

    runtime = make_runtime(line=ContentLine(content=make_docx(["釣鯽魚的故事"])))
    await build_user_content(runtime, [attachment("file", file_name="來釣鯽魚吧.docx")])
    await _recall_document(runtime, USER, "嗯嗯")          # 用掉寬限
    await _recall_document(runtime, USER, "聊點別的")       # 不帶

    # 但他重新問起就要找得回來
    note = await _recall_document(runtime, USER, "剛剛那份檔案幫我抓個大綱")
    assert note is not None and "釣鯽魚的故事" in note


async def test_明講不會消耗寬限():
    """他一直在問這份檔案的話,不該因為問太多次就被切掉。"""
    from app.services.chat import _recall_document

    runtime = make_runtime(line=ContentLine(content=make_docx(["內容"])))
    await build_user_content(runtime, [attachment("file", file_name="報告.docx")])

    for _ in range(3):
        assert await _recall_document(runtime, USER, "那份報告的重點是什麼") is not None
    # 寬限還在,因為前面都是明講
    assert await _recall_document(runtime, USER, "嗯") is not None
