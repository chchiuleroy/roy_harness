# superagent：疊在 roy_harness 之上的編排層（Slice 1）

> 一個**可以疊加、也可以整個拿掉**的獨立套件，放在 `src/superagent/`。它只經三條線碰 harness：子行程 `harness_vllm.py --jsonl`、每張 Ticket 目錄裡的 `.harness.hooks.json`、以及它自己組出來的環境。`src/harness/` 從不 import 它，它也不 import `src/harness/` 的任何模組。刪掉 `src/superagent/`，harness 一個位元組都不會變。
>
> 設計來自 DeepSeek Harness 團隊的 Super-agent 設計文章與開源原型（重點借用：Trigger／Thread／Ticket／Mandate 的分工、訂閱透傳、observe→auto 的灰度、先落庫再做外部動作）。**這是研究／實驗用的第一刀，不是產品**；Slice 2（模型 Main，`--assessor model`）見下，沒有 GitHub／飛書 Source。

## 做了什麼

```
inbox/*.json ─► FileInbox ─► Engine ──► SQLite（state.sqlite，唯一寫入者）
                              │  ├─ 准入：gate.admit／evaluate（確定性，無模型）
                              │  ├─ 每張 Ticket：一個目錄、一個 session 檔、一個 Mandate
                              │  ├─ 投遞：一次只送一則，先 commit 成 SENT 再寫 stdin
                              │  └─ 每張 Ticket 一個 harness 子行程（safe 模式）
                              └──► Outbox ─► outbox/<msg_id>.json
```

- **一個 Ticket＝一個目錄＋一個 session 檔＋一個 harness 行程**。目錄在 `SUPERAGENT_HOME/tickets/<id>/work`，session 與 `mandate.json` 放在它外面（模型的 `write_file` 寫不到）。
- **外部文字永遠先包框**（`frame.py`）：整則訊息以固定標頭開頭、每行加 `> ` 引用，所以不可能以 `/` 開頭（`/mode auto`、`/clear` 這類斜線命令在 hook 之前就會被 harness 執行）。
- **harness 只跑 `safe` 模式**，環境由白名單組成，不接受呼叫端加東西；`.harness.env` 的 `setdefault` 灌入被「釘空值」＋啟動前預檢擋住。
- **Mandate hook**（`mandate_hook.py`，獨立行程，對所有工具生效，`onFailure: block`）：`read_file` 限 Ticket 目錄；`write_file`／`edit_file` 限 Ticket 目錄，其餘交給 `safe` 詢問；`run_command` 禁背景；`agent`、`skill`、`view_image`、`monitor`、`schedule_wakeup`、`task_*`、`ask_user`、所有 MCP 工具一律否決；未知工具否決。
- **問題只由人回答**：harness 的核准問題（`ask`）進 SQLite 與 outbox，Ticket 轉 `waiting`；`python -m superagent answer <ask_id> y|n` 是唯一入口，只送 y／n（永不送會變成永久授權的 `a`）。`ask_id` 全域唯一，harness 重啟後 `rid` 會從 `q1` 重來，所以遲到的舊答案會被拒絕。逾時（預設 3600 秒）自動答 `n`。
- **崩潰可續**：一個事件的「入庫、決定、開 Ticket、排入第一則輸入」是**同一個 SQLite 交易**，收件檔在提交之後才移走；中途崩潰，檔案還在、資料庫沒有半截紀錄，重啟時整個重做。投遞是「至少一次」。重啟時在途的輸入變 `REPLAY`、重送時附上「可能已處理過」的標記；`harness` 自己的 `repair_interrupted` 會把沒結果的呼叫補成 `INTERRUPTED`，**不會自動重跑**。啟動時（`recover`）殺掉上一個已死的 orchestrator 留下、且能確認是自己的 harness 行程。

## 用法

先確保 `PYTHONPATH=src`（或在 `src/` 下執行）。

