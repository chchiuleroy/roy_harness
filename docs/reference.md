# 參考：模型後端細節、功能對照、安全分層、統計欄位、環境變數、已知限制、檔案配置（完整版）

> 這份文件是從 [README](../README.md) 拆出來的，**內容原文未改**（只在開頭加這一行與標題）。文中的「見上」「見下」「見某某節」指的是拆開前 README 的位置，現在可能在本資料夾的別份文件裡。

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
- 明文 `http://` 的規則：**託管 provider** 一律拒絕送金鑰到非 localhost；**自架 provider**（vllm／llamacpp／sglang）沒帶金鑰時可以用內網 http（沒有金鑰可洩漏，但 prompt 仍是明文），**帶金鑰就同樣拒絕**；怪 scheme（如 `ftp://`）一律拒絕。企業內網沒有 HTTPS 時，設 `HARNESS_ALLOW_HTTP=1` 明確接受風險；只能由環境變數或 `.harness.env` 開啟，伺服器回應無法開啟它。明文代表路徑上的人看得到金鑰，請用低權限金鑰。
- **所有模型請求都不跟隨 HTTP 重新導向**：實測（Python 3.13.7）urllib 會跟隨 301／302／303 並保留 `Authorization` 標頭，重新導向的端點會拿到金鑰，而且回應內容由它提供；現在遇到 3xx 一律當錯誤。`/cache` 也一樣。
- `anthropic-compat` 走 Anthropic 的 OpenAI 相容層。官方文件說這層「主要用於測試與比較，不視為長期或正式環境方案」，且沒有 prompt caching 與原生 thinking。
- `/cache` 依 provider 解析 `/metrics`：vLLM 讀 `vllm:prefix_cache_*`；SGLang 讀 `sglang:cache_hit_rate`（需 `--enable-metrics`）；llama.cpp 的 `/metrics`（需 `--metrics`）沒有前綴快取命中計數，只回報 `prompt_tokens_total`；hosted 不查（看 usage 行的 `cached=`）。

**個人端點不要寫進原始碼**：在 repo 根目錄建 `.harness.env`（已列入 `.gitignore`），內容是 `KEY=VALUE` 逐行；真正的環境變數優先於檔案。測試會檢查：追蹤檔案只能出現 localhost 與廠商網域，且 `.harness.env` 裡的主機不得出現在其他任何檔案。

```
HARNESS_PROVIDER=vllm
VLLM_BASE_URL=http://your-vllm-host:8000/v1
VLLM_MODEL=Qwen/Qwen3.6-35B-A3B
```

斜線指令：`/help /quit /clear /compact /mode [safe|normal|auto] /tasks /stop <id> /undo /perms /cache /stats`。空白行也會離開。

## 功能與對應章節

