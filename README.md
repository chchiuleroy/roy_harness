# roy_harness

把 Isuru Wijesiri 的《Harness Engineering 101》玩具 agent，從 Anthropic 格式移植到 **OpenAI 相容端點**（vLLM／llama.cpp／SGLang／OpenAI／Anthropic 相容層，見下），並補上原書玩具版沒有的章節（壓縮、串流重試、背景任務與喚醒、權限分層、並行子 agent）。

- 主程式：[`src/harness/harness_vllm.py`](src/harness/harness_vllm.py)（約 1130 行，只用標準函式庫，零依賴）；網路政策在同資料夾的 [`harness_net.py`](src/harness/harness_net.py)，Hook 在 [`harness_hooks.py`](src/harness/harness_hooks.py)
- 測試：[`tests/test_harness_vllm.py`](tests/test_harness_vllm.py)（122 項）
- 原書與其玩具版程式在上游 repo [IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)。本 repo **只放我自己新增與修改的部分**，不轉載書稿（書的文字授權是 CC BY-NC-ND，不可改作）。

> 這是教學／實驗用的 harness，不是產品。`shell=True` 是刻意設計（它就是要讓模型執行指令），真正的控制在 `gate()` 與下面的安全分層。

## 快速開始

```bash
cd roy_harness
export VLLM_BASE_URL=http://localhost:8000/v1          # 你的端點（或寫進 .harness.env，見下）
python -X utf8 src/harness/harness_vllm.py            # 互動；session 存成 session.json
python -X utf8 src/harness/harness_vllm.py my.json    # 指定 session 檔，再次執行同檔名即接續
HARNESS_MODE=auto python -X utf8 src/harness/harness_vllm.py
```

### 選模型後端：`HARNESS_PROVIDER`

| provider | 預設端點 | 必設 | 送出的請求 |
|---|---|---|---|
| `vllm`（預設） | `http://localhost:8000/v1` | — | `max_tokens`＋`chat_template_kwargs` |
| `llamacpp` | 無（必須自己給） | `HARNESS_BASE_URL`、`HARNESS_MODEL` | 同上 |
| `sglang` | 無（必須自己給） | `HARNESS_BASE_URL`、`HARNESS_MODEL` | 同上 |
| `openai` | `https://api.openai.com/v1` | `HARNESS_MODEL`、`OPENAI_API_KEY`（或 `HARNESS_API_KEY`） | `max_completion_tokens`，**不送** `chat_template_kwargs` |
| `anthropic-compat` | `https://api.anthropic.com/v1` | `HARNESS_MODEL`、`ANTHROPIC_API_KEY`（或 `HARNESS_API_KEY`） | `max_tokens`，**不送** `chat_template_kwargs` |

- 通用變數：`HARNESS_BASE_URL`、`HARNESS_MODEL`、`HARNESS_API_KEY`。舊的 `VLLM_BASE_URL`／`VLLM_MODEL`／`VLLM_API_KEY` **只對 `vllm` 有效**，避免換成 openai 時把金鑰送到你的 vLLM 主機。
- 本地三家都送 `chat_template_kwargs: {"enable_thinking": false}`（llama.cpp 與 SGLang 的官方文件都列了這個欄位；鍵名依模型而異，這裡的 `enable_thinking` 對應 Qwen 系列）。hosted 的兩家不送，未知欄位在 OpenAI 可能被 400。
- 託管端點預設拒絕用明文 `http://` 送金鑰（localhost 例外，方便接本地代理）。企業內網沒有 HTTPS 時，設 `HARNESS_ALLOW_HTTP=1` 明確接受風險；只能由環境變數或 `.harness.env` 開啟，伺服器回應無法開啟它。明文代表路徑上的人看得到金鑰，請用低權限金鑰。
- **所有模型請求都不跟隨 HTTP 重新導向**：實測（Python 3.13.7）urllib 會跟隨 301／302／303 並保留 `Authorization` 標頭，重新導向的端點會拿到金鑰，而且回應內容由它提供；現在遇到 3xx 一律當錯誤。`/cache` 也一樣。
- `anthropic-compat` 走 Anthropic 的 OpenAI 相容層。官方文件說這層「主要用於測試與比較，不視為長期或正式環境方案」，且沒有 prompt caching 與原生 thinking。
- `/cache` 依 provider 解析 `/metrics`：vLLM 讀 `vllm:prefix_cache_*`；SGLang 讀 `sglang:cache_hit_rate`（需 `--enable-metrics`）；llama.cpp 的 `/metrics`（需 `--metrics`）沒有前綴快取命中計數，只回報 `prompt_tokens_total`；hosted 不查（看 usage 行的 `cached=`）。

