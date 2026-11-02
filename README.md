# roy_harness

一個給本機模型用的 coding agent 外殼（harness）：把 Isuru Wijesiri 的《Harness Engineering 101》玩具 agent 從 Anthropic 格式移植到 **OpenAI 相容端點**（vLLM／llama.cpp／SGLang／OpenAI／Anthropic 相容層），並補上玩具版沒有的部分：串流與重試、壓縮、背景任務與喚醒、權限分層、並行子 agent、Hook／MCP／Skill、圖片、崩潰恢復，以及一個網頁 GUI。

- 只用 Python 標準函式庫，零依賴；主程式 [`src/harness/harness_vllm.py`](src/harness/harness_vllm.py)（約 2,000 行）。
- 本 repo **只放我自己新增與修改的部分**，不轉載書稿（書的文字是 CC BY-NC-ND，不可改作）。上游：[IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)。

> 這是教學／實驗用的 harness，不是產品。`shell=True` 是刻意設計（它就是要讓模型執行指令），真正的控制在 `gate()` 與下面的安全分層。**只在 Windows 11、對單一模型（作者的 vLLM 上的 Qwen3.6-35B-A3B）實測過**；其他 provider 只做了離線測試。

## 快速開始

```bash
export VLLM_BASE_URL=http://localhost:8000/v1          # 你的端點（或寫進 .harness.env）
python -X utf8 src/harness/harness_vllm.py            # 互動；session 存成 session.json
python -X utf8 src/harness/harness_vllm.py my.json    # 指定 session 檔，再次執行同檔名即接續
HARNESS_MODE=auto python -X utf8 src/harness/harness_vllm.py
python src/harness/harness_gui.py                     # 網頁 GUI，見下
```

**個人端點不要寫進原始碼**：在 repo 根目錄建 `.harness.env`（已列入 `.gitignore`），逐行 `KEY=VALUE`；真正的環境變數優先於檔案。測試會檢查追蹤檔案裡只出現 localhost 與廠商網域。

```
HARNESS_PROVIDER=vllm
VLLM_BASE_URL=http://your-vllm-host:8000/v1
VLLM_MODEL=Qwen/Qwen3.6-35B-A3B
```

### 模型後端：`HARNESS_PROVIDER`

| provider | 預設端點 | 必設 |
|---|---|---|
| `vllm`（預設） | `http://localhost:8000/v1` | — |
| `llamacpp`、`sglang` | 無 | `HARNESS_BASE_URL`、`HARNESS_MODEL` |
| `openai` | `https://api.openai.com/v1` | `HARNESS_MODEL`、`OPENAI_API_KEY`（或 `HARNESS_API_KEY`） |
| `anthropic-compat` | `https://api.anthropic.com/v1` | `HARNESS_MODEL`、`ANTHROPIC_API_KEY`（或 `HARNESS_API_KEY`） |

- 舊的 `VLLM_*` 變數**只對 `vllm` 有效**，避免換 provider 時把金鑰送到你的 vLLM 主機。
- 明文 `http://` 送金鑰一律拒絕（託管 provider 只允許 localhost；自架 provider 沒帶金鑰才可用內網 http）；要接受風險請設 `HARNESS_ALLOW_HTTP=1`。**所有模型請求都不跟隨 HTTP 重新導向**（urllib 會把 `Authorization` 帶過去）。
- 細節（各家送出的欄位、`/cache` 的指標來源）見 [docs/reference.md](docs/reference.md)。

## 它做了什麼

| | |
|---|---|
| 串流與重試 | SSE 組裝（含被切碎的 tool call）；503／429（尊重 `Retry-After`）／斷線／被截斷會重試；回報首字延遲 `ttft` |
| 壓縮與記憶 | 輸入超過 `HARNESS_COMPACT_TOKENS`（預設 96,000）就把舊訊息換成摘要；你打過的字會由程式附在摘要後（有長度上限）；`AGENT.md` 放系統提示，受寫入保護 |
| 工具 | `read_file write_file edit_file run_command task_output task_stop todo_write ask_user schedule_wakeup monitor agent`，加上條件式的 `view_image`、`skill` 與 MCP 工具 |
| 子 agent | 巢狀（深度 2）、同一回覆的多個 `agent` 呼叫並行 |
| 背景任務 | 任務結束、鬧鐘、監控條件成立會**自己啟動新回合**；`/tasks`、`/stop` |
| 崩潰恢復 | 先寫再執行；重開時沒有結果的呼叫補 `INTERRUPTED`，**絕不自動重跑** |
| Hook／MCP／Skill | 確定性反射（只能否決、不能放行）、MCP 客戶端（兩代協定）、按需載入的 skill → [docs/hooks-mcp-skills.md](docs/hooks-mcp-skills.md) |
| 圖片、GUI | `view_image` 與網頁 GUI（含貼圖／上傳）→ [docs/images-and-gui.md](docs/images-and-gui.md) |
| 評測 | [`bench/`](bench/README.md)：12 個題型、確定性評分器、自我改進迴圈 |

斜線指令：`/help /quit /clear /compact /mode [safe|normal|auto] /tasks /stop <id> /undo /perms /cache /stats /skills /mcp /hooks`。

### 安全分層