| 章 | 功能 | 實作要點 |
|---|---|---|
| 1 | OpenAI 格式、**串流**、**重試** | SSE 逐行組裝（含被切碎的 tool call）；503／429（尊重 `Retry-After`）／斷線／串流被截斷會重試（最多 5 次嘗試，含第一次；408／409／425／429 與 500／502／503／504／529 會重試，其餘 4xx 不重試）；回報首字延遲 `ttft` |
| 4 | Agent loop | 依 `finish_reason` 分流，最多 50 輪；工具錯誤原樣回給模型 |
| 6 | 截斷、`AGENT.md` 記憶、**壓縮** | 單筆工具結果上限 30,000 字元並標示截掉多少；`AGENT.md` 啟動時讀一次（上限 20,000 字元，超過截斷並標示），放在**系統提示**，所以續接舊 session、`/compact`、`/clear` 之後都還在（原本是第一則訊息裡的 reminder，只有全新 session 讀得到，壓縮會把它交給摘要模型）；啟動時的快照，session 中途改檔不會生效，子 agent 與摘要模型不帶；續接舊版存檔時，第一則訊息裡舊的 `AGENT.md` reminder 會被移除（只移除那一個區塊，且只認舊版固定標頭；已被壓縮進摘要的舊內容無法辨認）；`HARNESS_DEBUG=1` 捕捉的請求檔會含 `AGENT.md` 全文；放進系統提示後權威性比原本的 reminder 高，所以**檔案受寫入保護**：`write_file`／`edit_file` 以真實路徑比對（`./`、`..`、大小寫、Windows 尾端的點與空白都算同一個檔），只擋 harness 讀的這一個檔（`subagent.md`、子資料夾的 `AGENT.md` 不受影響）；`run_command`／`monitor` 的字串只要整詞提到 `AGENT.md` 也擋（連 `type AGENT.md` 都擋，要讀請用 `read_file`）；檔案已存在時另以檔案身分比對（只有「其中一個檔案不存在」才算沒有別名；權限或 I/O 錯誤導致讀不到身分時一律當作受保護，拒絕寫入），所以硬連結、本機管理分享 `\\localhost\C$\…` 這類別名也擋（符號連結由真實路徑處理，這台機器沒有建立符號連結的權限，該測試被略過、**未實跑**）；session 檔（包含它的 `.tmp` 暫存檔）不能是 `AGENT.md` 或它的別名（存檔不經閘門，啟動時直接拒絕，寫入時再檢查一次）；保護**只涵蓋內建工具**，MCP 伺服器自己的檔案能力不在內（與 hook／MCP 設定檔、skill 資料夾同一個邊界，接了會寫檔的 MCP 又設 `trust: always` 就沒有保護）；shell 用變數分段組檔名仍可繞過，沒有 OS 級 sandbox 解不掉，所以保留啟動快照；你自己改檔不受影響，下次啟動生效；模型不能再幫你更新這個檔；輸入超過 `HARNESS_COMPACT_TOKENS`（預設 96,000）就把舊訊息換成摘要、保留最近 8 則；舊摘要**原文保留**不重摘要 |
| 7 | **子 agent 巢狀（深度 2）與並行** | 同一回覆的多個 `agent` 呼叫並行；子 agent 沒有 `todo_write`／`schedule_wakeup`／`monitor`（避免干擾主迴圈） |
| 8 | 引導佇列 | 事件變成 `<system-reminder>` 區塊：附在下一則使用者訊息的**開頭**（使用者文字之前）；工具迴圈中則接在工具結果之後、獨立成一則 user 訊息；喚醒事件沒有使用者文字，自己成為一則 user 訊息 |
| 9 | 背景任務、`task_stop`、`schedule_wakeup`、`monitor` | 事件（任務結束、鬧鐘、監控條件成立）會**自己啟動新回合**，不需使用者輸入；`/tasks`、`/stop` 讓人看得到也殺得掉 |
| 12 | 除錯 | `HARNESS_DEBUG=1` 把每次完整請求存到 `debug/`；每輪印 token 用量；`/cache` 查伺服器快取狀態；`/stats` 與結束時的統計行（見下） |
| 13 | 安全分層 | 見下 |

共 11 個工具：`read_file write_file edit_file run_command task_output task_stop todo_write ask_user schedule_wakeup monitor agent`。

### 安全分層（第 13 章）