**個人端點不要寫進原始碼**：在 repo 根目錄建 `.harness.env`（已列入 `.gitignore`），內容是 `KEY=VALUE` 逐行；真正的環境變數優先於檔案。測試會檢查：追蹤檔案只能出現 localhost 與廠商網域，且 `.harness.env` 裡的主機不得出現在其他任何檔案。

```
HARNESS_PROVIDER=vllm
VLLM_BASE_URL=http://your-vllm-host:8000/v1
VLLM_MODEL=Qwen/Qwen3.6-35B-A3B
```

斜線指令：`/help /quit /clear /compact /mode [safe|normal|auto] /tasks /stop <id> /undo /perms /cache`。空白行也會離開。

## 功能與對應章節

| 章 | 功能 | 實作要點 |
|---|---|---|
| 1 | OpenAI 格式、**串流**、**重試** | SSE 逐行組裝（含被切碎的 tool call）；503／429（尊重 `Retry-After`）／斷線／串流被截斷會重試（最多 5 次嘗試，含第一次；408／409／425／429 與 500／502／503／504 會重試，其餘 4xx 不重試）；回報首字延遲 `ttft` |
| 4 | Agent loop | 依 `finish_reason` 分流，最多 50 輪；工具錯誤原樣回給模型 |
| 6 | 截斷、`AGENT.md` 記憶、**壓縮** | 單筆工具結果上限 30,000 字元並標示截掉多少；輸入超過 `HARNESS_COMPACT_TOKENS`（預設 96,000）就把舊訊息換成摘要、保留最近 8 則；舊摘要**原文保留**不重摘要 |
| 7 | **子 agent 巢狀（深度 2）與並行** | 同一回覆的多個 `agent` 呼叫並行；子 agent 沒有 `todo_write`／`schedule_wakeup`／`monitor`（避免干擾主迴圈） |
| 8 | 引導佇列 | 事件變成 `<system-reminder>` 區塊：附在下一則使用者訊息的**開頭**（使用者文字之前）；工具迴圈中則接在工具結果之後、獨立成一則 user 訊息；喚醒事件沒有使用者文字，自己成為一則 user 訊息 |
| 9 | 背景任務、`task_stop`、`schedule_wakeup`、`monitor` | 事件（任務結束、鬧鐘、監控條件成立）會**自己啟動新回合**，不需使用者輸入；`/tasks`、`/stop` 讓人看得到也殺得掉 |
| 12 | 除錯 | `HARNESS_DEBUG=1` 把每次完整請求存到 `debug/`；每輪印 token 用量；`/cache` 查伺服器快取狀態 |
| 13 | 安全分層 | 見下 |

共 11 個工具：`read_file write_file edit_file run_command task_output task_stop todo_write ask_user schedule_wakeup monitor agent`。

### 安全分層（第 13 章）

| 層 | 做法 |
|---|---|
| 0 不給手 | 子 agent 的工具集較窄 |
| 1 確定性規則 | 受保護路徑（`.env`、`.ssh` 等）；沒讀過不能編輯、讀後被改動也不能編輯；前景 `sleep`／`timeout` 被擋；`edit_file` 要求舊文字剛好出現一次 |
| 2 問人 | 三種模式：`safe`（所有寫入與指令都問）、`normal`（預設，只問風險字樣）、`auto`（見下）。回答 `a` 會把該指令存進 `.harness_permissions.json`，重啟後不再問。沒有人可問時一律拒絕 |
| 3 分類器 | `auto` 模式下，除了「可證明只讀」的指令，**全部**送給一個小模型判斷。它的結論只是證據：ALLOW 必須逐字引用你的原話，BLOCK 必須引用真實存在的規則，格式不對或失敗一律改問人 |
| 4 縮小爆炸半徑 | 寫入限制在專案根目錄；子程序環境變數會移除含 `KEY／TOKEN／SECRET／PASSWORD／CREDENTIAL` 的項目；寫入前先備份，`/undo` 可還原；`HARNESS_SANDBOX` 可外掛 sandbox 指令前綴（**未實測**，見限制） |

