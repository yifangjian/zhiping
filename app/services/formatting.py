"""把 LLM 的輸出整理成適合 LINE 的純文字。

**為什麼需要這一層**:LINE 的文字訊息不渲染 Markdown。`**粗體**` 在手機上就是
原樣印出星號,看起來很亂。system prompt 裡已經交代不要用 Markdown,但那是「請模型
自律」——尤其動用 web search 之後,模型會照搜尋工具的慣例在句子裡插滿
`([example.com](https://...))` 這種引用連結,prompt 壓不住。

會影響使用者看到什麼的事情,應該由程式保證,不是靠模型配合。
"""

import re

# 搜尋工具的引用格式:整組 ([顯示名](網址)) 直接拿掉,它對使用者沒有意義
CITATION_PATTERN = re.compile(r"\s*\(\[[^\]]*\]\([^)]*\)\)")

# 搜尋工具另一種引用標記,用 Unicode 私有使用區的字元當分隔符包住,
# 像 citeturn0news40。私有區字元在 LINE 上顯示成空白或豆腐,
# 而且去掉分隔符之後會留下「citeturn0news40」這種亂碼,兩者都要清掉。
PRIVATE_USE_PATTERN = re.compile("[\ue000-\uf8ff]")
CITE_TOKEN_PATTERN = re.compile(
    r"cite\s*turn\d+\w*|turn\d+(?:search|news|view|image)\d+"
)

# 一般的 Markdown 連結 [文字](網址) → 只留文字
LINK_PATTERN = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# 粗體、斜體、行內程式碼的標記符號
EMPHASIS_PATTERN = re.compile(r"(\*\*|__|\*|`)")

# 行首的 Markdown 標題與清單符號
HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
BULLET_PATTERN = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)

# 連續三行以上的空行壓成一行空行
BLANK_LINES_PATTERN = re.compile(r"\n{3,}")


def sanitize_for_line(text: str) -> str:
    """移除 Markdown 標記與搜尋引用連結,回傳適合直接送進 LINE 的純文字。"""
    if not text:
        return ""

    # 先清私有區分隔符,否則底下的 citeturn 標記還被包著,比對不到
    cleaned = PRIVATE_USE_PATTERN.sub("", text)
    cleaned = CITE_TOKEN_PATTERN.sub("", cleaned)
    cleaned = CITATION_PATTERN.sub("", cleaned)
    cleaned = LINK_PATTERN.sub(r"\1", cleaned)
    cleaned = HEADING_PATTERN.sub("", cleaned)
    # 條列符號換成中文全形點,那是 LINE 上看起來最自然的寫法
    cleaned = BULLET_PATTERN.sub("・", cleaned)
    cleaned = EMPHASIS_PATTERN.sub("", cleaned)

    # 逐行去掉尾端空白(Markdown 用兩個空白表示換行,在 LINE 上是多餘的)
    cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
    cleaned = BLANK_LINES_PATTERN.sub("\n\n", cleaned)

    return cleaned.strip()
