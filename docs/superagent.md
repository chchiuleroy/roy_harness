# superagent：疊在 roy_harness 之上的編排層

> 一個**可以疊加、也可以整個拿掉**的獨立套件，放在 `src/superagent/`。它只經三條線碰 harness：子行程 `harness_vllm.py --jsonl`、每張 Ticket 目錄裡的 `.harness.hooks.json`、以及它自己組出來的環境。`src/harness/` 從不 import 它，它也不 import `src/harness/` 的任何模組。刪掉 `src/superagent/`，harness 一個位元組都不會變。
>
> 設計來自 DeepSeek Harness 團隊的 Super-agent 設計文章與開源原型（重點借用：Trigger／Thread／Ticket／Mandate 的分工、訂閱透傳、observe→auto 的灰度、先落庫再做外部動作）。**這是研究／實驗用的實作，不是產品**；功能是分刀加上來的（Slice 1 收件匣到工單、Slice 2 模型 Main `--assessor model`、Slice 3 結果回送與閒置結束、Slice 4 量測），各節標題標明來源；沒有 GitHub／飛書 Source。

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
    --allow alice --base-url http://localhost:8000/v1 --model <模型名> \
    --idle-end 600          # 閒置幾秒後結束做完的 Ticket（預設 600）

# 丟一個事件進收件匣（一個事件一個 JSON 檔）
echo '{"id":"1","thread":"t1","sender":"alice","text":"請在 notes.txt 寫一行 hello","addressed":true}' \
    > C:/Users/roy/superagent_home/inbox/1.json

