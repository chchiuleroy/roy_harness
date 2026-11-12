# roy_harness

兩樣東西放在同一個 repo，彼此獨立：

- **harness**：一個給本機模型用的 coding agent 外殼。把 Isuru Wijesiri 的《Harness Engineering 101》玩具 agent 從 Anthropic 格式移植到 **OpenAI 相容端點**（vLLM／llama.cpp／SGLang／OpenAI／Anthropic 相容層），並補上玩具版沒有的部分：串流與重試、壓縮、背景任務與喚醒、權限分層、並行子 agent、Hook／MCP／Skill、圖片、崩潰恢復、跨 session 的筆記與整理，以及一個網頁 GUI。
- **superagent**：疊在 harness 之上、**可選也可整個拿掉**的編排層。讓外部事件自己開工單，交給獨立的 harness 子行程去做，把結果送回來，做完的工單閒置後自己結束、有人再發言就重開。

> 這是教學／實驗用的程式，不是產品。harness 的 `shell=True` 是刻意設計（它就是要讓模型執行指令），真正的控制在 `gate()` 與安全分層。**只在 Windows 11、對單一模型（作者的 vLLM 上的 Qwen3.6-35B-A3B）實測過**；其他 provider 只做了離線測試。

## 先看哪裡

| 我想… | 看這裡 |
|---|---|
| 快速了解這個專案的設計取向 | 〈特色〉 |
| 在本機模型上跑一個 coding agent | 〈快速開始〉 |
| 知道最近加了什麼 | 〈最近新增〉 |
| 了解記憶：`AGENT.md`、`MEMORY.md`、筆記、`/dream` | 〈記憶：`AGENT.md`、`MEMORY.md` 與筆記〉、[docs/notes-and-dream.md](docs/notes-and-dream.md) |
| 用網頁操作、貼圖給模型看 | 〈網頁 GUI 與圖片〉、[docs/images-and-gui.md](docs/images-and-gui.md) |
| 讓外部事件自動開工單並回覆 | 〈疊加層：superagent〉、[docs/superagent.md](docs/superagent.md) |
| 知道哪些東西實測過、哪些沒有 | 〈測試與驗證〉、〈已知限制〉 |

- 只用 Python 標準函式庫，零依賴；harness 主程式 [`src/harness/harness_vllm.py`](src/harness/harness_vllm.py)（約 2,200 行）。
- 本 repo **只放我自己新增與修改的部分**，不轉載書稿（書的文字是 CC BY-NC-ND，不可改作）。上游：[IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)。

## 特色

1. **確定性規則優先，模型只做判斷**。路徑保護、先讀再編輯、來源追蹤、引文逐字驗證都是程式碼，不是請模型自己小心；模型參與的地方（`auto` 分類器、`/dream`、superagent 的准入判斷）都沒有工具、只回 JSON；分類器的 ALLOW 與 `/dream` 的提案還必須附上程式能驗證的引文。
2. **分層安全**。從「子 agent 不給手」到「檔案工具的寫入限制在專案根目錄」共五層，預設只問有風險的指令；Hook 只能否決、不能放行。**沒有 OS 級 sandbox**：shell 指令仍可讀寫專案外的檔案（見〈已知限制〉）。
3. **記憶有信任模型**。人寫的 `AGENT.md` 當指令；模型寫的筆記永遠是**資料**，要附你原話的引文才會自動載入；目錄 `MEMORY.md` 由程式產生，手改無效。
4. **可拔除的編排層**。刪掉 `src/superagent/`，harness 與其測試的結果逐項不變，有測試驗證。
5. **崩潰可續、絕不重跑**。先寫再執行；重開時沒有結果的呼叫補 `INTERRUPTED`，不自動重做。
6. **重要的測量與驗證數字會附上環境、次數與限度**。哪裡實測過、哪裡只有離線測試、哪裡沒量，盡量都寫在文字裡（純結構數字如行數、題型數不另附）；〈已知限制〉單獨成節；多輪第三方（小o，唯讀靜態）審查紀錄在 [docs/validation-and-reviews.md](docs/validation-and-reviews.md)。

