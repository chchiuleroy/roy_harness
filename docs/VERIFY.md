# 怎麼驗證這個 repo（給人與代理）

這份文件是「說已驗證」的規範來源。`verify.py` 的檔頭說明它為什麼存在；這裡說怎麼用、哪些事不准做。

## 1. 入口契約

```
python verify.py                  # harness 套件，再 kb 套件；兩段一定都跑
python verify.py --only harness   # 只跑一段（harness 或 kb），迭代時用
python verify.py --only kb
```

- **兩段都跑**：harness 失敗不會隱藏 kb 的失敗，反過來也一樣。
- **跑不了算失敗，不算略過**：kb 缺 numpy／PyMuPDF（匯入名 `fitz`）時，那一段記為失敗並說明原因。
- **離線檢查**：`HARNESS_LIVE`、`SUPERAGENT_LIVE`、`SUPERAGENT_REMOVAL` 三個開關在啟動子行程前被移除，你 shell 裡殘留的變數不會讓測試連上真實伺服器。這三個要另外手動跑（見 README〈測試與驗證〉）。
- **結束碼才是依據**：0 只在所有被選的階段都通過時出現；畫面上的摘要給人看。
- `--only` 只用於迭代。**回報「已驗證」要嘛跑全套，要嘛明說只跑了哪一段。**
- 不安裝任何東西、不讀 `.harness.env`。
- 現況限度：完整跑約 16 分鐘（作者機器），沒有快速模式。

CI（`.github/workflows/ci.yml`）把同樣兩段拆成兩個 job：`offline-tests` 與 `kb-tests`。

## 2. 規則（C5）：測試是尺，不是障礙

1. **測試不為了過關而改。** 紅燈先查原因；只有在需求本身改了、且在 PR 說明裡寫出「改了什麼、為什麼」時，才改測試。把斷言放寬、把期望值寫死成當下輸出、刪掉測試，都算為了過關而改。
2. **不穩定（flaky）的測試不准跳過、不准加 `sleep`、不准加重試。** 要找根因（競態、時間假設、共用狀態），找不到就回報。
3. **同一個檢查連續失敗 3 次就停。** 保留失敗輸出，回報卡在哪；只有拿到新的證據、證明根因已經不同，才能重新計數。（「三」沿用 Anthropic 文章的自述，不是本專案實測出來的門檻。）
4. **不得悄悄降低完成條件。** 條件確實要改，明說改了什麼、為什麼。
5. **回報標證據等級**：實測（有輸出可查）／推斷（有依據但沒跑）／猜測。能自己跑的檢查，自己跑。
6. **改檢查的變更要明說。** 同一個變更若同時改了程式與驗證它的東西（`verify.py` 的階段或 `LIVE_SWITCHES`、`.github/workflows/ci.yml`、分支保護設定），PR 裡要寫出來，由人審。這是單人 repo 的信任邊界：對策是揭露加人工審查，**不是技術強制**。

## 3. 突變證據

新增的測試要附「弄壞受測程式、測試必須變紅」的證據。kb 用 `kb/tools/mutate.py`；harness 用 `tools/mutate_check.py`（標準函式庫，一次套一個字串替換，只跑指定測試，要求它失敗）。這兩個很慢，不在 `verify.py` 預設流程內，只在新增或修改測試時跑。

## 4. 已知限度

- 同一個 PR 可以改自己的檢查（見規則 6）；GitHub ruleset 能否鎖住 workflow 路徑，沒有查證。
- `verify.py` 沒有快速模式；`--only` 是迭代用的，不能代替全套。
- 本文件不描述 live 測試與移除檢查的做法，那些在 README〈測試與驗證〉。

## 5. 分支保護與緊急解除

`main` 的保護（2026-11-27 起）：必要檢查 `offline-tests` 與 `kb-tests`（就是 `ci.yml` 的兩個 job id，改名等於改保護），`enforce_admins` 開啟，所以**管理員也要等檢查通過才能合併**。`strict` 維持關閉，沒有強制 review（單人 repo）。開啟前的設定備份是 `gh api repos/chchiuleroy/roy_harness/branches/main/protection` 的輸出，存在作者機器上。

代價：Actions 或 runner 故障時，檢查會一直 pending 或不出現，連管理員都合併不了。這時依序做（`R=repos/chchiuleroy/roy_harness/branches/main/protection`）：

1. **只關 enforce_admins**：`gh api -X DELETE $R/enforce_admins`
2. 合併，釘住 head：`gh pr merge <號碼> --merge --admin --match-head-commit <head sha>`
3. **馬上還原**：`gh api -X POST $R/enforce_admins`，再讀回 `gh api $R`，確認 `enforce_admins.enabled` 是 `true`
4. 在 PR 或 commit 說明為什麼繞過、哪個檢查沒跑。

只有 `kb-tests` 壞掉（例如某個套件發行出問題）時，可以只拿掉它：`gh api -X DELETE $R/required_status_checks/contexts -f 'contexts[]=kb-tests'`，合併後加回：`gh api -X POST $R/required_status_checks/contexts -f 'contexts[]=kb-tests'`。

**不要用 `PUT $R` 整份重送**：沒列出的欄位會被預設值覆蓋。以上四個指令都只動一個欄位。

已演練（2026-11-27，實測）：加入與移除 `kb-tests`、開關 `enforce_admins`，每次都讀回，其餘欄位與備份相同。**沒演練**：步驟 2 的 `--admin` 合併（需要一個故意失敗的檢查才測得到）。
