# roy_harness

一個核心，加上可選的 superagent、kb 與一層 GUI 面板轉接，放在同一個 repo：

- **harness（核心）**：一個給本機模型用的 coding agent 外殼。把 Isuru Wijesiri 的《Harness Engineering 101》玩具 agent 從 Anthropic 格式移植到 **OpenAI 相容端點**（vLLM／llama.cpp／SGLang／OpenAI／Anthropic 相容層），並補上玩具版沒有的部分：串流與重試、壓縮、背景任務與喚醒、權限分層、並行子 agent、Hook／MCP／Skill、圖片、崩潰恢復、跨 session 的筆記與整理，以及一個網頁 GUI。
- **superagent（疊加層）**：讓外部事件自己開工單，交給獨立的 harness 子行程去做，把結果送回來。**它依賴 harness**（每張工單就是一個 harness 子行程），但 harness 不認得它：刪掉 `src/superagent/`，harness 與其測試的結果逐項不變，有測試驗證。
- **kb（個人知識庫，`kb/`）**：可選。文字與 PDF 從收件、轉換、切塊到搜尋（詞彙／向量／混合）；每個對知識的改動都是要你核准的提案；`kb ask` 先由程式檢索，再交給一個沒有任何工具的 harness 子行程回答，引文由程式逐字核對、答案旁列出它拿到的段落。**自己的 `README`、`docs/`、`tests/`，不 import harness**（只把它當子行程啟動），harness 也不認得它。做到 S4：圖片、音訊、影片與圖形介面還沒做。詳見 [kb/README.md](kb/README.md)。
- **GUI 面板轉接（`src/gui_layers/`）**：把 superagent 與 kb 接成網頁 GUI 設定頁裡的開關與面板。同樣，harness 不認得它。kb 沒有預設資料夾，要先自己建好（`kb init`）再到設定頁填路徑；沒有 `kb/src`（例如你刪掉了 kb）時 kb 面板顯示「未安裝」，其他功能不受影響。

> 這是教學／實驗用的程式，不是產品。harness 的 `shell=True` 是刻意設計（它就是要讓模型執行指令），真正的控制在 `gate()` 與安全分層。**只在 Windows 11、對單一模型（作者的 vLLM 上的 Qwen3.6-35B-A3B）實測過**；其他 provider 只做了離線測試。測試用的 Python 是 3.13（CI 與作者機器），其他版本沒驗證。

## 先看哪裡

| 我想… | 執行 | 看這裡 |
|---|---|---|
| 在終端機用本機模型跑一個 coding agent | `python -X utf8 src/harness/harness_vllm.py` | 〈快速開始〉 |
| 用網頁操作（多 session、貼圖、核准卡片、複製／保存／刪除 session） | `python src/harness/harness_gui.py` | 〈網頁 GUI〉 |
| 同一個網頁，**再載入**superagent／kb 兩個疊加層（設定頁出現它們的開關卡片與面板；用上一列的指令啟動時設定頁還在，但沒有任何層可開） | `python src/gui_layers/launcher.py` | 〈網頁 GUI〉 |
| 讓外部事件自動開工單並回覆 | 在 `src/` 下 `python -X utf8 -m superagent …` | 〈疊加層：superagent〉 |
| 建立個人知識庫、搜尋與提問 | 在 repo 根目錄 `PYTHONPATH=kb/src python -X utf8 -m kb --home <資料夾> init`（之後 `add`／`commit`／`search`／`ask`） | [kb/README.md](kb/README.md) |
| 知道哪些東西驗證過、哪些沒有 | | 〈測試與驗證〉、〈已知限制〉 |
| 了解記憶（`AGENT.md`、`MEMORY.md`、筆記） | | 〈記憶〉、[docs/notes-and-dream.md](docs/notes-and-dream.md) |

- 只用 Python 標準函式庫，零依賴；harness 主程式 [`src/harness/harness_vllm.py`](src/harness/harness_vllm.py)（約 2,200 行）。
- 本 repo **只放我自己新增與修改的部分**，不轉載書稿（書的文字是 CC BY-NC-ND，不可改作）。上游：[IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)。

## 特色