## 功能一覽

| 類別 | 內容 | 詳細 |
|---|---|---|
| 模型連線 | 串流（含被切碎的 tool call）；503／429（尊重 `Retry-After`）／529／斷線／被截斷會重試；首字延遲 `ttft`；多 provider | 〈模型後端〉 |
| 對話管理 | 超過 `HARNESS_COMPACT_TOKENS`（預設 96,000）就把舊訊息換成摘要，你打過的字由程式附在摘要後；session 存檔續跑 | [docs/reference.md](docs/reference.md) |
| 記憶 | `AGENT.md`（你寫）、`.harness_notes/MEMORY.md` 目錄與筆記（模型寫，當資料）、`/dream` 整理提案 | [docs/notes-and-dream.md](docs/notes-and-dream.md) |
| 工具 | `read_file write_file edit_file run_command task_output task_stop todo_write ask_user schedule_wakeup monitor agent`，加上條件式的 `view_image`、`skill`、`remember`／`recall` 與 MCP 工具 | [docs/reference.md](docs/reference.md) |
| 子 agent | 巢狀（深度 2）、同一回覆的多個 `agent` 呼叫並行 | |
| 背景任務 | 任務結束、鬧鐘、監控條件成立會**自己啟動新回合**；`/tasks`、`/stop` | |
| 崩潰恢復 | 先寫再執行；重開時沒有結果的呼叫補 `INTERRUPTED`，**絕不自動重跑** | [docs/validation-and-reviews.md](docs/validation-and-reviews.md) |
| Hook／MCP／Skill | 確定性反射（只能否決）、MCP 客戶端（兩代協定）、按需載入的 skill | [docs/hooks-mcp-skills.md](docs/hooks-mcp-skills.md) |
| 圖片與 GUI | `view_image`、網頁 GUI（含貼圖／上傳、背景任務面板、核准卡片） | [docs/images-and-gui.md](docs/images-and-gui.md) |
| 評測 | [`bench/`](bench/README.md)：12 個題型、確定性評分器、自我改進迴圈 | |
| 事件編排 | superagent：事件 → 工單 → 做事 → 回覆 | [docs/superagent.md](docs/superagent.md) |

斜線指令：`/help /quit /clear /compact /mode [safe|normal|auto] /tasks /stop <id> /undo /perms /cache /stats /skills /skill <名稱> /mcp /hooks /notes /dream`（`/skill` 手動把某個 skill 交給模型；`/notes`、`/dream` 見〈記憶〉）。

## 最近新增

| PR | 日期 | 內容 |
|---|---|---|
| #35、#36 | 2026-11-12 | **筆記與 `/dream`**：`remember`／`recall`、`.harness_notes/MEMORY.md` 目錄、`/notes`、`/dream`；**預設開啟**（`HARNESS_NOTES=0` 關閉）；superagent 工單與 bench 固定關閉 |
| #27–#34 | 2026-11-10～11 | `auto` 分類器補**來源追蹤**、同一回合的先前動作（R6）、寫檔內容檢查；修掉我自己引入的 `hosts_in()` ReDoS（30,000 字元的長單字 13.9 秒 → 3.6 毫秒）；IPv6 與 Unicode 網域；文件與測試校正 |
| #7–#26 | 2026-11-03～06 | 以 **superagent** 疊加層為主（另含 harness 的 HTTP 529 重試 #19 與測試全域狀態修復 #18）：准入、工單、閘門、測量與離線標註、操作者實測修的缺陷、「模型說有規則要再獨立看一次」 |
| #1–#6 | 2026-11-02 | 網頁 GUI、`view_image`、GUI 續跑與探索測試缺口、貼圖／上傳、README 重構 |
| （PR 之前，直接推 main） | 2026-10-21～30 | 多 provider、網路加固與 Hook、MCP 客戶端、Skill、崩潰恢復、`AGENT.md` 進系統提示並受寫入保護、執行統計 |

逐項紀錄與每一輪審查見 [docs/validation-and-reviews.md](docs/validation-and-reviews.md)。