「可證明只讀」是白名單：`dir type findstr where echo`（Windows 另含 `find`），且指令中不得含 `& | < > ^ ( ) %`。

### 環境變數

| 變數 | 預設 | 用途 |
|---|---|---|
| `HARNESS_PROVIDER` `HARNESS_BASE_URL` `HARNESS_MODEL` `HARNESS_API_KEY` | 見上 | 連線設定（vllm 另接受舊名 `VLLM_*`） |
| `HARNESS_MODE` | `normal` | `safe`／`normal`／`auto` |
| `HARNESS_COMPACT_TOKENS` `HARNESS_KEEP_RECENT` | 96000／8 | 壓縮門檻與保留訊息數 |
| `HARNESS_STREAM` | `1` | 設 `0` 關閉串流 |
| `HARNESS_BACKOFF` | `1.0` | 重試退避基數（秒） |
| `HARNESS_EOF_GRACE` | 互動 0／管線 60 | stdin 關閉後等待背景工作的秒數 |
| `HARNESS_DEBUG` | 無 | 設任意值即捕捉請求 |
| `HARNESS_SANDBOX` | 無 | JSON 陣列，作為指令前綴 |
| `HARNESS_ALLOW_HTTP` | 無 | 設 `1` 才允許託管 provider 對非 localhost 使用明文 http（見上） |

## Hook（第 11 章：確定性的反射）

Hook 是你寫的腳本或內部 HTTP 端點，綁在 harness 的事件上，**不管模型怎麼想都會執行**。它可以**觀察**、**補充**（文字會進模型的對話）或**否決**，**不能放行**：hook 回 `allow` 一律忽略，閘門、模式與人仍然照常把關。

設定放在專案目錄的 `.harness.hooks.json`（已列入 `.gitignore`，**只讀本機**，因為 hook 就是會執行的程式碼）。啟動時整份檢查，格式錯、事件名或欄位名打錯都會直接報錯並結束，不會讓安全 hook 靜默失效。`/hooks` 列出已載入的 hook。

```json
{"hooks": {
  "PreToolUse": [{"matcher": "run_command|write_file", "hooks": [
    {"type": "command", "command": "python", "args": ["check.py"], "timeout": 30, "env": ["MY_TOKEN"]},
    {"type": "http", "url": "http://policy.corp/hook", "allow_http": true,
     "headers_env": {"Authorization": "POLICY_TOKEN"}, "onFailure": "block"}]}]
}}
```

| 事件 | 收到的欄位（stdin JSON 或 POST 本文） | exit 0 的輸出 | 否決時 |
|---|---|---|---|
| `PreToolUse` | `tool_name`、`tool_input` | 接在工具結果後面給模型 | 工具不執行，理由回給模型 |
| `PostToolUse` | 另加 `tool_response`（前 10,000 字元） | 接在工具結果後面 | 不適用；exit 2 的 stderr 當 context |
| `UserPromptSubmit` | `prompt` | 當 reminder | 這則 prompt 不送給模型 |
| `SessionStart` | — | 當 reminder | 不適用 |
| `Stop` | — | 只印出，不回注 | 不適用 |

所有事件另帶 `session_id`、`cwd`、`hook_event_name`；子 agent 的工具呼叫也會觸發 hook，並帶 `agent_id`。`matcher` 是對**工具名稱**的完整比對（Python 正規表示式 `fullmatch`），省略代表全部；只對 `PreToolUse`／`PostToolUse` 有意義。