1. **確定性規則優先，模型只做判斷**。路徑保護、先讀再編輯、來源追蹤、引文逐字驗證都是程式碼，不是請模型自己小心；模型參與的地方（`auto` 分類器、`/dream`、superagent 的准入判斷）都沒有工具、只回 JSON；分類器的 ALLOW 與 `/dream` 的提案還必須附上程式能驗證的引文。
2. **分層安全**。從「子 agent 不給手」到「檔案工具的寫入限制在專案根目錄」共五層，預設只問有風險的指令；Hook 只能否決、不能放行。**沒有 OS 級 sandbox**：shell 指令仍可讀寫專案外的檔案（見〈已知限制〉）。
3. **記憶有信任模型**。人寫的 `AGENT.md` 當指令；模型寫的筆記永遠是**資料**，要附你原話的引文才會自動載入；目錄 `MEMORY.md` 由程式產生，手改無效。
4. **可拔除的疊加層**。superagent 與 GUI 面板各自可以整個刪掉；harness 的原始碼不 import、也不執行它們（有測試掃描）。
5. **崩潰可續、絕不重跑**。先寫再執行；重開時沒有結果的呼叫補 `INTERRUPTED`，不自動重做。
6. **驗證分層標示**。CI、作者機器上的 live 測試、手動操作、沒驗證的東西分開寫（〈測試與驗證〉），多數量測數字附環境、次數與限度；〈已知限制〉單獨成節；多輪第三方（小o，唯讀靜態）審查紀錄在 [docs/validation-and-reviews.md](docs/validation-and-reviews.md)。

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
| 圖片與網頁 GUI | `view_image`；多 session 分頁、核准卡片、背景任務面板、貼圖／上傳；session 的複製／保存／刪除；設定頁與疊加層面板 | 〈網頁 GUI〉、[docs/images-and-gui.md](docs/images-and-gui.md) |
| 評測 | [`bench/`](bench/README.md)：12 個題型、確定性評分器、自我改進迴圈 | |
| 事件編排 | superagent：事件 → 工單 → 做事 → 回覆 | [docs/superagent.md](docs/superagent.md) |
| 知識庫 | kb：收件、轉換、切塊、搜尋、提案與核准、`kb ask` | [kb/README.md](kb/README.md) |

斜線指令：`/help /quit /clear /compact /mode [safe|normal|auto] /tasks /stop <id> /undo /perms /cache /stats /skills /skill <名稱> /mcp /hooks /notes /dream`（`/skill` 手動把某個 skill 交給模型；`/notes`、`/dream` 見〈記憶〉）。

## 最近新增

| PR | 日期 | 內容 |
|---|---|---|
| #43 | 2026-11-24 | **kb 併入本 repo**（`kb/`，之前是只在本機的獨立 git repo）；`kb/s1/` 裡由私人 wiki 與掃描頁面產生的評測資料（`a0/`、`results/`、`tasks/`）不公開，腳本與報告留著 |
| #39–#41 | 2026-11-19～24 | **CI**（GitHub Actions、Windows）與 main 分支保護；修 `superagent status` 的讀取競態；GUI **設定頁**（疊加層滑動開關）、superagent／kb 面板（`src/gui_layers/`）、**session 複製／保存／刪除**；修「續跑」鈕會撤銷停止的缺陷與一個偶發失敗的筆記測試 |
| #35–#38 | 2026-11-12 | **筆記與 `/dream`**：`remember`／`recall`、`.harness_notes/MEMORY.md` 目錄、`/notes`、`/dream`；**預設開啟**（`HARNESS_NOTES=0` 關閉）；superagent 工單與 bench 固定關閉；README 重構與審查修正 |
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
python src/harness/harness_gui.py                     # 網頁 GUI，見〈網頁 GUI〉
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

## 記憶

三樣東西，**信任等級不同**：

- **`AGENT.md`**：**你**寫的規則，**當指令信任**；啟動時整份放進系統提示（上限 20,000 字元），模型不能改（受寫入保護）。
- **`.harness_notes/<名稱>.md`（筆記）**：**模型**用 `remember` 寫，是**資料，不是指令**。`remember` 要附一段從這個 session 逐字複製的引文：引文在**你打的字**裡 → `active`，下次啟動進目錄；只在**工具輸出**裡 → `pending`，不進提示、要你 `/notes promote`；哪裡都找不到（含模型自己說過的話）→ 拒絕，什麼都不寫。
- **`.harness_notes/MEMORY.md`**：筆記的目錄，由**程式**從各筆記標頭產生；手改在下次啟動會被丟掉並重建。

