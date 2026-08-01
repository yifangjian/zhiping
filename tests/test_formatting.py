"""送進 LINE 前的純文字整理(app/services/formatting.py)。

LINE 不渲染 Markdown。這些測試守的是「使用者手機上不會看到一堆星號和網址」。
"""

from app.services.formatting import sanitize_for_line


def test_移除搜尋工具的引用連結():
    """web search 會照自己的慣例在句子裡插引用,prompt 壓不住,只能程式處理。"""
    raw = "固定費約 1.5 萬美金 ([pancanal.com](https://pancanal.com/tariff)) ,另外還有容量費。"

    assert sanitize_for_line(raw) == "固定費約 1.5 萬美金 ,另外還有容量費。"


def test_移除私有區字元包住的引用標記():
    """搜尋工具會用 Unicode 私有使用區的字元把引用標記包起來。

    那些字元在 LINE 上顯示成豆腐,而且去掉之後會留下 citeturn0news40 這種亂碼。
    實機測試時真的漏到回覆裡過。
    """
    raw = "整體幾十萬美元比較常見 citeturn0news40"

    assert sanitize_for_line(raw) == "整體幾十萬美元比較常見"


def test_一般連結只留文字():
    raw = "可以看 [勞動部公告](https://example.gov.tw/a) 的說明"

    assert sanitize_for_line(raw) == "可以看 勞動部公告 的說明"


def test_移除粗體與行內程式碼():
    raw = "常見原因是 **活塞環磨損**,還有 `scavenge box` 積碳"

    assert sanitize_for_line(raw) == "常見原因是 活塞環磨損,還有 scavenge box 積碳"


def test_移除標題並把清單換成全形點():
    raw = "## 原因\n- 活塞環磨損\n- 積碳沒清"

    assert sanitize_for_line(raw) == "原因\n・活塞環磨損\n・積碳沒清"


def test_壓掉多餘空行與行尾空白():
    raw = "第一句  \n\n\n\n第二句"

    assert sanitize_for_line(raw) == "第一句\n\n第二句"


def test_一般聊天內容不受影響():
    """整理只該處理格式,不該動到內容。"""
    raw = "聽起來真的很累,今天特別忙嗎?"

    assert sanitize_for_line(raw) == raw


def test_空字串():
    assert sanitize_for_line("") == ""
    assert sanitize_for_line("   \n  ") == ""