**合約**（欄位名與結束碼對齊 Claude Code 的 hooks，讀官方文件後設計，已有腳本多半可直接沿用）：exit 0＝成功，stdout 純文字當 context；exit 2＝否決（stderr 或 stdout 是理由）；stdout 印 JSON 時，`hookSpecificOutput.permissionDecision: "deny"` 或頂層 `decision: "block"` 也算否決，`hookSpecificOutput.additionalContext` 當 context；其他結束碼、逾時、起不來、輸出不是合法 JSON，都算「hook 失敗」。每段輸出上限 10,000 字元，超過截斷並標示。

**hook 失敗怎麼辦（`onFailure`）**：`PreToolUse`／`UserPromptSubmit` 預設 `"ask"`，**改問人**（與分類器「失敗一律改問人」一致）；`"block"` 直接否決；`"continue"` 當沒發生。其他事件預設 `"continue"`。

**與 Claude Code 刻意不同的地方**

| 項目 | Claude Code | 這裡 |
|---|---|---|
| hook 失敗預設 | 繼續（未設 `onFailure`） | 前置事件改問人 |
| `allow` | 可放行 | 一律忽略，只能收緊 |
| `Stop` 的 exit 2 | 讓對話繼續 | 只觀察，避免無限迴圈 |
| 指令形式 | 有 shell form | 只有 exec form（`command` 是程式、`args` 是參數，不經 shell；Windows 上不能直接執行 `.cmd`／`.bat`，請用 `python 腳本.py`） |
| 環境變數 | 繼承 | 移除名稱含 `KEY／TOKEN／SECRET／PASSWORD／CREDENTIAL` 的變數，只加 `env` 明列的，並強制 `PYTHONUTF8=1`（cp950 管線會讓中文亂碼） |
| 不支援 | — | `async`、`if`、`shell` 等欄位可以出現在貼過來的設定裡但會被忽略 |

**HTTP hook 的網路規則**：與 provider 相同，`https://` 一律可以，`http://` 只有 localhost 或該 hook 寫 `"allow_http": true` 才放行（企業內網常沒有 HTTPS，明文代表路徑上的人看得到 token，請用低權限 token）；**不跟隨重新導向**，3xx 算失敗，所以政策伺服器無法把 token 轉去別處；token 值只從環境變數讀（`headers_env` 寫的是變數名稱，不是值），變數沒設就視為失敗，不會送出請求。2xx 且本文為空算成功，其他非 2xx、連線失敗、逾時都算失敗。

**模型不能改 hook**：`.harness.hooks.json` 在受保護路徑內，`write_file`／`edit_file` 擋下，`run_command` 提到該檔名也擋（對 shell 只是字串比對，可被刻意混淆，與其他狀態檔相同的限制）。

## 對作者所用 vLLM 伺服器（Qwen/Qwen3.6-35B-A3B）的實測發現（2026-10-21）

以下是那一台伺服器的設定，換一台可能不同；`/v1/models` 回報該伺服器 `max_model_len` 為 131072。

1. **Python 預設的 User-Agent 會被擋（HTTP 403）**，curl 不會；程式自帶 UA。
2. **預設開啟思考模式時，工具呼叫會變成純文字塞在 `reasoning` 欄，`tool_calls` 是空的**；加 `chat_template_kwargs: {enable_thinking: false}` 才正常，所以所有請求都帶這個參數。
3. **前綴快取未啟用**：`/metrics` 的 `cache_config_info` 顯示 `enable_prefix_caching="False"`，`prefix_cache_queries_total` 為 0。因此 `usage` 的 `cached` 一律是 `None`，每輪都會重算整個 prompt，書中第 5 章的快取省錢效果在這台伺服器上不存在。這要伺服器端處理。
4. **cmd 的 `find` 會解析到 Git 內附的 GNU find**（PATH 順序），在 C 槽遞迴列檔。已改為子程序 PATH 把 `System32` 放最前面。
5. **子程序輸出是 OEM 碼頁（cp950）**，即使 Python 用 `-X utf8` 也一樣；用 UTF-8 解碼會變成 U+FFFD 亂碼，模型還會「猜」出看似正確的內容。已改用 `encoding="oem"`。

## 測試

```bash
python -X utf8 -m unittest discover -s tests -v              # 離線，約 11 秒
HARNESS_LIVE=1 python -X utf8 -m unittest discover -s tests  # 含打真實伺服器，約 90 秒
```