```bash
python -X utf8 -m superagent --home C:/Users/roy/superagent_home run \
    --allow alice --base-url http://localhost:8000/v1 --model <模型名>

# 丟一個事件進收件匣（一個事件一個 JSON 檔）
echo '{"id":"1","thread":"t1","sender":"alice","text":"請在 notes.txt 寫一行 hello","addressed":true}' \
    > C:/Users/roy/superagent_home/inbox/1.json

python -X utf8 -m superagent --home C:/Users/roy/superagent_home status
python -X utf8 -m superagent --home C:/Users/roy/superagent_home answer 1 y
python -X utf8 -m superagent --home C:/Users/roy/superagent_home stop 1
python -X utf8 -m superagent --home C:/Users/roy/superagent_home down --mode stop    # 停機，保留資料
python -X utf8 -m superagent --home C:/Users/roy/superagent_home down --mode purge   # 完全清除
```

事件檔欄位：`id`、`thread`、`sender`、`text`、`addressed`（是否直接對 agent 說話）。壞檔會移到 `inbox/bad/` 並附 `.why.txt`，不會被默默丟掉。

### 三種「拿掉」

| 語意 | 做法 | 結果 |
|---|---|---|
| 停機 | `down --mode stop` | 所有它啟動的 harness 行程結束，資料保留 |
| 移除程式 | 刪掉 `src/superagent/`（與 `tests/test_superagent_*.py`） | harness 與其測試完全不變；資料資料夾原地保留 |
| 完全清除 | `down --mode purge` | 先停機，再刪整個 `SUPERAGENT_HOME`；資料庫壞掉也能用（改以命令列掃描找行程：命令列須**同時**含 home、`--jsonl`、`harness_vllm`，只是開著 home 內檔案的編輯器或終端不會被誤殺） |

驗證「移除程式」：`SUPERAGENT_REMOVAL=1 python -X utf8 -m unittest tests.test_superagent_cli -v`（比對 `a10ef4e` 的測試結果與刪掉 superagent 後的結果，並要求 `git diff`／`git status` 在 `src/harness/` 為空）。

## Slice 2：模型 Main（`--assessor model`）

預設（`--assessor rules`）就是 Slice 1，行為完全不變。加上 `--assessor model` 後，**不是直接請求、但可能屬於某條主動規則的訊息**（寄件者在白名單但沒有對 agent 說話，或是名單上的機器人）會交給一個**模型 session** 判斷：

```bash
python -X utf8 -m superagent --home C:/Users/roy/superagent_home run \
    --allow alice --base-url http://localhost:8000/v1 --model <模型名> \
    --assessor model --rule export-bug="有人回報匯出失敗，或匯出的檔案打不開" \
    --accept-mode observe        # 先看紀錄；確定可以了再改 auto
```

- **模型只能回答「這則符合哪一條規則，或都不符合」**（一個 JSON 物件）。它**沒有任何工具**（hook 否決每一個，連 `todo_write` 都不給）。它的回答本身不改變任何東西：`gate.authorize` 與 store 決定後果。
- **誰不經過模型**：明確請求（白名單的人對 agent 說話）、同一討論串的追問透傳、沒被准入的人——全部仍然是程式碼。
- **`authorize` 是唯一出口**：規則名必須在設定的規則裡（否則記為 `unavailable`，模型寫的字不保留）；只有 `--accept-mode auto` 才會把符合變成 Ticket（`strength=inferred`、`matched_rules` 寫進不可變的 Mandate）；`observe` 只記 `would_accept`。模型寫的 `reason` 只當稽核文字（截到 300 字元），不是授權依據。store 的套用函式收的是模型原話 `(規則名或 None, reason)`，**由它自己呼叫 `gate.authorize`**；呼叫端交不進現成的決定（交了就 `TypeError`）。
- **一次一批、非阻塞**：候選累積到 `--batch-min` 或等到 `--batch-wait` 秒就成一批（上限 `--batch-max`，預設 8，範圍 1–100）；一批最多 `--eval-timeout` 秒（預設 180），逾時就停掉該 session、把候選放回佇列。**逾時先於讀答案**：過了期限才到的答案（即使已在佇列裡）不會被套用。設定值不合理（`--batch-max 0`／負數、`--batch-min` 大於 `--batch-max`、非正的逾時等）一律啟動即拒絕。同一個候選 3 次沒有得到可用答案（回覆讀不懂、漏掉它、逾時），記為 `unavailable`。
- **回覆怎麼讀**：只認「最後一個工具呼叫**之後**」模型寫的文字，從中取第一個 JSON 物件，嚴格驗證（沒問到的 id 丟掉、缺的 id 不猜、重複取第一個）。到達 50 輪上限而沒有最終回覆的，視為失敗，不會把較早輪留下的 JSON 當答案。
- **預設每一批都換新 session**（`--main-reset-batches 1`）：候選之間彼此獨立，所以不需要跨批脈絡；代價是一次污染最多影響一批（≤ `--batch-max` 則）。調大這個數字等於接受跨批累積（含壓縮摘要）的風險。
- **崩潰與重啟**：候選與「等模型」的標記和訊息是同一個交易；批次先 commit 成 `sent` 再送出；重啟時所有 `sent` 批放回佇列，**不算一次嘗試**（重啟不是候選的錯）；同一個答案不會被套用兩次（批次狀態檢查＋每個 trigger 最多一個終局 decision 的唯一索引）。重啟或規則改變時沒有 Policy 版本化：等待中的候選在**套用當下**依**當時**的規則與 accept mode 處理。
- 模型 session 也記在行程清單裡，`down --mode stop/purge` 會一併停掉；`status` 顯示 `eval` 佇列。