## 快速開始

PowerShell：

```powershell
$env:VLLM_BASE_URL = "http://localhost:8000/v1"       # 你的端點（或寫進 .harness.env）
python -X utf8 src/harness/harness_vllm.py            # 互動；session 存成 session.json
python -X utf8 src/harness/harness_vllm.py my.json    # 指定 session 檔，再次執行同檔名即接續
$env:HARNESS_MODE = "auto"; python -X utf8 src/harness/harness_vllm.py
python src/harness/harness_gui.py                     # 網頁 GUI，見下
```

Bash（Git Bash 等）：

```bash
export VLLM_BASE_URL=http://localhost:8000/v1
HARNESS_MODE=auto python -X utf8 src/harness/harness_vllm.py
```

**個人端點不要寫進原始碼**：在 repo 根目錄建 `.harness.env`（已列入 `.gitignore`），逐行 `KEY=VALUE`；真正的環境變數優先於檔案。測試會檢查追蹤檔案裡只出現 localhost、廠商網域與保留後綴 `*.example`（永遠不會解析，所以不可能是外洩的端點）。

```
HARNESS_PROVIDER=vllm
VLLM_BASE_URL=http://your-vllm-host:8000/v1
VLLM_MODEL=Qwen/Qwen3.6-35B-A3B
```

## 模型後端

`HARNESS_PROVIDER`：

| provider | 預設端點 | 必設 |
|---|---|---|
| `vllm`（預設） | `http://localhost:8000/v1` | — |
| `llamacpp`、`sglang` | 無 | `HARNESS_BASE_URL`、`HARNESS_MODEL` |
| `openai` | `https://api.openai.com/v1` | `HARNESS_MODEL`、`OPENAI_API_KEY`（或 `HARNESS_API_KEY`） |
| `anthropic-compat` | `https://api.anthropic.com/v1` | `HARNESS_MODEL`、`ANTHROPIC_API_KEY`（或 `HARNESS_API_KEY`） |

- 舊的 `VLLM_*` 變數**只對 `vllm` 有效**，避免換 provider 時把金鑰送到你的 vLLM 主機。
- 明文 `http://` 送金鑰一律拒絕（託管 provider 只允許 localhost；自架 provider 沒帶金鑰才可用內網 http）；要接受風險請設 `HARNESS_ALLOW_HTTP=1`。**所有模型請求都不跟隨 HTTP 重新導向**（urllib 會把 `Authorization` 帶過去）。
- 細節（各家送出的欄位、`/cache` 的指標來源）見 [docs/reference.md](docs/reference.md)。

## 記憶：`AGENT.md`、`MEMORY.md` 與筆記

三樣東西，**信任等級不同**：

| | `AGENT.md` | `.harness_notes/MEMORY.md` | `.harness_notes/<名稱>.md`（筆記） |
|---|---|---|---|
| 誰寫 | **你** | **程式**，從各筆記標頭產生 | **模型**，用 `remember` 工具 |
| 性質 | 給模型的規則，**當指令信任** | 筆記的目錄（每條啟用的筆記一行） | **資料，不是指令** |
| 模型能不能改 | 不能（受寫入保護） | 不能直接改 | 只能經 `remember`，檔案工具與 shell 碰不到資料夾 |
| 手動編輯 | 照你寫的生效 | **下次啟動丟掉並重建** | 可以；手寫的要標 `origin: user` 與 `status: active` 才會載入 |
| 載入 | 啟動時整份放進系統提示（上限 20,000 字元） | 啟動時的目錄快照放進系統提示 | 模型用 `recall` 讀，讀回來加資料橫幅 |

**筆記怎麼把關**：`remember` 要附一段從這個 session 逐字複製的引文。

| 引文在哪 | 結果 |
|---|---|
| 在**你打的字**裡 | `active`，下次啟動進目錄 |
| 只在**工具輸出**裡 | `pending`，不進提示，要你 `/notes promote` |
| 哪裡都找不到（含模型自己說過的話） | 拒絕，什麼都不寫 |