| 層 | 做法 |
|---|---|
| 0 不給手 | 子 agent 的工具集較窄 |
| 1 確定性規則 | 受保護路徑（`.env`、`.ssh` 等）；沒讀過不能編輯、讀後被改動也不能編輯；前景 `sleep`／`timeout` 被擋；`edit_file` 要求舊文字剛好出現一次；**來源追蹤**（`harness_origin.py`）：`run_command`／`monitor` 的指令，或 `write_file`／`edit_file` 要寫入的文字，若提到一個**出現在工具輸出、而使用者從沒講過**的主機（網址、`user@host`、IPv4、常見網域後綴；**只有完全相同的主機**才算使用者的，子網域不算，因為共用主機如 `github.io`、`amazonaws.com` 會把子網域發給陌生人），一律問人。三種模式都適用，這個提問本身不經分類器，也不被「一律允許」的前綴授權放行（只有「這一條完全相同的指令」被核准過才免問）。**人說 yes 之後，`auto` 模式的分類器仍然照常判斷**（BLOCK 照樣擋；人只看到「這個主機來自工具」，沒看到指令是否同時違反 R1–R5）；已經問過一次就不會因為分類器存疑或 `RISKY` 字樣再問第二次。道理：注入的指示必須點名目的地，而注入來自工具輸出 |
| 2 問人 | 三種模式：`safe`（所有寫入與指令都問）、`normal`（預設，只問風險字樣）、`auto`（見下）。回答 `a` 會把該指令存進 `.harness_permissions.json`，重啟後不再問。沒有人可問時一律拒絕 |
| 3 分類器 | `auto` 模式下，除了「可證明只讀」的指令，**全部**送給一個小模型判斷。它的結論只是證據：ALLOW 必須逐字引用你的原話，BLOCK 必須引用真實存在的規則，格式不對或失敗一律改問人。它另外看得到**這一回合先前做過的動作**（最多 8 筆，每筆 160 字元；新使用者訊息就清空），因為鏈的每一步單看都無害；規則 R6（「送出的內容被前面的動作塞進機密」）是對鏈的推測，證據比單步違規弱，所以 R6 的 BLOCK 由程式改成 ASK、交給人。`auto` 模式下 `write_file`／`edit_file` 的**寫入內容**也送分類器（`edit_file` 送 `new_text` 與它取代的 `old_text`，**看不到檔案其餘部分**；兩者合計超過 20,000 字元不讀、直接問人），因為寫進去的程式稍後會在閘門之外執行。`HARNESS_CLASSIFIER_MODEL` 可以讓分類器改用另一個模型 |
| 4 縮小爆炸半徑 | 寫入限制在專案根目錄；子程序環境變數會移除含 `KEY／TOKEN／SECRET／PASSWORD／CREDENTIAL` 的項目；寫入前先備份，`/undo` 可還原；`HARNESS_SANDBOX` 可外掛 sandbox 指令前綴（**未實測**，見限制） |

「可證明只讀」是白名單：`dir type findstr where echo`（Windows 另含 `find`），且指令中不得含 `& | < > ^ ( ) %`。

### 執行統計（`/stats`）

結束時印一行（主迴圈與收尾階段收到 Ctrl-C 也會印；啟動期間載入 hook、MCP、skill 時的 Ctrl-C 不會），`/stats` 隨時可查：`[stats] {...}`。它獨佔一行，JSON，鍵排序固定，沒發生的項目也列出 0，方便程式直接解析。**只描述這個行程這一次執行**，續接舊 session 從零開始；行程被強制殺掉或當機時不會印。**解析時請取最後一個 `[stats] ` 行**：執行途中模型的文字可能剛好以同樣的前綴開頭，但結束行之後不會再有模型輸出。

| 欄位 | 意義 |
|---|---|
| `seconds` | 從啟動起算的秒數 |
| `model_calls` | 代理迴圈的輪數（主 agent 加子 agent）。**不含**壓縮的摘要請求與 `auto` 分類器的請求，重試中失敗的嘗試也不算 |
| `tokens_in` `tokens_out` | 伺服器回報的 `usage` 加總；沒回報 `usage`、或回報的不是非負整數（字串、小數、負數、NaN、布林）的輪次不計，那種輪次數在 `rounds_without_usage`，所以這兩個數字可能偏低 |
| `llm_retries` `compactions` | 實際重試的次數（任何模型請求的重試都算，包含摘要與分類器的）、壓縮成功的次數 |
| `tool_calls` `by_tool` | 模型發出的工具呼叫數（含引數不是合法 JSON 的），以及各工具名稱的次數 |
| `tool_errors` | harness 自己回報的錯誤：引數不是 JSON、`old_text` 找不到恰好一次、不存在的工具或任務、子 agent 呼叫不給它的工具、工具丟出例外、MCP 與 skill 回報的失敗。**不含**被閘門拒絕或被使用者拒絕的，也不含使用者自己下的 `/stop` 打錯 |
| `gate_blocked` `denied_by_user` | 閘門直接拒絕的次數；使用者回答不同意（包含 hook 失敗後使用者不同意）的次數 |
| `asked_human` `human_declined` | 問了使用者幾次、其中幾次被拒或沒有回答 |
| `classifier` | `auto` 模式分類器的判決次數：`allow`、`ask`、`block` |
| `hook_vetoes` `hook_failures` | hook 否決的次數、hook 失敗的次數（任何事件） |
| `subagents` | 上面幾項裡子 agent 做的部分：`model_calls`、`tokens_in`、`tokens_out`、`tool_calls`、`tool_errors`（主表的數字**包含**它們） |