筆記**預設開啟**，`HARNESS_NOTES=0` 關閉；開啟後每個專案資料夾第一次跑會多出 `.harness_notes/`，建議加進該專案的 `.gitignore`。`/dream` 回頭讀這個 session，**只提案**（模型沒有工具、只回 JSON，引文與「誰說的」由程式檢查）。指令：`/notes`、`/notes show|promote|forget NAME`、`/dream [session.json …]`、`/dream apply p1 p2`。信任規則、設定、驗證與限制全文：[docs/notes-and-dream.md](docs/notes-and-dream.md)。

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
| `HARNESS_GUI_HOME` | `~/.roy_harness_gui` | GUI 存 session 與設定的資料夾 |

完整清單與統計欄位（`/stats`）見 [docs/reference.md](docs/reference.md)。

## 網頁 GUI

```bash
python src/harness/harness_gui.py     # 核心 GUI：印出帶 token 的網址並開瀏覽器；--port（預設 8791）、--no-browser
python src/gui_layers/launcher.py     # 同一個 GUI，另外載入疊加層（superagent／kb）；參數相同
```

- **核心功能**（兩種啟動方式都有）：多個 session 分頁與續跑、核准卡片、背景任務面板、模式切換，可以貼上或拖進圖片。每個 session 是一個子行程 `harness_vllm.py --jsonl`。
- **session 的複製、保存、刪除**（右側面板，或左側列表每一列滑過時的圖示）：複製＝同一段對話與圖片的新 session，**不會自動執行**；保存＝下載 `.md`（給人讀）或 `.json`（harness 的完整對話檔）；刪除＝先確認，再結束它的 harness 並**永久**刪掉對話、標題與貼過的圖片（沒有垃圾桶，不動專案資料夾）。
- **設定頁**（左下角「⚙ 設定」）：每個疊加層一張卡片、一個滑動開關，加上它自己的幾個文字設定；記在 `~/.roy_harness_gui/settings.json`。**superagent 預設開、kb 預設關。** 關掉的層，伺服器對它的請求回 409，不只是把按鈕藏起來（已經開始的動作不會被中斷）。開著的層在左下角有入口。用 `harness_gui.py` 啟動時設定頁會寫明沒有載入任何疊加層。
- **superagent 面板**：顯示工單、等你回答的問題、outbox；問題可按「允許一次」「拒絕」，工單可「停止」，被擱置的才能「續跑」。它**不會替你啟動 orchestrator**。**kb 面板**：搜尋、「問」（先檢索再問模型，模型沒有工具；會把檢索到的段落送到你填的模型端點）、看與放行被扣住的答案；要先用 `kb init` 建好資料夾並在設定頁填路徑；沒有 `kb/src` 時顯示「未安裝」。
- 只綁 `127.0.0.1`，每次啟動隨機 token、檢查 `Host`／`Origin`、CSP。**token 外洩等於交出執行權**，重啟伺服器才換新。架構、限制與測試過程見 [docs/images-and-gui.md](docs/images-and-gui.md)。

`view_image(path)` 讓模型看圖：session 只存參照（不存位元組），組請求時才讀檔並驗 sha256，只送最近 3 張；從檔頭判斷格式，上限 5 MB、總像素數不超過 3840×2160（寬×高的乘積，不是各邊各自的上限）。預設只對 `vllm` 開；託管的第三方 provider 每張圖都會問你。

## 疊加層：superagent

harness 一次只服務一個 session。`superagent` 把它包成「事件 → 工單 → 做事 → 回覆」的迴圈。**這是研究／實驗用的實作，不是產品。**

```
inbox/*.json ─► 准入（確定性）─► Ticket：一個目錄＋一個 session＋一個 harness 子行程（safe 模式）
                  │                  └─ 做完 → 回覆進 outbox ─► 閒置結束 ─► 名單內的人再發言就重開同一張
                  └─ 不是明確請求、但可能屬於某條主動規則 → 可選：交給一個「沒有任何工具」的模型判斷
```