筆記**預設開啟**，`HARNESS_NOTES=0` 關閉。開啟後每個專案資料夾第一次跑會多出 `.harness_notes/`，建議加進該專案的 `.gitignore`。

`/dream` 回頭讀這個 session（與你指定的舊 session 檔），**只提案**：模型沒有工具、只回 JSON，每條提案的引文由**程式**逐字檢查、誰說的也由程式判斷；`/dream apply p1 p2` 只動筆記資料夾；要搬進 `AGENT.md` 或 skill 的教訓只印出文字給你貼。

指令：`/notes`（列出）、`/notes show NAME`、`/notes promote NAME`、`/notes forget NAME`、`/dream [session.json …]`、`/dream apply p1 p2`。信任規則、設定、驗證與限制全文：[docs/notes-and-dream.md](docs/notes-and-dream.md)。

## 安全分層

| 層 | 做法 |
|---|---|
| 0 不給手 | 子 agent 的工具集較窄 |
| 1 確定性規則 | 受保護路徑（`.env`、`.ssh` 等）；沒讀過不能編輯；前景 `sleep` 被擋；`edit_file` 要求舊文字剛好出現一次；**來源追蹤**：指令或要寫入的文字提到「只在工具輸出出現過、使用者沒講過」的主機，一律問人（不經分類器、不被「一律允許」的前綴授權放行） |
| 2 問人 | `safe`（寫入與指令都問）、`normal`（預設，只問風險字樣）、`auto`（見下）；沒有人可問時一律拒絕 |
| 3 分類器 | `auto` 模式下，除了「可證明只讀」的指令，全部送給小模型判斷（指令判斷會看到**這一回合先前做過的動作**；每一次**寫檔**也會送去判斷，看的是寫入的內容，不帶先前動作）；ALLOW 必須逐字引用你的原話，格式不對或失敗一律改問人；「前面的動作把機密塞進要送出的內容」（R6）只問人、不直接拒絕 |
| 4 縮小爆炸半徑 | 檔案工具的寫入限制在專案根目錄（**shell 指令不受此限**）；子程序環境移除含 `KEY／TOKEN／SECRET／PASSWORD／CREDENTIAL` 的變數；寫入前備份，`/undo` 可還原 |

## 常用環境變數

| 變數 | 預設 | 用途 |
|---|---|---|
| `HARNESS_MODE` | `normal` | `safe`／`normal`／`auto` |
| `HARNESS_COMPACT_TOKENS`、`HARNESS_KEEP_RECENT` | 96000／8 | 壓縮門檻與保留訊息數 |
| `HARNESS_STREAM` | `1` | 設 `0` 關閉串流 |
| `HARNESS_VISION` | vllm 為 `1`，其他 `0` | 是否提供 `view_image` |
| `HARNESS_NOTES`、`HARNESS_NOTES_DIR`、`HARNESS_NOTES_AUTOLOAD`、`HARNESS_DREAM_MODEL` | 開（`0` 關）／`.harness_notes`／`user`／無 | 筆記與做夢；`AUTOLOAD=none` 讓所有筆記都要你批准 |
| `HARNESS_DEBUG` | 無 | 設任意值就把每次請求存到 `debug/`（圖片 base64 會遮掉） |
| `HARNESS_SANDBOX` | 無 | JSON 陣列，作為指令前綴（**未實測**） |
| `HARNESS_CLASSIFIER_MODEL` | 無（用主模型） | 讓 `auto` 分類器改問另一個模型；同一個伺服器與金鑰，所以那個模型要由同一個端點供應。**只做了離線測試，沒有對第二個真實模型跑過** |

完整清單與統計欄位（`/stats`）見 [docs/reference.md](docs/reference.md)。

## 網頁 GUI 與圖片

```bash
python src/harness/harness_gui.py     # 印出帶 token 的網址並開瀏覽器；--port（預設 8791）、--no-browser
```

多個 session 分頁與續跑、核准卡片、背景任務面板、模式切換，可以貼上或拖進圖片。每個 session 是一個子行程 `harness_vllm.py --jsonl`；只綁 `127.0.0.1`，每次啟動隨機 token、檢查 `Host`／`Origin`、CSP。**token 外洩等於交出執行權**，重啟伺服器才換新。架構、限制與測試過程見 [docs/images-and-gui.md](docs/images-and-gui.md)。

