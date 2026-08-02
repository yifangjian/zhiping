# 從零開始架一個自己的陪伴型 LINE Bot

這份文件假設你**沒寫過程式**,但願意跟著複製貼上。全部做完大約 1.5 小時,其中大半在等網頁載入。

會用到的東西:一台電腦、一個 LINE 帳號、一張信用卡(部署要用,每月約 5 美元)。

如果你只是想改一下讓它變成你的用途,跳到最後一節「[改成你自己的](#改成你自己的)」。

---

## 你要準備的四個帳號

| 服務 | 用途 | 費用 |
|---|---|---|
| LINE Developers | 讓機器人有一個 LINE 帳號 | 免費(每月 200 則主動訊息) |
| Supabase | 存對話與記憶 | 免費方案夠用 |
| OpenAI | 產生回覆 | 每月約 1–3 美元 |
| Railway | 讓它 24 小時運作 | 每月 5 美元起 |

---

## 第一步:把程式碼抓下來

打開「終端機」(Mac 按 Cmd+空白鍵,輸入 Terminal;Windows 用 PowerShell),貼上:

```bash
cd ~/Desktop
git clone https://github.com/yifangjian/zhiping.git
cd zhiping
```

如果它說「git: command not found」,先去 [git-scm.com](https://git-scm.com/downloads) 裝 git。

接著建立執行環境:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Windows 的第二行是 `.venv\Scripts\activate`。

裝完之後,終端機每一行前面會出現 `(.venv)`。**之後每次重開終端機都要再跑一次 `source .venv/bin/activate`**,不然會找不到套件。

---

## 第二步:Supabase(存資料的地方)

1. 到 [supabase.com](https://supabase.com) 註冊,可以直接用 GitHub 帳號登入
2. **New project**,名字隨便取,region 選離你近的(台灣選 Singapore)
3. 會叫你設一組資料庫密碼,**先記下來**,等一下要用

專案建好之後要拿三個值:

**Project URL** — 看瀏覽器網址列:

```
https://supabase.com/dashboard/project/abcdefghijklmnop
                                       └──── 這一段 ────┘
```

把它接成 `https://abcdefghijklmnop.supabase.co`,這就是 `SUPABASE_URL`。

**Secret key** — 左下角 Project Settings → API Keys → Secret keys → 建一把,`sb_secret_` 開頭。

> ⚠️ 不要拿錯成 `publishable` 或 `anon` 開頭的那把。那是給網頁前端用的,權限不夠,後端會一直失敗。

**資料庫連線字串** — 儀表板上方 **Connect** → Session pooler → URI。裡面有個 `[YOUR-PASSWORD]` 要換成你剛剛設的密碼。

---

## 第三步:LINE 官方帳號

到 [developers.line.biz](https://developers.line.biz) 用 LINE 帳號登入。

1. 建一個 **Provider**(名字隨便,例如你的名字)
2. 在裡面建一個 **Messaging API** 頻道
   - 頻道名稱就是機器人在 LINE 上顯示的名字
   - 大頭貼可以之後再換

建好之後拿兩個值:

- **Channel secret** — 在 **Basic settings** 分頁最下面
- **Channel access token** — 在 **Messaging API** 分頁最下面,要按 **Issue** 才會產生

> ⚠️ **一定要做**:同一個 Messaging API 分頁裡,把「**自動回應訊息**」和「**加入好友的歡迎訊息**」都關掉。不關的話,LINE 官方帳號會搶在你的機器人前面回罐頭訊息,你會以為是程式壞了。

---

## 第四步:OpenAI

1. 到 [platform.openai.com](https://platform.openai.com) 註冊
2. 儲值一筆小額(5 美元就夠用很久)
3. API keys → Create new secret key,`sk-proj-` 開頭
4. **Settings → Limits 設一個月額度上限**,例如 10 美元

第 4 步很重要。萬一哪裡寫錯造成無限迴圈,這是唯一擋得住帳單的東西。

---

## 第五步:把憑證填進去

```bash
cp .env.example .env
```

用文字編輯器打開 `.env`(Mac 可以打 `open -e .env`),把前面拿到的值填進去:

```
LINE_CHANNEL_SECRET=剛剛的 channel secret
LINE_CHANNEL_ACCESS_TOKEN=剛剛的 access token
OPENAI_API_KEY=sk-proj-...
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=sb_secret_...
DATABASE_URL=postgresql://postgres.xxxxx:你的密碼@aws-0-....pooler.supabase.com:5432/postgres
```

`=` 後面直接接,不要加引號、不要留空格。

> `.env` 已經被 `.gitignore` 排除,不會被上傳到 GitHub。**永遠不要把這個檔案的內容貼到任何地方**,包括問人問題的時候。

然後建立資料表:

```bash
python scripts/migrate.py
```

看到「完成:執行 N 份」就對了。

---

## 第六步:先在自己電腦上跑跑看

```bash
python -m pytest -q          # 確認環境沒問題,應該全部 passed
uvicorn app.main:app --reload
```

看到 `configured: true` 就代表憑證都讀到了。

**開另一個終端機視窗**(原本那個要留著跑服務),讓 LINE 找得到你的電腦:

```bash
brew install ngrok     # 或到 ngrok.com 下載
ngrok http 8000
```

畫面上會有一行 **Forwarding**:

```
Forwarding  https://a1b2-c3d4.ngrok-free.app -> http://localhost:8000
            └────────── 複製這一段 ──────────┘
```

回到 LINE Developers Console → Messaging API → **Webhook URL**,填入:

```
https://a1b2-c3d4.ngrok-free.app/line/webhook
```

(記得後面要加 `/line/webhook`)

按 **Update** → **Verify**,出現 Success 就成功了。再把 **Use webhook** 打開。

現在用手機掃 Messaging API 分頁上的 QR code 加好友,傳一句話試試看。

> 這個階段關掉電腦機器人就不會回話了,`ngrok` 重開網址也會變、要重填一次。下一步解決這個問題。

---

## 第七步:部署,讓它 24 小時活著

1. 到 [railway.com](https://railway.com) 用 GitHub 登入,訂閱 Hobby 方案
2. **New Project** → **Deploy from GitHub repo** → 選你的 repo
3. 進去之後點那個服務 → **Variables** → 把 `.env` 裡的值一個一個加進去

   ⚠️ `DATABASE_URL` **不用**加。它只有建表和備份時才用得到,少放一份憑證在雲端比較安全。

4. **Settings** → Networking → **Generate Domain**,會得到一個 `xxx.up.railway.app` 的網址
5. 回到 LINE Console,把 Webhook URL 改成 `https://xxx.up.railway.app/line/webhook`,再 Verify 一次

現在你可以關掉電腦了,機器人還會繼續運作。

---

## 改成你自己的

專案裡的預設角色是「陪伴一位在遠洋商船上工作的輪機系實習生」。要換成你自己的用途,**大部分情況只要改一個地方**。

### 改背景(最常改的)

打開 [`app/prompts.py`](../app/prompts.py),找到 `DEFAULT_PROFILE`:

```python
DEFAULT_PROFILE = """\
他是輪機系的實習生,在遠洋商船上工作,一次上船幾個月。
工作內容要靠自己看說明書學,說明書多半是全英文的,常有挫折感。
船上網路是衛星訊號,時好時壞。他每個月只靠港幾天。\
"""
```

換成你要陪伴的人的處境,例如:

```python
DEFAULT_PROFILE = """\
她是護理師,在醫學中心上三班制,常常日夜顛倒。
工作壓力大,下班後多半只想耍廢,不想被問「今天過得怎麼樣」。\
"""
```

**這段只寫事實,不要寫「你應該多鼓勵她」這類規則。** 行為規則在 `PERSONA` 裡,兩者刻意分開——因為背景會隨對話自動更新,不能讓它有機會改掉機器人的行為準則(語氣、心理邊界那些)。

改完重新部署:`git push` 之後 Railway 會自動更新。

### 背景會自己長

你不用一直手動改。程式會在對話累積到一定量時,把現有背景連同新對話一起交給模型,**只有在對方明講了根本處境的改變**(換工作、畢業、實習結束)才會改寫。

改寫有三道保護:

- 只在處境**根本改變**時才動,一般的心情起伏不算
- 寫入前檢查有沒有混進行為規則,有就整個丟掉
- 每次變動都寫進 `profile_history`,查得出來什麼時候改成什麼

使用者自己也看得到——在 LINE 打「你記得我什麼」,背景會顯示在最上面。

### 改名字與語氣

`app/prompts.py` 的 `PERSONA` 開頭:

```python
你叫知平,是一個 AI 夥伴,長期陪伴一位在遠洋商船上工作的使用者。
```

名字、講話方式、要不要用敬語,都在這一段。往下還有語氣規則和幾組示範對話,那些示範對回覆長度的影響很大,值得照著自己想要的感覺重寫。

### 心理邊界要不要留

`PERSONA` 裡有一段「關於陪伴的分寸」,分三層處理情緒,第三層會提供台灣的求助專線(1925、1995)。

**如果你在其他國家,記得換成當地的號碼。** 這段的設計理由寫在 [DESIGN.md](DESIGN.md) 第 6 節——簡單說,是為了避免一個 AI 太會接住情緒,反而讓使用者更少向真實的人求助。

### 其他常見調整

| 想改什麼 | 改哪裡 |
|---|---|
| 回覆長度 | `.env` 的 `OPENAI_CHAT_MAX_TOKENS` |
| 用更便宜的模型 | `.env` 的 `OPENAI_CHAT_MODEL` |
| 等幾秒才合併訊息 | `app/config.py` 的 `debounce_seconds` |
| 每日對話上限 | `app/config.py` 的 `daily_chat_limit` |
| 記幾則對話才抽取一次記憶 | `app/services/memory_extraction.py` 的 `EXTRACTION_THRESHOLD` |

---

## 維運

### 備份

```bash
python scripts/backup.py
```

匯出到 `~/zhiping-backups/`。**腳本會拒絕把檔案寫進專案資料夾**——備份檔含完整對話紀錄,放在專案外面才不可能被誤傳到 GitHub。

備份檔在你電腦上仍然是敏感資料,電腦送修或轉手前記得處理掉。

### 健康檢查

```bash
python scripts/healthcheck.py
```

檢查 API、資料庫、當日對話數、多久沒備份。可以排程每天跑一次(專案裡有 GitHub Actions 的範例,在 `.github/workflows/`),出問題會透過 LINE 通知你。

要收到通知,`.env` 要填 `OWNER_LINE_USER_ID`——那是**你自己**的 LINE user id。取得方式:你也加這個機器人好友、傳一則訊息,然後到 Supabase 的 Table Editor 看 `conversations` 表最新那筆的 `line_user_id`。

---

## 卡住的時候

| 症狀 | 通常是 |
|---|---|
| Verify 按下去失敗 | 服務沒在跑,或網址少了 `/line/webhook` |
| 傳訊息沒反應 | 「自動回應訊息」沒關,或 Use webhook 沒打開 |
| 回一句罐頭訊息 | 一樣是「自動回應訊息」沒關 |
| `configured: false` | `.env` 有欄位沒填,看終端機會列出缺哪個 |
| 資料庫一直失敗 | key 拿錯了(拿到 anon/publishable 那把) |
| `command not found: python3` | 需要先安裝 Python |

看終端機的錯誤訊息,它通常會直接說缺什麼。