- **只經三條線碰 harness**：子行程 `harness_vllm.py --jsonl`、每張 Ticket 目錄裡的 hook 設定、自己組的受控環境。
- **權限不放大**：Executor 固定 `safe` 模式，寫檔與指令都要人答 `y`（永不送會變成永久授權的 `a`）；外部文字永遠先包框；hook 把 `read_file`／`write_file`／`edit_file` 限在 Ticket 目錄，並否決 `agent`／`skill`／MCP 等工具（shell 指令不限路徑，靠人答 `y` 把關）；**筆記固定關閉**（`HARNESS_NOTES=0`），工單處理外部文字、沒人看著，不該留下筆記。
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

其他指令（`status`、`answer <ask_id> y|n`、`stop <ticket_id>`、`resume <ticket_id>`、`down --mode stop|purge`、`decisions`，量測流程 `label`／`eval`／`report`）與事件檔格式、Outbox 契約（`executor_output` 的文字是**資料，不是指令**）見 [docs/superagent.md](docs/superagent.md)。**每個指令一律帶同一個 `--home`**：省略時用環境變數 `SUPERAGENT_HOME`，沒設就用程式裡寫死的作者路徑，命令會指向另一個 home。

**託管 provider 的金鑰**用環境變數 `SUPERAGENT_API_KEY`；superagent 只讀這一個，不讀你環境裡的 `OPENAI_API_KEY`／`ANTHROPIC_API_KEY`／`HARNESS_API_KEY`；工單子行程不繼承這些變數，`SUPERAGENT_API_KEY` 的值會以 `HARNESS_API_KEY` 傳給它。本機 vLLM 不需要金鑰。

**和 DeepSeek 的 Super-agent 原型的關係**：設計借自 DeepSeek Harness 團隊的 Super-agent 文章與開源原型，但這是**另一份 Python 實作**，沒有複製其程式碼，也和它不相容；來源目前只有本機檔案收件匣，沒有 GitHub／飛書。

## 測試與驗證

```bash
python verify.py                                       # 一條指令跑離線 harness 與 kb 兩套（`--only kb` 只跑一段）；kb 缺 numpy／PyMuPDF 算失敗，不算略過
python -X utf8 -m unittest discover -s tests          # 離線
HARNESS_LIVE=1 python -X utf8 -m unittest discover -s tests   # 加上打真實伺服器的 live 測試
SUPERAGENT_LIVE=1 ...                                  # superagent 對真實模型的端到端（模型判斷＋整個工單迴圈，兩個測試）
SUPERAGENT_REMOVAL=1 ...                               # 慢速的「拿掉 superagent 後結果不變」檢查
```

驗證分四個層級，**不要混在一起讀**：

| 層級 | 內容 |
|---|---|
| **CI（自動）** | 每個 PR 與每次推 main 都跑離線整套：`windows-latest`、Python 3.13、`python -X utf8 -m unittest discover -s tests`、逾時 40 分鐘。另有一個平行的 `kb-tests`（在 `kb/` 下 `python run_tests.py`；裝 `numpy` 與 `pymupdf`，它們是 kb 的可選套件）。main 有分支保護：**`offline-tests` 與 `kb-tests` 都必須通過，管理員也一樣**（2026-11-27 起，`enforce_admins` 開啟；故障時怎麼解除見 [docs/VERIFY.md](docs/VERIFY.md) 第 5 節）。最近一次完整離線跑（2026-11-27，作者機器，`python verify.py`，單次，在 main `837ba1f` 上）：harness **1264 項、OK、略過 27 項、895 秒**；kb **799 項、OK、略過 1 項、199 秒**；合計 1097 秒（兩段是依序跑的，約 18 分鐘）。CI 兩個 job 是並行的，PR #58 的耗時：`offline-tests` 11 分 12 秒、`kb-tests` 4 分 15 秒（CI 上的項數沒另外讀） |
| **作者機器 live** | 2026-11-25，真實 vLLM、單次：`HARNESS_LIVE=1` 整套 **1213 項全 OK、略過 11、1150 秒**（上一次 2026-11-11：1060 項、略過 11、813 秒；這次涵蓋之後新增的測試）；`SUPERAGENT_LIVE=1` 的 2 項 OK（11-11 約 68 秒、11-25 約 74 秒，見下一列）。**整套 live 跑過不等於新功能對真模型的行為都量過**：筆記與 `/dream` 真模型只手動跑過 2 次，單一模型，沒量過模型主動存筆記的頻率與 `/dream` 準確度 |
| **手動瀏覽器（真實 Chrome＋Selenium，單次，腳本沒進 repo）** | 2026-11-24：GUI 設定頁與 session 複製／保存／刪除端到端 25 項檢查；superagent 面板對運作中的 orchestrator（**腳本化假模型**，不是真模型）13 項；kb 面板的「問」對真實模型端點（合成資料）13 項，兩個問題、一個模型、lexical 檢索，是存在性檢查，不是準確率。**2026-11-25**：superagent 對**真實模型**（作者的 vLLM，單次）：`SUPERAGENT_LIVE=1` 兩項 OK、74 秒；面板那一輪 7 項（真模型的工單要求寫檔 → harness 問 → 面板「允許一次」→ 檔案寫出且內容含 hello → 回覆「完成」進 outbox）；真正被擱置的工單（用一個會立刻死三次、第四次才變成真 harness 的**替身行程**，引擎自己的規則擱置它）：面板列出備註與「停止」「續跑」，按「續跑」後工單真的重新跑起來，8 項；kb 的金鑰路徑（`--api-key-env`，對**本機假的託管端點**要求 `Authorization: Bearer`，不是真的託管服務）11 項：金鑰到得了請求、值不出現在 kb 的輸出或家目錄任何檔案、變數沒設就在送出前拒絕、金鑰錯誤與沒給金鑰都變成「扣住的答案」而不是當機 |
| **沒驗證** | **真正的託管 provider（OpenAI／Anthropic）**：需要真金鑰，我沒有、也不會替你填，所以只用本機假端點驗過金鑰路徑；**kb 面板不支援金鑰**（面板沒有 `--api-key-env` 欄位，要用託管 provider 只能走命令列）；superagent 對真模型只有一個模型、單次；擱置工單是用替身行程造的，不是真 harness 自己當掉；POSIX；GUI 沒有放進 repo 的自動化瀏覽器測試 |