| 層 | 做法 |
|---|---|
| 0 不給手 | 子 agent 的工具集較窄 |
| 1 確定性規則 | 受保護路徑（`.env`、`.ssh` 等）；沒讀過不能編輯；前景 `sleep` 被擋；`edit_file` 要求舊文字剛好出現一次 |
| 2 問人 | `safe`（寫入與指令都問）、`normal`（預設，只問風險字樣）、`auto`（見下）；沒有人可問時一律拒絕 |
| 3 分類器 | `auto` 模式下，除了「可證明只讀」的指令，全部送給小模型判斷；ALLOW 必須逐字引用你的原話，格式不對或失敗一律改問人 |
| 4 縮小爆炸半徑 | 寫入限制在專案根目錄；子程序環境移除含 `KEY／TOKEN／SECRET／PASSWORD／CREDENTIAL` 的變數；寫入前備份，`/undo` 可還原 |

### 常用環境變數

| 變數 | 預設 | 用途 |
|---|---|---|
| `HARNESS_MODE` | `normal` | `safe`／`normal`／`auto` |
| `HARNESS_COMPACT_TOKENS`、`HARNESS_KEEP_RECENT` | 96000／8 | 壓縮門檻與保留訊息數 |
| `HARNESS_STREAM` | `1` | 設 `0` 關閉串流 |
| `HARNESS_VISION` | vllm 為 `1`，其他 `0` | 是否提供 `view_image` |
| `HARNESS_DEBUG` | 無 | 設任意值就把每次請求存到 `debug/`（圖片 base64 會遮掉） |
| `HARNESS_SANDBOX` | 無 | JSON 陣列，作為指令前綴（**未實測**） |

完整清單與統計欄位（`/stats`）見 [docs/reference.md](docs/reference.md)。

## 網頁 GUI

```bash
python src/harness/harness_gui.py     # 印出帶 token 的網址並開瀏覽器；--port（預設 8791）、--no-browser
```

多個 session 分頁與續跑、核准卡片、背景任務面板、模式切換，可以貼上或拖進圖片。每個 session 是一個子行程 `harness_vllm.py --jsonl`；只綁 `127.0.0.1`，每次啟動隨機 token、檢查 `Host`／`Origin`、CSP。**token 外洩等於交出執行權**，重啟伺服器才換新。架構、限制與測試過程見 [docs/images-and-gui.md](docs/images-and-gui.md)。

## 圖片

`view_image(path)` 讓模型看圖：session 只存參照（不存位元組），組請求時才讀檔並驗 sha256，只送最近 3 張；從檔頭判斷格式，上限 5 MB、3840×2160。預設只對 `vllm` 開；託管的第三方 provider 每張圖都會問你。

## 測試

```bash
python -X utf8 -m unittest discover -s tests          # 離線，約 200 秒
HARNESS_LIVE=1 python -X utf8 -m unittest discover -s tests   # 加上打真實伺服器的 live 測試
```

2026-11-02 在作者機器上：**484 項 OK、略過 24 項**（主要是 live 測試，加一項需要符號連結權限的測試，作者的機器沒有，從未實跑）。前端沒有自動化瀏覽器測試，是手動對真實 vLLM 驗證的。每項測試的 docstring 寫的是「為什麼這個行為重要」。實測與多輪第三方（小o，唯讀靜態）審查紀錄見 [docs/validation-and-reviews.md](docs/validation-and-reviews.md)。

## 已知限制

- **沒有 OS 級 sandbox**：shell 指令仍可讀寫專案外的檔案；寫入限制與環境清理是行程內的強制。
- 保護 `AGENT.md`、hook／MCP 設定、skills 資料夾，對 shell 只是字串比對，**可被刻意混淆**（例如用變數分段組檔名）。
- `auto` 分類器的「引用驗證」只確認引文是請求的子字串，不理解否定語境；誤判率只量過一種任務、3 次。
- 除了 `vllm`，其他 provider 只做了離線測試；模型回應與 `/cache` 的回應沒有大小上限。
- 白名單與風險字串偏 Windows／cmd；沒有在 POSIX 實跑過。
- GUI：同一個瀏覽器開 6 個以上「執行中」的 session 會用光連線；「停止」是強制結束、不能只中斷一輪。

完整清單見 [docs/reference.md](docs/reference.md)。

## 檔案配置

```
src/harness/   harness_vllm.py（主程式）、harness_net.py（URL 政策）、harness_hooks.py、harness_mcp.py、
               harness_skills.py、harness_gui.py ＋ .html ＋ _md.js（GUI）
tests/         各模組的測試（含 fake_mcp_server.py 與 live_fastmcp_server.py）
bench/         評測與自我改進迴圈（結果夾 bench_runs/ 不進 repo）
docs/          從 README 拆出的詳細文件（原文未改）
```

## 出處與授權

程式碼骨架來自 [IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)（Copyright (c) 2026 Isuru Wijesiri）：程式碼 MIT，文字 CC BY-NC-ND 4.0。`harness_vllm.py` 是以其 MIT 程式碼為基礎的衍生作品，MIT 全文見 `LICENSE-UPSTREAM-MIT`；書中文字未被改寫或轉載。

本 repo 在上游之上新增的部分同樣採 **MIT**（見 `LICENSE`，Copyright (c) 2026 Kun-Min Chiu）；上游的版權聲明保留在 `LICENSE-UPSTREAM-MIT`。