**這個設計的已知風險**：`authorize` 只能確認規則名有效與模式，**驗證不了語意是否真的符合**。被注入而說服了模型的最壞情形，是「那一批」裡的多則候選在 `auto` 下被開成 Ticket（每張仍受 Mandate／`safe`／hook 限制）。所以預設是 `observe`，並且每批換新 session。

C5（對真實模型的端到端）見 `tests/test_superagent_live.py`，`SUPERAGENT_LIVE=1` 才跑；結果與條件寫在它產生的報告檔。

## Outbox 的契約

回覆是 `outbox/<msg_id>.json`，內容含同一個 `msg_id`。崩潰恢復可能把同一則回覆再寫一次（同檔名、同內容）；**消費端必須以 `msg_id` 去重**。

## 已知限制（誠實清單）

- 只在 Windows 11 測過。行程偵測（`procutil`）用 Win32 API 與 PowerShell，其他系統回傳「無法確認」並因此不殺任何行程。
- `safe` 模式下 Executor 的寫檔與指令都要人答 `y`，所以它做到需要批准就停在 `waiting`，不是全自動。
- hook 的 `command` 是絕對的 `sys.executable`，harness 把 hook 所列檔名視為受保護，所以 **Ticket 內的 shell 命令不能寫 `python.exe`**（寫 `python` 可以）。
- 並行上限 `--max-running`（預設 4）只限制同時啟動的 harness 行程數，沒有為「人的追問」預留名額。
- `ask_timeout` 內若 orchestrator 不在線，逾時不會發生（逾時由 engine 的每一輪檢查）。
- 清除時殘留的孫行程（`cmd.exe` 的子行程）若其父行程已先死，命令列不含 home，掃描找不到；正常停機走 `taskkill /T` 不受影響。緊急掃描只認「命令列同時含 home、`--jsonl`、`harness_vllm`」的行程，這是刻意收窄，寧可漏掉也不誤殺。
- 答案送達才算數：`answer` 在寫進 harness 的管線成功之後才把問題標成已答；寫入失敗時問題仍開著，可以再答一次。管線寫入成功不代表 harness 已讀到，所以只要 harness 在**答案寫入之後**回報任何進展（工具結果、回合結束、下一個問題）才算「確認收到」（答案寫入時佇列裡已有的事件不算，它們不可能是這個答案的結果）；若它在那之前就結束，這筆答案改標 orphaned，狀態不會謊稱已答。
- `run` 啟動時取 home 鎖會等最多 1 秒（`status` 與 `down` 探測鎖時會瞬間佔住它）；真的有第二個 orchestrator 時，等完仍失敗並以退出碼 3 結束。
- 資料庫的舊紀錄不會被刪除，會一直成長。舊版本建立的 home 在 `Store.open()` 時會自動補上新增的欄位（目前只有 `ask.confirmed`），不需手動遷移。
- 沒有 Watchdog、BYOM／daemon、Chain of Recall、跨批脈絡、模型決定 attach／stop、GitHub／飛書 Source。
- 除了 `test_superagent_live.py`（需要 `SUPERAGENT_LIVE=1` 與真實端點），所有測試用腳本化的假模型端點。