- **kb 的測試**：在 `kb/` 下執行 `python run_tests.py`（標準函式庫 unittest）。在乾淨環境（沒裝任何套件）會有 52 個錯誤、4 個模組載入失敗，因為 numpy 與 PyMuPDF 是可選套件，要先裝；另外有兩個測試曾因為 import 作者私人 wiki 腳本而在 CI 上失敗（已修，#44）。裝好之後：作者機器（2026-11-27）799 項、OK、略過 1 項（符號連結權限）、199 秒；CI 的 `kb-tests` 約 4 到 5 分鐘（PR #58：4 分 15 秒）。CI 從 #44 起會跑它們。
- **CI 的偶發失敗**：兩個都是時間相關的測試。筆記測試（機制已在作者機器重現：Windows 檔案時間戳可能比剛取的 `time.time()` 還早，零間隔下 600 次寫入有 444 次；**那次 CI 失敗是否就是這個機制，是推測**，沒有 runner 上的時序紀錄；PR #41 已改成明確設定修改時間）；`test_superagent_replay` 的一個 chunk 逾時測試（逾時也涵蓋為下一段啟動新的 harness 行程；2026-11-24 在 #40 合併後的 main 失敗一次）。#44 把逾時從 2 秒放寬到 15 秒：在作者機器把 CPU 吃滿重現，修前 10 次全失敗，修後 1/6（24 個忙迴圈 0/6、閒置 0/4），所以**大幅改善但極端超載下不是免疫**。**單次綠燈不等於穩定。**
- **略過的 27 項**：需要另設環境變數才會跑的 live 測試與移除檢查，以及依機器條件略過的測試（例如需要符號連結權限的那幾項，作者的機器沒有，從未實跑）。
- 每項測試的 docstring 寫的是「為什麼這個行為重要」；筆記與 `/dream`、GUI 這批新程式碼另做過突變驗證（把規則一個一個弄壞，測試要抓到；腳本沒進 repo）。
- 較早的整套離線跑、移除檢查、live 紀錄與多輪第三方（小o，唯讀靜態）審查紀錄都在 [docs/validation-and-reviews.md](docs/validation-and-reviews.md)。

## 已知限制

**harness**