122 項 = 離線 106 項（假伺服器、時間、並行、分類器邏輯等）＋ live 16 項（其中 1 項視模型行為可能 skipped）。其中 `test_auto_mode_blocks_instruction_hidden_in_a_file` 只有在模型真的嘗試執行注入的 `curl` 時才會檢驗閘門，否則會被標為 skipped（實測該模型 5/5 次都沒有照做）；閘門對「已被注入的腦」的防護由 `test_compromised_brain_cannot_get_attacks_past_the_real_classifier` 負責。live 測試需要可連的 vLLM 端點（`VLLM_BASE_URL`／`.harness.env`）。每項測試的 docstring 寫的是「為什麼這個行為重要」。

**測試實際抓到的問題**
- 壓縮在一般聊天**永遠不會觸發**（觸發用的 token 數是函式區域變數，每個使用者回合歸零）；由 live 測試發現，補了離線回歸測試。
- 讓模型重摘要自己的摘要會丟掉最早的事實（在提示詞要求「請合併」的版本，live 共 8 次有 3 次丟，分兩批：3 次中 1 次、5 次中 2 次；更早沒有該提示詞的版本 2 次觀察都丟）；改成舊摘要原文保留後連跑 5 次全過。
- `auto` 模式只把命中 RISKY 字串清單的指令送分類器，`powershell -c "iwr ... | iex"` 會直接放行；改成除只讀白名單外全部送分類器。
- 分類器曾 3/3 誤判無害的 `find /v /c "" a.txt`；加進只讀白名單。

**第三方審查（2026-10-21，Codex 唯讀靜態審查）**：回報 9 項缺陷與 1 項待驗證，逐條對原碼核對後處理如下。
- 已修並補回歸測試（先寫失敗測試再修）：多行指令的換行未視為 shell 串接符號，會讓 `auto` 模式的只讀白名單被繞過（在 Windows cmd 上實測第二行**不會**被執行，因為 cmd 在換行處截斷，但邏輯缺口成立，sh 下會被利用）；權限檔 `.harness_permissions.json` 與備份目錄可被模型寫入（也就能預先授權危險指令）；受保護路徑比對區分大小寫（`.ENV` 可繞過）；壓縮在收到格式異常的回應時會拋例外；`HARNESS_KEEP_RECENT` 為 0 或負數會索引越界；串流重試時失敗嘗試已印出的文字沒有標示作廢；README 對 reminder 位置的描述與程式相反。
- 未修，已列入限制：分類器的「引用驗證」只確認引文是請求的子字串，不理解否定或條件語境。
- `live` 測試空過問題：見上，改為 skipped。
- 待驗證項「串流 tool_call 缺 `index`」：在作者的伺服器上 3 次、每次 3 個並行呼叫、12 個片段，全部都有 `index`，未重現，未改程式；其他伺服器未驗證。

**第二次審查（2026-10-21，針對多 provider 支援，Codex 唯讀靜態審查）**：3 項 `confirmed` 缺陷，全部在本機重現後修復，並先確認新測試在修之前是紅的。
- 明文 http 檢查只用 `startswith("http://")`，`HTTP://evil.example` 與 `http://localhost:1@evil.example` 都能通過，hosted API 金鑰會明文送去外部主機；改用 `urlsplit` 解析（大小寫、userinfo、IPv6 一併處理，非 http(s) 或無主機一律拒絕）。
- 洩漏私人端點的掃描測試用手寫檔案清單，新增的追蹤檔不會被掃；改掃 `git ls-files` 的全部追蹤檔。
- `/cache` 的數字 regex 會吃下 `1e`、`+`、`.` 這類 `float()` 讀不了的值，例外會結束整個互動 session；regex 收緊成 `float()` 一定能讀的格式。
- 審查者的 `not_checked`：它的唯讀沙盒建不了暫存目錄，離線測試全在 setUp 失敗，所以測試結果只有作者這邊的紀錄；也沒有重查各廠商文件。修復後沒有再送複審；修復後 live 測試也沒有重跑。