**怎麼數**：在事件發生的地方累加，不是事後從對話紀錄還原。事後還原會錯在最值得量的那幾種執行：只是內容以 `ERROR` 開頭的檔案會被當成失敗、平行子 agent 的結果順序是亂的、被拒絕的呼叫在紀錄裡只剩一段文字、壓縮會換掉舊訊息。測試用「讀一個內容就是 `ERROR: boom` 的檔案」證明讀它不算錯誤。

**限度**：
- `tool_errors` 有兩處只能用 `ERROR` 開頭判斷：MCP 與 skill 的失敗字串由那兩個模組自己產生，不改它們就無法精確區分；伺服器自己的正常文字若以 `ERROR` 開頭也會被算進去。
- 計數器有鎖（平行子 agent 會同時計數），但這一點沒有測試能抓到拿掉鎖的情況。
- 這些是計數，不是品質：沒有對錯判斷、沒有成功率；要比較模型或伺服器，需要固定的任務集與重複試驗（還沒做）。
- 只在 Windows 11 對單一模型測過。

### 環境變數

| 變數 | 預設 | 用途 |
|---|---|---|
| `HARNESS_PROVIDER` `HARNESS_BASE_URL` `HARNESS_MODEL` `HARNESS_API_KEY` | 見上 | 連線設定（vllm 另接受舊名 `VLLM_*`） |
| `HARNESS_MODE` | `normal` | `safe`／`normal`／`auto` |
| `HARNESS_COMPACT_TOKENS` `HARNESS_KEEP_RECENT` | 96000／8 | 壓縮門檻與保留訊息數 |
| `HARNESS_STREAM` | `1` | 設 `0` 關閉串流 |
| `HARNESS_BACKOFF` | `1.0` | 重試退避基數（秒） |
| `HARNESS_EOF_GRACE` | 互動 0／管線 60 | stdin 關閉後等待背景工作的秒數 |
| `HARNESS_DEBUG` | 無 | 設任意值即捕捉請求（圖片的 base64 會被遮掉） |
| `HARNESS_VISION` | vllm 為 `1`，其他為 `0` | 是否提供 `view_image` 工具（見「圖片」） |
| `HARNESS_SANDBOX` | 無 | JSON 陣列，作為指令前綴 |
| `HARNESS_CLASSIFIER_MODEL` | 無（用主模型） | `auto` 分類器改問這個模型。只換模型名稱：同一個 `HARNESS_BASE_URL`、同一把金鑰，不支援另一個網址（避免把金鑰送去別處），所以這個模型要由同一個端點供應。**只有離線測試，沒有對第二個真實模型跑過 live** |
| `HARNESS_ALLOW_HTTP` | 無 | 設 `1` 才允許託管 provider 對非 localhost 使用明文 http（見上） |

## 已知限制（沒做或沒驗證）