`view_image(path)` 讓模型看圖：session 只存參照（不存位元組），組請求時才讀檔並驗 sha256，只送最近 3 張；從檔頭判斷格式，上限 5 MB、總像素數不超過 3840×2160（寬×高的乘積，不是各邊各自的上限）。預設只對 `vllm` 開；託管的第三方 provider 每張圖都會問你。

## 疊加層：superagent

harness 一次只服務一個 session。`superagent` 把它包成「事件 → 工單 → 做事 → 回覆」的迴圈。**這是研究／實驗用的實作，不是產品。**

```
inbox/*.json ─► 准入（確定性）─► Ticket：一個目錄＋一個 session＋一個 harness 子行程（safe 模式）
                  │                  └─ 做完 → 回覆進 outbox ─► 閒置結束 ─► 名單內的人再發言就重開同一張
                  └─ 不是明確請求、但可能屬於某條主動規則 → 可選：交給一個「沒有任何工具」的模型判斷
```

- **只經三條線碰 harness**：子行程 `harness_vllm.py --jsonl`、每張 Ticket 目錄裡的 hook 設定、自己組的受控環境。`src/harness/` 不 import 它；刪掉 `src/superagent/`，harness 與其測試的結果逐項不變（有測試驗證）。
- **權限不放大**：Executor 固定 `safe` 模式，寫檔與指令都要人答 `y`（`python -X utf8 -m superagent --home <資料夾> answer <ask_id> y|n`，永不送會變成永久授權的 `a`）；外部文字永遠先包框；hook 把 `read_file`／`write_file`／`edit_file` 限在 Ticket 目錄，並否決 `agent`／`skill`／MCP 等工具（shell 指令不限路徑，靠人答 `y` 把關）；**筆記固定關閉**（`HARNESS_NOTES=0`），工單處理外部文字、沒人看著，不該留下筆記。
- **准入判斷用的模型只在「推斷候選」時出現**（`--assessor model`，預設關；預設 `observe` 只記「本來會接受」，不開工單；模型第一次說符合規則時，還要由全新的 session 再獨立判斷一次、兩次同規則才算，見 `docs/superagent.md`），且它沒有工具、只回一個 JSON，後果由程式碼決定。這和「做事的模型」是兩回事：每張工單的 harness 子行程本來就會呼叫你設定的模型。
- **崩潰可續**：一個事件的入庫、決定、開 Ticket、排入輸入是同一個 SQLite 交易；回覆與「輸入完成」也是；投遞是至少一次。

```bash
cd src
python -X utf8 -m superagent --home <資料夾> run --allow alice --base-url http://localhost:8000/v1 --model <模型名>
```

丟一個事件進 `<資料夾>/inbox/1.json`（一個事件一個 JSON 檔，只有 `text` 必填，其餘缺了會用預設值，見 docs）；結果在 `<資料夾>/outbox/`：

```json
{"id": "1", "thread": "t1", "sender": "alice", "text": "請在 notes.txt 寫一行 hello", "addressed": true}
```

其他指令（**一律帶同一個 `--home`**：省略時用環境變數 `SUPERAGENT_HOME`，沒設就用程式裡寫死的作者路徑，命令會指向另一個 home）：

```bash
python -X utf8 -m superagent --home <資料夾> status                 # 工單、等你回答的問題、outbox
python -X utf8 -m superagent --home <資料夾> answer <ask_id> y      # 回答核准問題（y／n）
python -X utf8 -m superagent --home <資料夾> stop <ticket_id>       # 永久停掉一張工單
python -X utf8 -m superagent --home <資料夾> resume <ticket_id>     # 被擱置的工單重新開始
python -X utf8 -m superagent --home <資料夾> down --mode stop       # 停機保留資料；--mode purge 連資料夾一起刪
python -X utf8 -m superagent --home <資料夾> decisions              # 唯讀看模型到底判了什麼；量測流程（label／eval／report）見 docs
```