- **沒有 OS 級 sandbox**：shell 指令仍可讀寫專案外的檔案；寫入限制與環境清理是行程內的強制。
- 保護 `AGENT.md`、hook／MCP 設定、skills 與筆記資料夾，對 shell 只是字串比對，**可被刻意混淆**（例如用變數分段組檔名）。
- **筆記有殘留風險**：模型可以附一句真的使用者引文、把壞內容放進本文；引文證明想法從哪來，不證明本文忠實。補強（長度上限、主機檢查、資料橫幅、啟動時列出新增項目）擋不住說服力強的文字；能寫該資料夾的 MCP 工具與你自己的 hook 不經過 `remember`（MCP 預設每次問你）。預設開啟之後每個專案資料夾會多出 `.harness_notes/`，其日常足跡與每次請求多兩個工具定義的 token 成本**沒量**。見 [docs/notes-and-dream.md](docs/notes-and-dream.md)。
- `auto` 分類器的「引用驗證」只確認引文是請求的子字串，不理解否定語境；誤判率只量過一種任務、3 次。
- **`auto` 分類器新增的三項檢查（來源追蹤、先前動作、寫檔內容）證據很薄**：來源追蹤只看主機、不看路徑，拆字串／環境變數／編碼可繞過，也會讓工具輸出裡出現過的名稱（如 `socket.io`）之後被提到時多問一次人；鏈偵測只量過 9 個我自己寫的案例；`edit_file` 的分類器看不到檔案其餘部分；`HARNESS_CLASSIFIER_MODEL` 沒有對第二個真實模型驗證。完整清單見 [docs/reference.md](docs/reference.md) 的〈已知限制〉。
- 除了 `vllm`，其他 provider 只做了離線測試；模型回應與 `/cache` 的回應沒有大小上限。
- 白名單與風險字串偏 Windows／cmd；沒有在 POSIX 實跑過。

**GUI**

- 同一個瀏覽器開 6 個以上「執行中」的 session 會用光連線；「停止」是強制結束、不能只中斷一輪。
- session 的刪除是永久的（沒有垃圾桶）；疊加層的開關只管「之後的請求」，已經開始的動作（例如一個還沒回來的 `kb ask`）會跑完。
- kb 面板的「問」只在作者機器對一個真實端點用合成資料驗證過一次；kb 自己的 S4 端到端也只有一個模型、四個問題（`kb/docs/S4_E2E.md`）。

**superagent**

- Ticket 的行程仍以你的 OS 帳號執行，`safe` 模式靠人答 `y` 把關，不是隔離。
- 只在 Windows 11 測過；真模型端到端只有兩個 live 測試與兩輪手動操作（單一模型）；來源只有本機收件匣。其餘見 [docs/superagent.md](docs/superagent.md)。

完整清單見 [docs/reference.md](docs/reference.md)。

## 檔案配置

```
src/harness/   harness_vllm.py（主程式）、harness_net.py（URL 政策）、harness_origin.py（主機來源追蹤）、harness_hooks.py、
               harness_mcp.py、harness_skills.py、harness_notes.py（筆記與 MEMORY.md）、harness_dream.py（/dream）、
               harness_gui.py ＋ .html ＋ _md.js（GUI；只有通用的疊加層掛點，不認得任何一層）
src/superagent/ 可選的疊加層（engine、store、gate、executor、main_session、mandate_hook、frame、outbox…）
src/gui_layers/ 把 superagent、kb 接成 GUI 設定頁開關與面板的轉接層（launcher.py 啟動）；harness 資料夾不認得它們
tests/         各模組的測試（含 fake_mcp_server.py、live_fastmcp_server.py 與 superagent 的測試台）
bench/         評測與自我改進迴圈（結果夾 bench_runs/ 不進 repo）
docs/          詳細文件：harness 的細節（從 README 拆出，原文未改）、notes-and-dream.md、superagent.md、驗證與審查紀錄
.github/       CI（workflows/ci.yml）
kb/            個人知識庫（可選；自己的 pyproject、README、docs、tests；與 harness 互不 import）
```

## 出處與授權

程式碼骨架來自 [IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)（Copyright (c) 2026 Isuru Wijesiri）：程式碼 MIT，文字 CC BY-NC-ND 4.0。`harness_vllm.py` 是以其 MIT 程式碼為基礎的衍生作品，MIT 全文見 `LICENSE-UPSTREAM-MIT`；書中文字未被改寫或轉載。

本 repo 在上游之上新增的部分同樣採 **MIT**（見 `LICENSE`，Copyright (c) 2026 Kun-Min Chiu）；上游的版權聲明保留在 `LICENSE-UPSTREAM-MIT`。