python -X utf8 -m superagent --home C:/Users/roy/superagent_home status
python -X utf8 -m superagent --home C:/Users/roy/superagent_home answer 1 y
python -X utf8 -m superagent --home C:/Users/roy/superagent_home stop 1
python -X utf8 -m superagent --home C:/Users/roy/superagent_home down --mode stop    # 停機，保留資料
python -X utf8 -m superagent --home C:/Users/roy/superagent_home down --mode purge   # 完全清除
```

事件檔欄位：`id`、`thread`、`sender`、`text`、`addressed`（是否直接對 agent 說話）。壞檔（不是 JSON 物件、或 `text` 不是字串）會移到 `inbox/bad/` 並附 `.why.txt`，不會被默默丟掉。

**缺欄位不會被拒絕，而是用預設值**（拼錯欄位名時要留意）：沒有 `thread` → 用 `id` 當 `thread`（每則這樣的訊息自成一個討論串，追問不會接回原本的 Ticket）；沒有 `id` → 用檔名；沒有 `sender` → `unknown`（不會在任何白名單裡）；沒有 `addressed` → `false`。只有 `text` 是必填。

### 三種「拿掉」

| 語意 | 做法 | 結果 |
|---|---|---|
| 停機 | `down --mode stop` | 所有它啟動的 harness 行程結束，資料保留 |
| 移除程式 | 刪掉 `src/superagent/`（與 `tests/test_superagent_*.py`） | harness 與其測試完全不變；資料資料夾原地保留 |
| 完全清除 | `down --mode purge` | 先停機，再刪整個 `SUPERAGENT_HOME`；資料庫壞掉也能用（改以命令列掃描找行程：命令列須**同時**含 home、`--jsonl`、`harness_vllm`，只是開著 home 內檔案的編輯器或終端不會被誤殺） |

驗證「移除程式」：`SUPERAGENT_REMOVAL=1 python -X utf8 -m unittest tests.test_superagent_cli -v`（跑整套測試一次，再在刪掉 `src/superagent/` 與 `tests/test_superagent_*.py` 的副本上跑一次，兩邊 harness 測試逐項結果必須相同；兩邊都只跑離線測試）。

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
- **第二眼（預設開，`--no-confirm-inferred` 關）**：模型第一次說某則符合規則 R 時，**不開工單也不寫 decision**；同一則進「確認批次」，由**全新的 session**（引擎會自己丟掉舊的，即使你把 `--main-reset-batches` 調大）再獨立判斷一次，批次文字只有訊息與規則清單，**不含第一次的答案**。兩次說**同一個**規則才算：`auto` 開工單、`observe` 記 `would_accept`（`observe` 是 `auto` 的預演，所以也要兩次）。第二次說沒有規則、說另一個規則：記為忽略，decision 的 `model_rule` 留第二次所說，reason 寫「second look differs」；第二次的規則名不在清單：`unavailable`；第二次沒得到可用答案：沿用上面的 3 次上限（從 0 重算），用盡記 `unavailable`，reason 註明是哪個規則的確認。已進入確認階段的候選，即使之後關掉這個開關，仍會完成第二眼（不直接採信第一次、也不重問第一次）。代價：第一次命中的候選多一次模型呼叫、工單晚一個批次。限度：兩次是同一個模型、同樣的取樣設定，不是獨立專家；兩次犯相關的錯時，這道門擋不住。
- **一次一批、非阻塞**：候選累積到 `--batch-min` 或等到 `--batch-wait` 秒就成一批（上限 `--batch-max`，預設 8，範圍 1–100）；一批最多 `--eval-timeout` 秒（預設 180），逾時就停掉該 session、把候選放回佇列。**逾時先於讀答案**：過了期限才到的答案（即使已在佇列裡）不會被套用。設定值不合理（`--batch-max 0`／負數、`--batch-min` 大於 `--batch-max`、非正的逾時等）一律啟動即拒絕。同一個候選 3 次沒有得到可用答案（回覆讀不懂、漏掉它、逾時），記為 `unavailable`。
- **回覆怎麼讀**：只認「最後一個工具呼叫**之後**」模型寫的文字，要求其中**恰好一個** JSON 物件，嚴格驗證（沒問到的 id 丟掉、缺的 id 不猜、重複取第一個）。回覆裡有兩個以上 JSON 物件（例如「答案＋更正」）時整則拒絕、該批重試，因為無從判斷哪個才是模型的意思；寫壞的半成品或散文裡的大括號不算 JSON 物件，會被略過；但**包在一個從未閉合的 `{` 裡面**的合法物件不採信（模型當時在引用或打草稿，內容可能是複誦），整則重試。超過 `MAX_REPLY`（65,536 個字元，非位元組）的回覆整則拒絕，因為超出的部分從不被讀。回覆裡有 64 個以上不閉合的 `{` 時，解析器放棄並**整則拒絕**（不是只讀到放棄處為止）：無上限的重試曾讓 65,536 個大括號卡住引擎執行緒四分鐘，而「放棄後接受前面的答案」會讓人用大量括號遮住後面的更正。到達 50 輪上限而沒有最終回覆的，視為失敗，不會把較早輪留下的 JSON 當答案。
- **預設每一批都換新 session**（`--main-reset-batches 1`）：候選之間彼此獨立，所以不需要跨批脈絡；代價是一次污染最多影響一批（≤ `--batch-max` 則）。調大這個數字等於接受跨批累積（含壓縮摘要）的風險。
- **崩潰與重啟**：候選與「等模型」的標記和訊息是同一個交易；批次先 commit 成 `sent` 再送出；重啟時所有 `sent` 批放回佇列，**不算一次嘗試**（重啟不是候選的錯）；同一個答案不會被套用兩次（批次狀態檢查＋每個 trigger 最多一個終局 decision 的唯一索引）。重啟或規則改變時沒有 Policy 版本化：等待中的候選在**套用當下**依**當時**的規則與 accept mode 處理。
- 模型 session 也記在行程清單裡，`down --mode stop/purge` 會一併停掉；`status` 顯示 `eval` 佇列。

**這個設計的已知風險**：`authorize` 只能確認規則名有效與模式，**驗證不了語意是否真的符合**。被注入而說服了模型的最壞情形，是「那一批」裡的多則候選在 `auto` 下被開成 Ticket（每張仍受 Mandate／`safe`／hook 限制）。所以預設是 `observe`，並且每批換新 session；`auto` 與 `observe` 還要兩次判斷一致（見上面的「第二眼」），單次被說服不夠。

C5（對真實模型的端到端）見 `tests/test_superagent_live.py`，`SUPERAGENT_LIVE=1` 才跑；結果與條件寫在它產生的報告檔。

## Ticket 的一生（Slice 3）

```
事件 ─► 開 Ticket ─► 做事（一回合一則輸入）─► 回覆進 outbox ─► 閒置 ─► ended（行程停、槽位還）
                          ▲                                              │
                          └──────── 名單內的人在同一串再發言：重開同一張 ◄─┘
```

- **結果會回來**：每個回合結束，outbox 恰好多一則訊息——模型最後一段文字（`kind=executor_output`），或模型沒有寫任何文字時的一句固定狀態（`kind=status`，例如模型呼叫失敗）。「標輸入完成」與「排入回覆」是**同一個 SQLite 交易**，所以崩潰既不會丟掉回覆，也不會產生第二則（同一個輸入的 `msg_id` 固定為 `reply-<ticket>-<input>`）。
- **閒置就結束**：回合結束後 `--idle-end` 秒（預設 600）沒有新活動（新輸入、人答了問題、回合結束都算活動，時間存在資料庫，重啟不會歸零），而且沒有待送／在途輸入、沒有未答問題、沒有 `note`（需要人看的），Ticket 就標 `ended`、停掉行程、歸還 `--max-running` 的槽位。判斷與改狀態在一個交易裡重查全部條件；行程在提交**之後**才停，崩潰在兩者之間時，同一輪的下一個 tick 或下次啟動的 `recover` 會收掉。沒有這一步，做完的 Ticket 會永遠占著槽位（預設 4 張之後第 5 張起動不了）。
- **名單內的人再發言就重開同一張**：同一討論串上有 `ended` 的 Ticket、發言者被**目前**政策准入為明確請求，該 Ticket 就回到 `open`、同一個 session 檔續跑（模型仍看得到先前的脈絡，但 harness 的自動壓縮可能已改過舊訊息，脈絡保留是盡力而為）。Mandate 與 hook 設定逐位元不變；重開前不看舊 Mandate 的規則名，只看這則新訊息被不被准入。人 `stop` 過的 Ticket 不會重開。槽位已滿時重開只會排隊，不會擠掉別人。
- **追問要先被准入**（**行為變更**：Slice 1/2 的活 Ticket 追問是誰說的都投遞）：現在只有名單內的人（明確）的發言會送進 Ticket；名單外的人、只算 `inferred` 的機器人，以及 `accept_mode=off` 時的推斷發言，都**不投遞**，只留一筆 `ignore` 的 Decision。這比 DeepSeek 原型更嚴（原型不查發送者就扇出）。

## Slice 4：量測（`decisions`、`eval`）

這一刀**不改任何行為**：不自動切換 `auto`、不調提示詞、報告也**不寫「通過」**。它只給兩樣東西——看得見模型到底說了什麼，以及一把用人工標註語料量模型的尺。

**`decisions`（唯讀）**——`python -X utf8 -m superagent --home <資料夾> decisions [--since 時間] [--verdict accept|ignore|unavailable] [--would-accept] [--limit N] [--jsonl]`。用 `mode=ro` 開資料庫，不建檔、不改檔（有測試比對整個資料夾每個檔的內容與 mtime）；orchestrator 在跑也能看。WAL 模式的 home 與留有未回復 journal 的 home 會拒絕並說明，不去碰。文字裡的控制字元、改方向字元等一律拼成 `\uXXXX` 顯示。

**`decisions --for-labeling`**——匯出「模型看過的那些訊息」讓人標註：**只有**訊息文字（截到 `--card-cap`，並寫明被截掉幾個字元與用了哪個上限）、`sender`／`thread`、`uncertain`，**沒有**模型的判決、規則名或理由（看了會帶偏標註者）。`id` 是不透明的 `c0001`…（依檔案順序，不是資料庫主鍵，所以標註檔不能靠主鍵連回模型答案；仍可靠文字本身對回去，所以這防的是「被帶偏」，不是防刻意去查）；**刻意沒有 `label` 欄**，沒標的列會被 `eval` 的載入器拒絕，而不是被默默當成「沒有規則適用」。匯出時已被截掉的部分（`truncated_chars`）載入時仍算被截（與超過 `eval --card-cap` 的列同樣處理：沒加 `--allow-truncated` 就拒絕，加了就重放但不計分）；匯出用的 `card_cap` 與 `eval` 不同時會警告。home 沒有記錄過去那次運行用的卡片上限，所以 `--card-cap` 是你的聲明（目前 `run` 固定用預設值），必須是 1 以上的整數。這個篩選（只匯出送過模型的候選）是 Gate 2 之後加的，為了不讓規則式的 `ignore` 混進標註集。

**`decision.model_rule` 三態**：`NULL` ＝ 沒有模型答案（舊資料、規則式決定、試滿次數放棄的候選）；`''` ＝ 模型答「都不符合」；規則名 ＝ 模型選的規則（不在設定裡的名字先去控制字元再截 64 字元，決定記為 `unavailable`）。舊 home 開啟時補欄位、**不回填**——回填 `''` 會把「沒記錄」變成「模型說沒有」。

**`eval`**——`python -X utf8 -m superagent eval --corpus 語料.jsonl --rule NAME=說明 ... --base-url ... --model ... [--repeats N] [--order-check] [--no-confirm] [--hash-ids] [--out 報告.json]`。語料每行一個 JSON：`id`、`text`、`label`（規則名或 `null`）、選填 `uncertain`／`sender`／`thread`／`note`；有任何問題會**一次列出全部**再拒絕（標籤不在規則裡、超過卡片上限而沒加 `--allow-truncated`、同一討論串多列而沒加 `--allow-shared-threads` 等）。

- **含第二眼（預設，和線上一致）**：命名了規則的列會再問一次，兩次同規則才算「模型說有規則」；兩次不一致的列在指標裡算沒有規則。報告多兩個欄位：每列的 `first`（第一次所說的規則，沒進確認則 `null`）與 `confirmation`（`asked`／`confirmed`／`disagreed`／`unavailable`，以第一個 repeat 為準），`meta.confirm` 標明這次有沒有確認。`--no-confirm` 只量第一次的答案（舊的量法）。`--repeats` 仍只是穩定度，不是投票。
- **走真的引擎**：臨時資料夾裡跑真的 `Engine`＋`MainSession`（`accept_mode=observe`、`max_running=0`），一個 chunk（≤ `--batch-max` 列）一次丟進收件匣就恰好是一批。報告的 `differences` 列出和實際運行仍不同的地方（收件匣是暫存檔案、批次由語料切塊而非到達時間、每趟全新 store、標籤是人寫的）。
- **統計定義**（先定好，結果出來才不會挑）：分析單位是被標註的**列**；主要數字只看**第一個 repeat**，後面的 repeat 只用來看穩定度，不併進 n；沒得到可用答案（`unavailable`）的列**留在分母**（intention-to-classify），所以放棄作答會讓召回下降而不是讓精確度變好；區間用 Wilson（不用 Wald）；另附只算有答案列的 `diagnostic`。`uncertain` 與被截斷的列照常重播但不計分，超過 10% 會警告。
- **`conclusion` 不下判決**：只說在目標半寬（`--half-width`，預設 0.10）下每個指標「精確度達到／未達到／無資料」，給 `rows_needed`（假設各列獨立、比例不變）；另一句固定寫「尚未設定驗收門檻，本報告不說模型夠不夠好」。
- **`--order-check`**：每個 chunk 再倒序重跑一次（同一批成員、只改批內順序），報告「第一次與倒序結果不同的列數」，旁邊放「同序重跑」的基線當雜訊參考；單獨成批的列不計。這是描述性的，**不是**位置效應的估計。
- **報告不含**訊息文字、模型寫的理由、端點網址、暫存路徑；列 id、規則名與說明、模型名會留（`--hash-ids` 可把列 id 換成雜湊）。有測試鎖定欄位白名單，新增欄位要先改測試。

**親手操作的流程**（不需要真實部署的 home 也能走完）：

1. **準備訊息**：一個文字檔（一行一則）、或 JSONL（每行至少有 `text`，可有 `id`／`sender`／`thread`），或 `decisions --for-labeling` 的匯出。
2. **做標註頁**：`python -X utf8 -m superagent label --input 訊息檔 --rule NAME=說明 [--rule ...] --out label.html`。產生一個**離線單檔網頁**（不連網、不需伺服器）：一次一則，按鈕或鍵盤（數字鍵選規則、`0` 無規則、`u` 不確定、←／→ 換則、Backspace 清除）；答案自動暫存在瀏覽器裡（key 依內容指紋，不同批次不會互相污染）；文字超過卡片上限會在上限處截斷並明說，所以你標的就是模型看到的。頁面裡**沒有任何模型的回答**，訊息以純文字顯示（控制字元、改變方向的字元會拼成 `\uXXXX`）。
3. **下載**：「下載已標註的」給 `eval` 用；「下載全部」是續標用（沒標的列沒有 `label` 欄，`eval` 會拒絕它，不會默默當成「無規則」；把這個檔再丟給 `label` 就能接著標）。
4. **量測**：`python -X utf8 -m superagent eval --corpus corpus_labelled.jsonl --rule ... --base-url ... --model ... --out report.json --errors`。標註檔裡有被截斷的列（頁面上有橘字提示）要加 `--allow-truncated`（照常重放但不計分）；有多列同一個 `thread`（真實匯出可能發生）要加 `--allow-shared-threads`（區間會偏窄，報告會警告）。沒加時 `eval` 會**一次列出全部問題**再拒絕，不會跑一半。stderr 會印進度（`[eval] pass 1/3: batch 4/17`，`--quiet` 關掉）；stdout 印可讀的摘要（主要指標、逐規則、穩定度、順序敏感度、失敗、警告、報告的結論原文，加上答錯的列和訊息開頭——**訊息開頭只印在終端，不寫進 report.json**）。
5. **之後再看**：`python -X utf8 -m superagent report report.json [--errors --corpus corpus_labelled.jsonl]`。

跑一次要多久：每批一次模型呼叫；列數 ÷ `--batch-max` 就是一趟的批數，再乘上趟數（`--repeats`、加 `--order-check` 多一趟）；含第二眼時，第一次命中的列另外還要多批（每趟額外約「命中列數 ÷ `--batch-max`」批）。

**限制（誠實）**：目前**沒有**真實部署的收件匣或人工標註的真實語料，所以第一份有意義的準確度數字要等你提供標註語料；在那之前 `eval` 只有用合成語料做過「工具測試」（見該次報告，註明不是準確度證據）。`rows_needed` 與區間都假設各列獨立；同一討論串多列、或語料不代表實際流量時，區間會偏樂觀。模型取樣設定用端點預設，不同端點的結果不可直接比較。

## Outbox 的契約

回覆是 `outbox/<msg_id>.json`，內容含同一個 `msg_id`。崩潰恢復可能把同一則回覆再寫一次（同檔名、同內容）；**消費端必須以 `msg_id` 去重**。

每則訊息有 `kind`：

| `kind` | 內容 |
|---|---|
| `status` | `ticket_id`、`text`：系統自己的話（已開 Ticket、需要處理、問題、重開、回合沒有文字）。回合沒有文字而 harness 說模型呼叫失敗時（例如 `--base-url` 打錯，HTTP 404），另有 `detail`（harness 那一行的第一行，去除控制字元、最多 300 字元）與 `untrusted: true`：`detail` 是端點回的字，是資料不是指令，`text` 仍是固定句子 |
| `executor_output` | `ticket_id`、`input_id`、`text`（模型最後一段文字；去除控制字元（含 C1）、改變文字方向或隱藏文字的格式字元（保留零寬連接符，表情符號與部分文字要用）、孤立的代理字元與 Unicode 行／段分隔符，上限 4,000 個字元）、`truncated_chars`、`untrusted: true` |

**`executor_output` 的 `text` 是資料，不是指令**：它是被不可信輸入影響過的模型寫的。消費端不得把它當命令執行、當 Markdown／HTML 動作、當路由欄位，或當作下一個 agent 的控制訊息；要顯示給人看就當純文字。`status` 的 `msg_id` 目前是隨機的（只在入庫交易內產生，不會重複），回覆的是決定性的；兩種並存，待日後統一。

## 已知限制（誠實清單）

- 只在 Windows 11 測過。行程偵測（`procutil`）用 Win32 API 與 PowerShell，其他系統回傳「無法確認」並因此不殺任何行程。
- `safe` 模式下 Executor 的寫檔與指令都要人答 `y`，所以它做到需要批准就停在 `waiting`，不是全自動。
- hook 的 `command` 是絕對的 `sys.executable`，harness 把 hook 所列檔名視為受保護，所以 **Ticket 內的 shell 命令不能寫 `python.exe`**（寫 `python` 可以）。
- 並行上限 `--max-running`（預設 4）只限制同時啟動的 harness 行程數，沒有為「人的追問」預留名額（Ticket 閒置結束後會歸還槽位，但所有槽位都在做事時，新的請求與重開都只能排隊）。
- 結束一張閒置的 Ticket 時，引擎用優雅停止（送 `/quit`、最多等 5 秒）；這段時間引擎的單一執行緒不處理別的事。正在等核准的 harness 不回應 `/quit`（讀碼推斷、沒有逐一驗證），所以它們每張都會用完 5 秒。`down --mode stop` 與關機現在**同時**停所有 Ticket（以前一張接一張：7 張等核准的 Ticket 實測約 75 秒才停完，而 `down` 預設 30 秒就報錯；改後同樣情況實測約 13 秒、`status` 顯示已停、無殘留行程，單次、真實端點）；`down` 逾時的訊息改為「可能仍在關閉」並提示用 `status` 確認。
- 閒置結束的預設 600 秒是個選擇，不是量出來的：太短，等著人回應的 Ticket 會被收掉再重開（多一次行程啟動）；太長，槽位被占住。可用 `--idle-end` 調。
- `executor_output` 只是把模型的字原樣（去控制字元、截斷）放進 outbox；它不會被執行或轉成別的動作，但消費端要遵守上面的 Outbox 契約。目前的 Sink 只有本機檔案。
- `ask_timeout` 內若 orchestrator 不在線，逾時不會發生（逾時由 engine 的每一輪檢查）。
- 清除時殘留的孫行程（`cmd.exe` 的子行程）若其父行程已先死，命令列不含 home，掃描找不到；正常停機走 `taskkill /T` 不受影響。緊急掃描只認「命令列同時含 home、`--jsonl`、`harness_vllm`」的行程，這是刻意收窄，寧可漏掉也不誤殺。
- 答案送達才算數：`answer` 在寫進 harness 的管線成功之後才把問題標成已答；寫入失敗時問題仍開著，可以再答一次。管線寫入成功不代表 harness 已讀到，所以只要 harness 在**答案寫入之後**回報任何進展（工具結果、回合結束、下一個問題）才算「確認收到」（每個事件進佇列時都有流水號，答案寫入時記下當下的號碼，號碼不大於它的事件不算，因為它們在答案寫入前就已排入佇列；記號與寫入是同一個上鎖的步驟，中間排不進任何事件）；若它在那之前就結束，這筆答案改標 orphaned，狀態不會謊稱已答。
- `run` 啟動時取 home 鎖會等最多 1 秒（`status` 與 `down` 探測鎖時會瞬間佔住它）；真的有第二個 orchestrator 時，等完仍失敗並以退出碼 3 結束。
- 資料庫的舊紀錄不會被刪除，會一直成長。舊版本建立的 home 在 `Store.open()` 時會自動補上新增的欄位（目前有 `ask.confirmed` 與 `decision.model_rule`；後者舊列保持 `NULL`），不需手動遷移。
- 沒有 Watchdog、BYOM／daemon、Chain of Recall、跨批脈絡、模型決定 attach／stop、GitHub／飛書 Source。
- 除了 `test_superagent_live.py`（需要 `SUPERAGENT_LIVE=1` 與真實端點），所有測試用腳本化的假模型端點。