**託管 provider 的金鑰**用環境變數 `SUPERAGENT_API_KEY`；superagent 只讀這一個，不讀你環境裡的 `OPENAI_API_KEY`／`ANTHROPIC_API_KEY`／`HARNESS_API_KEY`；工單子行程不繼承這些變數，`SUPERAGENT_API_KEY` 的值會以 `HARNESS_API_KEY` 傳給它。本機 vLLM 不需要金鑰。

**和 DeepSeek 的 Super-agent 原型的關係**：設計借自 DeepSeek Harness 團隊的 Super-agent 文章與開源原型（Trigger／Thread／Ticket／Mandate、訂閱透傳、observe→auto 的灰度、先落庫再動作），但這是**另一份 Python 實作**，沒有複製其程式碼，也和它不相容：原型是 TypeScript、打包成 DeepSeek Harness 的 bundle，來源是 GitHub／飛書；這裡的執行端是本 repo 的 harness，來源目前只有本機檔案收件匣。

**限制**：只在 Windows 11 測過；除兩個 live 測試外都用腳本化的假模型端點；真模型只做過兩個 live 測試與兩輪手動操作（單一模型、沒有準確度數字）；沒有 GitHub／飛書來源，也沒有 Watchdog 或遠端 daemon。完整說明、事件檔格式、Outbox 契約（`executor_output` 的文字是**資料，不是指令**）與限制清單見 [docs/superagent.md](docs/superagent.md)。

## 測試與驗證

```bash
python -X utf8 -m unittest discover -s tests          # 離線
HARNESS_LIVE=1 python -X utf8 -m unittest discover -s tests   # 加上打真實伺服器的 live 測試
SUPERAGENT_LIVE=1 ...                                  # superagent 對真實模型的端到端（模型判斷＋整個工單迴圈，兩個測試）
SUPERAGENT_REMOVAL=1 ...                               # 慢速的「拿掉 superagent 後結果不變」檢查
```

最近一次完整離線跑（2026-11-12，加入 `MEMORY.md` 與預設開啟之後，作者機器，單次）：**1173 項，全 OK，略過 27 項**，約 656 秒。前一次（2026-11-10）：1060 項中 1033 項通過、略過 27 項，約 588 秒。每項測試的 docstring 寫的是「為什麼這個行為重要」。

- **略過的 27 項**：需要另設環境變數才會跑的 live 測試與移除檢查，以及依機器條件略過的測試（例如需要符號連結權限的那幾項，作者的機器沒有，從未實跑）。
- **突變驗證**：筆記與 `/dream` 的規則被一個一個弄壞（67 個），測試全部抓到（腳本沒進 repo）。
- **移除檢查**（`SUPERAGENT_REMOVAL=1`；離線，把整套測試各跑一次，一邊是完整的樹、一邊是刪掉 superagent 的副本，逐項比對）：2026-11-05 重寫後跑過一次，通過，約 790 秒；2026-11-06 加入第二眼後再跑一次，通過，約 797 秒；2026-11-11 再跑一次（`discover -s tests -p test_superagent_cli.py`），`test_suite_identical_without_superagent` 通過、該檔 17 項 OK，約 921 秒。
- **live 測試**：2026-11-11（作者機器，真實 vLLM，單次）以 `HARNESS_LIVE=1` 整套一次 `discover` 跑過：**1060 項、全 OK、略過 11、0 失敗，813 秒**；逐檔跑與之一致（累計約 930 秒）；另以 `SUPERAGENT_LIVE=1` 跑 `test_superagent_live.py` 的 2 項（真模型判斷、整個工單迴圈），OK、約 68 秒。**這之後新增的筆記與 `/dream`，真模型只手動跑過 2 次**（單一模型；沒量過模型主動存筆記的頻率與 `/dream` 準確度）。
- superagent 的測試用真的 harness 子行程加腳本化假模型，不碰網路。前端沒有自動化瀏覽器測試，是手動對真實 vLLM 驗證的。
- 實測與多輪第三方（小o，唯讀靜態）審查紀錄見 [docs/validation-and-reviews.md](docs/validation-and-reviews.md)。