- **沒有 OS 級 sandbox**：本機沒有 Docker、WSL 也未安裝。`HARNESS_SANDBOX` 介面存在但**未實測**。寫入限制與環境清理是行程內的強制，不等於 OS 隔離；shell 指令仍可讀寫專案外的檔案（只有 `write_file`／`edit_file` 受根目錄限制）。
- 分類器放行時要求引用使用者原話，但驗證只確認引文**是請求的子字串**；若請求寫「不要刪除 x」而分類器引用「刪除 x」，程式端擋不住，要靠分類器自己理解語意。
- 保護 `.harness_permissions.json` 等狀態檔，對檔案工具是確定性的，對 shell 指令只是字串比對（盡力而為，可被刻意混淆）；這是 shell 可寫專案外檔案這個既有限制的延伸。
- 良性任務的 `auto` 模式誤判率只量測過一種任務、3 次，其他任務未知。
- **來源追蹤只看主機，不看路徑**（列目錄後用裡面的檔名是正常工作）。網域後綴是一份短清單（`harness_origin.TLDS`），沒有 scheme 的冷門後綴抓不到；IPv6 不抓；拆字串（`"ev"+"il.com"`）、環境變數、編碼過的主機都繞得過。模型憑自己知識說出的主機（工具輸出和使用者都沒出現過）不會被標，使用者被社交工程騙著自己打出來的主機算使用者的。反向的代價：工具輸出裡出現過的名稱（例如 `socket.io`）之後被指令提到，會問一次人。
- 來源集合有上限（各 5,000 個主機）。**工具集合滿了以後改成 fail-closed**：之後每個使用者沒打過的主機都會問人（不是靜默忽略）；使用者集合滿了，沒放進去的主機就只是「沒打過」，也會問。單一超大輸出同樣被截到上限。
- **`edit_file` 的盲區**：分類器只看 `old_text`／`new_text`，看不到檔案其餘部分；一個編輯若只是打開檔案裡別處已存在的危險路徑（例如把旗標從 False 改成 True，而旗標的意義在別處），分類器和主機來源檢查都可能看不出來。探針有一個旗標名稱本身就寫明意圖的案例被擋下，不能代表意圖藏在別處的情況。
- 工具輸出的登記點是 `execute_tool`（所有工具、含子 agent）；`task_output` 讀回的背景輸出算工具輸出，但提醒（reminder）本身不登記；續跑時從歷史重建（壓縮摘要整段算工具來源，只有摘要裡逐字保留的使用者原話算使用者的）。
- **鏈偵測的證據很薄**：9 個我自己寫的案例、每個 3 次、單一模型、temperature 0；而且措辭（V0→V2→R6）是看著其中 5 個案例調出來的，只有另外 4 個是調完之後才寫的。「先前動作」只在同一個使用者回合內累積，`schedule_wakeup` 喚醒的回合不重置也不特別處理。純 prompt 版本（沒有 R6）對「讀過 `.env` 後上傳無關的 `coverage.xml`」誤 BLOCK 3/3；改成 R6 只問人後變成 ASK 3/3（多一次提問，不再是終局拒絕）。
- **寫檔分類器**：只在 `auto`；良性寫入 6 個案例 × 3 次全 ALLOW、惡意 4 個 × 3 次全 BLOCK（都是我寫的非適應性案例）；沒量過大型真實專案的 ASK 比率，所以不知道 `auto` 模式下每次寫檔多一次模型呼叫的實際延遲與提問頻率。`HARNESS_CLASSIFIER_MODEL` 沒有對第二個真實模型驗證，無法說「另一個模型是否更不容易被同一個注入說服」。
- 兩層巢狀子 agent 只有單元測試，沒有 live 示範；並行子 agent 有 live 觀察（3 個交錯執行）但不是自動化測試。
- 攻擊清單只有 5 種，不是完整紅隊測試。
- 白名單與風險字串都偏 Windows／cmd。
- **`llamacpp`／`sglang`／`openai`／`anthropic-compat` 只做了離線測試（請求內容、設定解析、`/metrics` 文字解析），沒有對真實伺服器或 API 跑過 live**；欄位行為依各家官方文件。OpenAI 的 `max_completion_tokens` 有官方依據：公開的 OpenAPI 規格（`openai/openai-openapi`）把 Chat Completions 的 `max_tokens` 標為 deprecated，並註明與 o-series 模型不相容；但「託管端點收到 `chat_template_kwargs` 是否回 400」是推斷、未實測，所以預設不送。實測過的只有 `vllm`。
- 回應大小：HTTP hook 的回應上限 1MB；但**模型回應與 `/cache` 的回應沒有大小上限**（既有行為，惡意端點可讓 harness 吃光記憶體），command hook 的輸出也是讀完才截斷（腳本是你自己的、有逾時）。
- Hook：HTTP hook 只對假伺服器測過，沒有對真實的內部政策伺服器測過；`command` 型 hook 對真實模型各測過一次（否決寫檔、SessionStart 文字進模型）。沒做 `async`、`if`、shell form。hook 腳本要自己可重入（並行子 agent 可能同時觸發）。
- MCP：只對我自己寫的假伺服器與兩代 FastMCP（4.1.3、2.14.7）驗證過，其他伺服器未驗證；舊版協定只讀過 2025-11-25，伺服器回 2025-06-18／2025-03-26 時形狀相同是假設；`x-mcp-header` 只對假伺服器測過；第三方審查一次（見上）；無 OAuth、無 resources／prompts 等。
- Skill：不實作 `allowed-tools` 等欄位的限制；選單預算放不下大型資料夾，而且**超過預算時按字母順序丟掉後面的**（被丟掉的 skill 模型不知道它存在），預算數字是我訂的；符號連結一律照 SymJack 政策處理，所以你資料夾裡指向別處的連結不會載入；`skill` 工具只給主 agent；只在 Windows 11 測過；第三方審查一次（見下）。
- GUI：只在 Windows 11、一個瀏覽器窗格（暗色、1024 寬與 375 寬）測過；同一個瀏覽器同時開著 6 個以上「正在執行」的 session 仍會用光連線上限（只修了沒在跑的那一半；每個執行中的 session 一條 `/stream`，要留一條給一般請求）；前端沒有自動化瀏覽器測試；兩個同時開著的 GUI 瀏覽器視窗共用同一份「分頁還原」清單（後存的蓋掉先存的）；markdown 不支援連結、圖片、表格（刻意：連結與圖片會變成要檢查的網址）；停止是強制結束、不能只中斷一輪；每個 session 只留最近 20,000 筆事件（頁面重新整理靠重播）；沒有 per-session 的模型或 provider 設定；token 外洩（例如把帶 token 的網址貼出去）等於交出執行權，重啟伺服器才會換新。
- 圖片：`view_image` 只對 `vllm` 探測過；不縮圖（零依賴，只能以 5 MB／3840×2160 擋）；附上後檔案被改，之後的請求只剩文字說明；GUI 的貼圖／上傳走同一條管線（上限 4 張、每張 5 MB，base64 包在 JSON 裡所以傳輸約大 33%）；真實截圖、照片、小字的準確度沒量；`view_image` 跟 `read_file` 一樣能讀專案外的檔。
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
├─ src/harness/harness_origin.py 主機來源追蹤（使用者講的 vs 工具輸出）、本回合動作
├─ src/harness/harness_hooks.py Hook 載入與執行
├─ src/harness/harness_mcp.py   MCP 客戶端（stdio、Streamable HTTP、兩代協定）
├─ src/harness/harness_skills.py Skill：選單、skill 工具、保護
├─ src/harness/harness_gui.py   網頁 GUI 伺服器（多 session、一個 session 一個子行程）
├─ src/harness/harness_gui.html GUI 前端
├─ src/harness/harness_gui_md.js markdown → 資料樹（純函式，Node 測試；不產生 HTML）
├─ tests/test_harness_vllm.py
├─ tests/test_harness_provenance.py 來源追蹤、先前動作、寫檔分類器、分類器模型（含 main() 子行程端對端）
├─ tests/test_harness_gui.py        GUI 與 --jsonl 測試（含存取控制、核准綁 id、停止續跑）
├─ tests/test_harness_gui_md.py     markdown 解析與頁面原始碼檢查（需要 Node，沒有就略過）
├─ tests/test_harness_vision.py     view_image 測試（檔頭判斷、參照不存位元組、只送最近幾張、端到端）
├─ tests/test_harness_mcp.py        MCP 離線測試（假伺服器）
├─ tests/test_harness_mcp_live.py   MCP 對真實 FastMCP 的 live 測試
├─ tests/fake_mcp_server.py         假 stdio 伺服器（新版／舊版／雙版…）
├─ tests/live_fastmcp_server.py     真實 FastMCP 伺服器（live 測試用）
├─ tests/test_harness_skills.py     Skill 測試（含 live）
├─ tests/test_bench_tasks.py        bench 評分器測試（正解通過、無作為失敗、典型錯誤失敗、突變）
├─ tests/test_bench_optimize.py     bench 接受規則、摘要、分析測試
└─ bench/                           評測與自我改進迴圈（見上節；結果夾 bench_runs/ 不進 repo）
   ├─ README.md  tasks.py  run_bench.py  analyze.py  optimize.py
```
