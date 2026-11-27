# bench/labeling：用兩位獨立標註員做出 `superagent eval` 的參考語料

把 2026-11-06 那次「公開 GitHub issue 語料＋兩個 AI 盲標」的做法做成可重跑的腳本（只用標準函式庫）。**資料不進這個 repo**：issue 文字是別人的，全部放在 `--work-dir` 指定的資料夾。

這是量測 superagent 規則判斷用的**參考答案**，不是人工標註；它的限度在最後一節。

## 流程

```
fetch ──► candidates.jsonl (+ .sha256) ──► blind ──► annot/chunkN.jsonl ──► （兩位標註員，腳本外）──► merge
fetch-usage（選配）─► candidates2.jsonl ──► blind（第二批）                                        │
                                                                          prompts ─► prompts/codex_chunkN.md
merge ─► labelled_agreed.jsonl（進 `superagent eval --corpus`）、disputed.jsonl、agreement.json
```

只有 `fetch`／`fetch-usage` 取決於 GitHub 今天的內容，**不可重現**；它們存下的檔案與 sha256 是之後一切的起點。`blind`、`prompts`、`merge` 都是確定性的：同樣的輸入，輸出逐位元相同。

## 指令（都要 `--work-dir`）

```
python -X utf8 bench/labeling/labeling.py fetch --work-dir D --repos vllm-project/vllm=60,ollama/ollama=60 \
    --since 2026-09-15T00:00:00Z --seed 20261106 [--exclude-ids 舊試驗.jsonl] [--cap 700]
python -X utf8 bench/labeling/labeling.py fetch-usage --work-dir D --seed 20261107 \
    --plan "vllm-project/vllm:usage:2026-05-01:30,ollama/ollama:question:2026-05-01:30" \
    [--carry 舊試驗.jsonl --carry-repo vllm-project/vllm --carry-stratum trial-1104]
python -X utf8 bench/labeling/labeling.py blind --work-dir D --pool candidates.jsonl
python -X utf8 bench/labeling/labeling.py blind --work-dir D --pool candidates2.jsonl --id-start 240 --first-chunk 5 --mapping mapping2.json
python -X utf8 bench/labeling/labeling.py prompts --work-dir D
python -X utf8 bench/labeling/labeling.py merge --work-dir D --pool candidates.jsonl:random-draw --pool candidates2.jsonl
```

第二批 `blind` 的 `--id-start` 要等於第一批的列數，`--first-chunk` 要接在第一批最後一個 chunk 之後。設錯時：`blind` 發現目標檔已存在且內容不同就拒絕（相同資料重跑沒事，`--force` 才覆寫）；`merge` 發現兩批用了同樣的盲標 id、同一個盲標 id 出現在兩個 chunk、盲標時的文字與候選檔不同、mapping 指向不存在或重複的 issue，也都拒絕。

## 腳本外的一步：標註

兩位標註員各自獨立，只讀被指定的那一個 chunk（`annot/chunkN.jsonl`，只有 `{id, text}`），依 `annot_prompt.md` 作答：

- 一位把回答存成 `results_codex/outN.md`（腳本取**最後一個** ```json 區塊；做法是 `codex exec`，唯讀、工作目錄 `annot/`，提示用 `prompts/codex_chunkN.md`）。
- 另一位存成 `results_claude/chunkN.json`（一個 JSON 陣列）。
- 每項 `{"id", "label": 規則名或 null, "uncertain": true/false}`，順序同輸入、不多不少；`merge` 對任何不符的檔案會直接拒絕。
- 規則文字（`annot_prompt.md` 裡的兩條）要與你拿去 `eval --rule` 的**逐字相同**；換規則就改這個檔，並用 `merge --rules a,b`。

## 輸出

- `labelled_agreed.jsonl`：兩人標籤相同、而且都沒標 `uncertain` 的列，**這才是計分語料**（`note` 欄是來源層）。
- `disputed.jsonl`：其餘列（不計分）；`labelled_codex_all.jsonl`／`labelled_claude_all.jsonl`：單一標註員的完整標籤，做敏感度用。
- `agreement.json`：列數、原始一致率、Cohen's kappa（只當描述，類別分布很偏）、各來源層與各 repo 的細目。

## 驗證過什麼

2026-11-27：用 2026-11-06 存下的 322 列快照與兩位標註員的原始輸出，重跑 `blind`（兩批）、`prompts`、`merge`，19 個產物（6 個 chunk、6 份提示、2 個 mapping、5 個語料與報告）與當時的檔案逐位元相同（只差換行：舊檔在 Windows 以 CRLF 寫出，新程式一律寫 LF）。**`fetch`／`fetch-usage` 沒有對真實 GitHub 重跑**（結果會隨時間變，只用假的 `gh` 輸出測試過篩選與配額）。

## 限度

- 參考答案是兩個 AI 的共識，**不是人工標註**；挑「兩人一致且都不猶豫」的列會選到比較容易的案例，使被測模型的數字偏樂觀。
- 欄位盲，**內容不盲**：文字裡仍有網址、標題類型前綴（`[Bug]`、`[Usage]`）等線索，被測模型看到同樣的文字。
- Claude 子 agent 那一路沒有可稽核的工具紀錄；`codex exec` 那一路的 log 可查，但唯讀沙箱不是路徑隔離。
- `fetch-usage` 是**分層抽樣，不是隨機**：目的是讓 usage 規則有足夠的列數，合併後的類別比例是設計選擇。