## 已知限制

**harness**

- **沒有 OS 級 sandbox**：shell 指令仍可讀寫專案外的檔案；寫入限制與環境清理是行程內的強制。
- 保護 `AGENT.md`、hook／MCP 設定、skills 與筆記資料夾，對 shell 只是字串比對，**可被刻意混淆**（例如用變數分段組檔名）。
- **筆記有殘留風險**：模型可以附一句真的使用者引文、把壞內容放進本文；引文證明想法從哪來，不證明本文忠實。補強（長度上限、主機檢查、資料橫幅、啟動時列出新增項目）擋不住說服力強的文字；能寫該資料夾的 MCP 工具與你自己的 hook 不經過 `remember`（MCP 預設每次問你）。預設開啟之後每個專案資料夾會多出 `.harness_notes/`，其日常足跡與每次請求多兩個工具定義的 token 成本**沒量**。見 [docs/notes-and-dream.md](docs/notes-and-dream.md)。
- `auto` 分類器的「引用驗證」只確認引文是請求的子字串，不理解否定語境；誤判率只量過一種任務、3 次。
- **`auto` 分類器新增的三項檢查（來源追蹤、先前動作、寫檔內容）證據很薄**：來源追蹤只看主機、不看路徑，拆字串／環境變數／編碼可繞過，也會讓工具輸出裡出現過的名稱（如 `socket.io`）之後被提到時多問一次人；鏈偵測只量過 9 個我自己寫的案例；`edit_file` 的分類器看不到檔案其餘部分；`HARNESS_CLASSIFIER_MODEL` 沒有對第二個真實模型驗證。完整清單見 [docs/reference.md](docs/reference.md) 的〈已知限制〉。
- 除了 `vllm`，其他 provider 只做了離線測試；模型回應與 `/cache` 的回應沒有大小上限。
- 白名單與風險字串偏 Windows／cmd；沒有在 POSIX 實跑過。
- GUI：同一個瀏覽器開 6 個以上「執行中」的 session 會用光連線；「停止」是強制結束、不能只中斷一輪。

**superagent**

- Ticket 的行程仍以你的 OS 帳號執行，`safe` 模式靠人答 `y` 把關，不是隔離。
- 只在 Windows 11 測過；真模型端到端只有兩個 live 測試與兩輪手動操作（單一模型）；來源只有本機收件匣。其餘見上一節與 [docs/superagent.md](docs/superagent.md)。

完整清單見 [docs/reference.md](docs/reference.md)。

## 檔案配置

```
src/harness/   harness_vllm.py（主程式）、harness_net.py（URL 政策）、harness_origin.py（主機來源追蹤）、harness_hooks.py、
               harness_mcp.py、harness_skills.py、harness_notes.py（筆記與 MEMORY.md）、harness_dream.py（/dream）、
               harness_gui.py ＋ .html ＋ _md.js（GUI）
src/superagent/ 可選的疊加層（engine、store、gate、executor、main_session、mandate_hook、frame、outbox…）
tests/         各模組的測試（含 fake_mcp_server.py、live_fastmcp_server.py 與 superagent 的測試台）
bench/         評測與自我改進迴圈（結果夾 bench_runs/ 不進 repo）
docs/          詳細文件：harness 的細節（從 README 拆出，原文未改）、notes-and-dream.md、superagent.md、驗證與審查紀錄
```

## 出處與授權

程式碼骨架來自 [IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)（Copyright (c) 2026 Isuru Wijesiri）：程式碼 MIT，文字 CC BY-NC-ND 4.0。`harness_vllm.py` 是以其 MIT 程式碼為基礎的衍生作品，MIT 全文見 `LICENSE-UPSTREAM-MIT`；書中文字未被改寫或轉載。

本 repo 在上游之上新增的部分同樣採 **MIT**（見 `LICENSE`，Copyright (c) 2026 Kun-Min Chiu）；上游的版權聲明保留在 `LICENSE-UPSTREAM-MIT`。
