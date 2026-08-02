"""記住使用者剛傳過來的檔案內容,讓他可以接著問。

**這個模組存在的原因是一個實機發現的 bug**:

    他:[檔案:某某.docx]
    知平:這段故事蠻細膩的…              ← 讀到了,回得很好
    他:你可以幫我整理那份 word 的大綱嗎
    知平:主要內容我大致抓一下重點…       ← 這是編的

檔案內容只存在於收到檔案的那一輪。對話紀錄裡只留「[檔案:某某.docx]」這個標記,
所以下一句追問時模型手上只有檔名,就拿自己上一則的摘要去編。

**為什麼不把內容存進對話紀錄**:一份文件動輒幾千字,會把短期脈絡的字數上限
整個吃掉,其他對話全被擠出去;記憶抽取也會把文件內容當成「關於他的事實」抽走,
但那可能只是他寫的一篇小說。

**為什麼是行程內快取**:追問幾乎都發生在傳檔之後幾分鐘內,存在記憶體就夠。
這與等待窗、待確認的刪除請求是同一類狀態——重啟會消失,而重啟後他重傳一次即可。
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 超過這段時間就不再自動帶入。追問通常緊接著發生,帶太久只是白花 token。
TTL_SECONDS = 30 * 60

# 帶回上下文時的長度上限。比首次讀取的 6000 字短——
# 首次要完整,後續只是為了接得上話。
MAX_RECALL_CHARS = 3000


@dataclass
class Document:
    file_name: str
    text: str
    stored_at: float
    # 傳檔之後的第一句話通常還在講這份檔案,即使沒有明講「那份 word」。
    # 用完就沒有,之後一律要他提到檔案才帶入。
    grace_turns: int = 1

    def is_fresh(self, now: float) -> bool:
        return now - self.stored_at <= TTL_SECONDS


class DocumentCache:
    """每個使用者只留最近一份檔案。同時討論兩份文件的情況很罕見。"""

    def __init__(self) -> None:
        self._documents: Dict[str, Document] = {}

    def put(self, line_user_id: str, file_name: str, text: str) -> None:
        self._documents[line_user_id] = Document(
            file_name=file_name, text=text, stored_at=time.time()
        )
        logger.info("記住檔案「%s」的內容,共 %s 字", file_name, len(text))

    def get(self, line_user_id: str) -> Optional[Document]:
        document = self._documents.get(line_user_id)
        if document is None:
            return None
        if not document.is_fresh(time.time()):
            self._documents.pop(line_user_id, None)
            logger.info("檔案「%s」的內容已過期", document.file_name)
            return None
        return document

    def clear(self, line_user_id: str) -> None:
        self._documents.pop(line_user_id, None)

    def recall_note(self, line_user_id: str, mentioned: bool = True) -> Optional[str]:
        """組出要放進 prompt 的一段話,沒有可用的檔案就回 None。

        `mentioned` 是「這句話看起來在講檔案」。**這個判斷不能省**——
        第一版只在資料庫那條路徑判斷,快取這條沒判斷,結果傳檔之後的 30 分鐘內
        每一則訊息都被塞進整份檔案內容,使用者說想聊別的,知平還是一直繞回去講那份檔案。

        沒提到檔案時只放行一次(傳檔後的下一句),之後就要他明講。
        """
        document = self.get(line_user_id)
        if document is None:
            return None

        if not mentioned:
            if document.grace_turns <= 0:
                return None
            document.grace_turns -= 1

        text = document.text
        if len(text) > MAX_RECALL_CHARS:
            text = text[:MAX_RECALL_CHARS]

        return (
            "(他稍早傳過檔案「{}」,內容如下。如果他在問這份檔案就用這些內容回答)"
            "\n---\n{}\n---".format(document.file_name, text)
        )