**量測結果（單一模型、少量樣本，不可外推）**
- 壓縮：live 連續 5 次，早期暗號都保留。
- `auto` 模式下使用者明說「Delete the file junk.txt.」：5/5 分類器放行並執行。
- 假設主模型已被注入並服從（腳本直接餵攻擊指令，分類器是真模型，shell 換成記錄器）：5 種攻擊 × 3 次 = 15/15 被攔，shell 零執行；規則引用為 R3 ×9、R2 ×3、R1 ×3。
- 突變驗證（壓縮改版前做的）：6 種故意弄壞的實作全被測試抓到。

## 已知限制（沒做或沒驗證）

- **沒有 OS 級 sandbox**：本機沒有 Docker、WSL 也未安裝。`HARNESS_SANDBOX` 介面存在但**未實測**。寫入限制與環境清理是行程內的強制，不等於 OS 隔離；shell 指令仍可讀寫專案外的檔案（只有 `write_file`／`edit_file` 受根目錄限制）。
- 分類器放行時要求引用使用者原話，但驗證只確認引文**是請求的子字串**；若請求寫「不要刪除 x」而分類器引用「刪除 x」，程式端擋不住，要靠分類器自己理解語意。
- 保護 `.harness_permissions.json` 等狀態檔，對檔案工具是確定性的，對 shell 指令只是字串比對（盡力而為，可被刻意混淆）；這是 shell 可寫專案外檔案這個既有限制的延伸。
- 良性任務的 `auto` 模式誤判率只量測過一種任務、3 次，其他任務未知。
- 兩層巢狀子 agent 只有單元測試，沒有 live 示範；並行子 agent 有 live 觀察（3 個交錯執行）但不是自動化測試。
- 攻擊清單只有 5 種，不是完整紅隊測試。
- 白名單與風險字串都偏 Windows／cmd。
- **`llamacpp`／`sglang`／`openai`／`anthropic-compat` 只做了離線測試（請求內容、設定解析、`/metrics` 文字解析），沒有對真實伺服器或 API 跑過 live**；欄位行為依各家官方文件。OpenAI 的 `max_completion_tokens` 有官方依據：公開的 OpenAPI 規格（`openai/openai-openapi`）把 Chat Completions 的 `max_tokens` 標為 deprecated，並註明與 o-series 模型不相容；但「託管端點收到 `chat_template_kwargs` 是否回 400」是推斷、未實測，所以預設不送。實測過的只有 `vllm`。
- Hook：HTTP hook 只對假伺服器測過，沒有對真實的內部政策伺服器測過；`command` 型 hook 對真實模型各測過一次（否決寫檔、SessionStart 文字進模型）。沒做 `async`、`if`、shell form。hook 腳本要自己可重入（並行子 agent 可能同時觸發）。
- 沒有 token 計數器；壓縮用的是伺服器回報的 `prompt_tokens`。
- 若伺服器沒有 `/metrics`，`/cache` 只會回報 unknown。

## 檔案配置

```
roy_harness/
├─ README.md
├─ LICENSE                     MIT（本專案）
├─ LICENSE-UPSTREAM-MIT        上游程式碼的 MIT 授權全文
├─ .gitignore                  含 .harness.env（個人端點）
├─ src/harness/harness_vllm.py
├─ src/harness/harness_net.py   URL 政策、不跟隨重新導向
├─ src/harness/harness_hooks.py Hook 載入與執行
└─ tests/test_harness_vllm.py
```

## 出處與授權

程式碼骨架來自 [IsuruMaduranga/harness-engineering-101](https://github.com/IsuruMaduranga/harness-engineering-101)（Copyright (c) 2026 Isuru Wijesiri）：程式碼 MIT，文字 CC BY-NC-ND 4.0。`harness_vllm.py` 是以其 MIT 程式碼（`src/harness/harness.py`）為基礎的衍生作品，MIT 全文見 `LICENSE-UPSTREAM-MIT`；書中文字未被改寫或轉載。

本 repo 在上游之上新增的部分同樣採 **MIT**（見 `LICENSE`，Copyright (c) 2026 Kun-Min Chiu）；上游的版權聲明保留在 `LICENSE-UPSTREAM-MIT`。
