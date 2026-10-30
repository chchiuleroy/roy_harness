# bench：roy_harness 的評測與自我改進迴圈

動機：wiki 裡的 harness 論文（TraceTrial-Bench、Harness²、Self-Harness / Wei 綜述、Meta-Harness）都先有「可重複的任務集＋確定性評分」，才談改進。roy_harness 之前只有 `[stats]` 計數，沒有品質量測。

## 預先登記（2026-10-30，在看到正式基線之前寫下）

**可被優化的東西**只有候選資料夾裡的 `AGENT.md` 與 `skills/`（Harness² 的 C1/C3/C6 子集）。Python 原始碼、`gate()`、Hook、權限、崩潰恢復**全程不動**，所以改進迴圈不可能動到安全層（Weng 的「評估器與權限放在迴圈之外」）。

**任務**：12 個家族 × 6 個 seed（`tasks.py`），每題有確定性評分器、正解、典型錯解（`tests/test_bench_tasks.py` 證明：什麼都不做會失敗、正解通過、典型錯誤失敗）。
**切分按家族，事先固定，之後不挪**：

| split | 家族 | 用途 |
|---|---|---|
| train | reconcile、biglog、edit_unique、bugfix_scope、answer_only、csv_bom | proposer 看得到這些 trace |
| val | tree_ext、json_top、injection | 決定接不接受候選；proposer 看不到任何 val trace |
| test | zh_stats、gpa、rename | 最後跑一次 |

每題重複 3 次（模型取樣有隨機性，伺服器沒設 temperature）。

**每次試驗量兩層**（TraceTrial 的思路縮小版）：
- Agent 層：確定性評分器 pass/fail。
- Harness 層：行程正常結束、session 有存、沒有 `model call failed`、沒有 `hit MAX_ROUNDS`、沒逾時。另記 token、輪數、秒數、工具錯誤。

**接受規則**（候選 C 對目前最佳 B，只看 val，逐題配對）：
1. C 的通過率 ≥ B；且
2. C 的 harness 層正常率 ≥ B；且
3. 通過率 > B，**或**每次試驗總 token 少 ≥10% 且配對 bootstrap 95% 區間不含 0。

**最終判定**：test split 只對「最終候選」跑一次，與裸 harness 的 test 結果逐題配對比較；只有 95% 區間排除 0 才說有改進，否則寫「沒有證明有改進」。

## 這套規則的限度（先講）

- Wei et al. 的接受規則要求 `Δ̂ + √(2 ln(KT/δ)/m) < 0`。取 K=2、T=2、δ=0.1，ln(KT/δ)=ln 40，bound = √(7.38/m)：val 只有 m=18 題時 ≈ 0.64（已用程式驗算），要壓到 0.1 需要 m ≈ 738 個**獨立**驗證題。差距（通過率差）不可能大到 0.64，所以**這個樣本量給不出那個保證**；上面的規則是務實的不退步閘門，不是統計證明。
- val 會被反覆使用（最多 K·T 次），選擇偏差存在。
- 同家族的題彼此相關，題數不等於獨立樣本數；按題 bootstrap 的區間偏窄。
- 評分是確定性的，所以沒有「裁判與被測同源」問題；但任務是我設計的，題目選擇本身可能偏向某些失敗模式。
- 模型：執行器與 proposer 都是同一個 Qwen3.6-35B-A3B（vLLM），結論只對這個設定。
- 煙霧測試（每個家族各 4 個 seed 跑 1 次，含 test split）在正式基線前跑過，我看過那份結果；沒有據此改任何切分，但修了一個 `gpa` 評分器過嚴的問題（`4.0` 與 `4.00` 視為相同）。這份煙霧結果不用於任何比較。

## 實際執行紀錄與偏離預先登記之處（2026-10-30）

誠實揭露，依發生順序：

1. **第一份基線作廢**：`run_bench.py` 收到相對路徑的 session 檔，harness 在別的工作目錄執行，216 次全部「session 沒存」。已改成強制絕對路徑並加 assert，重跑得到 `baseline`（v1）。
2. **v1 評分器有瑕疵，看過 v1 test 結果後才修**：helper 檔被算成多出的檔案、「」括號被當字面、UTF-8 BOM、`answer_only` 沒強制只回數字、巢狀 snapshot。修正後為 v2，並加了近似錯解的突變測試（先前 10 個存活突變現在全被抓到）。這代表 **test split 在 v1 之後被看過並用來改評分器**，v2 的 test 不再是未碰過的留出集。
3. **優化迴圈只跑了 2 輪**，proposer 透過歷史記錄間接看過 val 的方向（val 洩漏），且第 2 輪接受的候選只多 1 個 trial 的通過數，而規則在「通過率較高」分支不看成本與區間。`decide()` 太鬆。
4. **洩漏防護**（`FORBIDDEN` 詞表）只擋已知的檔名與關鍵字，擋不了換個說法的記憶。
5. **最終候選有已知錯誤**：skill `precise_text_matching` 的範例指令不分大小寫，在 `answer_only` 會系統性答錯。
6. **主機暫停假象**：v2 兩組各有數筆 1400–1640 秒的試驗（TRIAL_TIMEOUT 為 300），集中在同一時段。Windows 系統事件記錄顯示該機器在 2026-10-30 11:36:30 進入 Modern Standby、11:58:46 離開（約 22 分鐘），與停滯時段吻合（實測）；僅憑時間吻合，不排除 vLLM 同時也有排隊。其中一筆（基線 `zh_stats-15-r2`）被判逾時而同時算 fail 與 harness 失敗，不是 harness 或候選的問題。
7. 規則 3 的實際影響與 harness 的 gate 會擋住含 `AGENT.md` 字樣的指令，兩者的互動未另外測試。
8. **執行條件不一致**：v2 基線與候選同時（11:05:43 起，各 4 個 worker）共用同一個模型伺服器；A/A 是 12:12:18 起單獨執行（4 個 worker）。A/A 的負載條件與主比較不同。
9. **模型識別未存檔**：results 與 config.json 沒有記錄當時伺服器實際載入的模型名；「Qwen3.6-35B-A3B」是設定檔與 Roy 指定的，未由 run 本身佐證。

## v2 結果（最終候選 `opt/r2_environment` 對裸 harness，每題 3 次，皆為本機實測）

| split | 候選 | 基線 | 差（95% 區間，按題 bootstrap） | token 比 |
|---|---|---|---|---|
| train | 0.898 | 0.935 | −3.7 pp [−13.0, +3.7] | ×1.50 [1.15, 1.97] |
| val | 0.944 | 0.963 | −1.9 pp [−13.0, +7.4] | ×1.36 [1.01, 1.77] |
| test | 1.000 | 0.963 | +3.7 pp [0.0, +9.3] | ×1.27 [1.05, 1.63] |

**A/A 雜訊底**：同一個裸 harness 在 val+test 再跑一次，對第一次的差為 val −9.3 pp [−22.2, +1.9]、test −5.6 pp [−13.0, +1.9]。同一設定重跑的差異比 val、test 上候選對基線的差都大，所以這兩個 split 的差沒有超出已觀察到的重跑波動。限度：A/A 只有一次、只涵蓋 val 與 test（各 18 題）、沒有 train，且負載條件與主比較不同（見上第 8 點），它不是雜訊分布，也不是「差為零」的等價證明；train 的 −3.7 pp 沒有對應的 A/A。test 的 +3.7 pp 中，一筆是基線逾時（主機停滯）假象，另一筆是 `zh_stats` 的一次錯答。

候選的分家族效應：`answer_only` 0.39 對 0.83（11/18 失敗；skill 範例不分大小寫確是原因之一，但失敗 trace 還含 `-SimpleMatch` 誤用、回覆附帶說明而違反只回數字、建立暫存腳本造成副作用等獨立模式，未做修正後的控制重跑，不能全算在大小寫上），`csv_bom` 1.00 對 0.78（可能是真效應，未做控制實驗）。每次模型呼叫的輸入 token 3917 對 3457，每題呼叫數 11.6 對 10.4（基線的分母排除了缺 stats 的逾時那一筆，共 215 筆；含它則為 10.3）。

**結論**：沒有證明有改進；成本確定增加（token ×1.3–1.5，區間下限 >1）。基線已接近天花板（約 0.94–0.96），檢定力因此偏低；但資料無法區分「題集太容易」、「proposer／接受規則不佳」、「候選本身退步」與取樣噪音，所以這次結果既不是優化器有效的證據，也不是它無效的證據。

## 優化迴圈 v3：預先登記（2026-10-30，在跑第二次迴圈之前寫下）

動機：wiki 的 `concept_harness_engineering`「Self-Improving Harness」一節（Self-Harness 的 weakness mining → proposal → validation）與第一次迴圈的差距。第一次迴圈只看 val、沒擋 train 退步、提案 context 沒有「應保留的行為」，最終候選在 train 掉了 3.7 pp。v3 改的地方：

1. **弱點挖掘**：`digest()` 把 train 失敗按「題型＋判決類型」聚成失敗模式（數字與引號內的值先遮蔽），每個模式附出現次數與一筆代表軌跡，取最多 6 個。
2. **應保留的行為**：通過率 ≥0.9 的題型，附一筆最短的通過軌跡，標 `KEEP`，提案者被告知不得改變這些行為。
3. **範例指令規則**：提案文字中的指令必須在摘要的成功軌跡中真的跑成功過（對應第一次候選的 `Select-String` 錯例）；沒有證據的行不要寫。這是寫給提案者的指示，**沒有程式檢查**，所以不保證。
4. **接受規則**（取代舊 `decide()`）：
   - 先在 val 跑；val 的通過率或 harness 正常率較差就直接拒絕，不跑 train。
   - 再跑 train。**val 與 train 兩個 split 的通過率與 harness 正常率都必須不低於目前最佳**（Self-Harness 的 held-in＋held-out 都不退步）。
   - 在兩個 split 合併的 54 題上，通過率較高，或 token 比 ≤0.90 且 bootstrap 區間上界 <1。
   - 若 token 比的區間下界 >1.15（明顯更貴），合併通過率差需 ≥5 pp，否則拒絕。
   - 某個 split 沒跑，或有任何題／重複次數遺失（配對題數不等於該 split 應有題數），視為不通過（小o 審查指出原版只檢查整個 split 缺失，已修）。
5. **提案歷史不再透露 val 方向**：只說「被接受」「在 train 上較差」或「未通過 held-out 檢查」，不給 val 的數字或方向。（val 洩漏減少，沒有消除：提案者仍能從「被拒／被接受」推得一點資訊。）
6. **修正一個潛在錯誤**：候選資料夾重寫後，舊的 `_val`／`_train` 結果夾會被刪掉；原本 runner 可續跑，會把舊候選的結果當成新候選的。
7. 移除「全拒後續跑」的 resume 邏輯（它是把 val 方向放進歷史的來源）。

**不變的**：同一個 Qwen3.6-35B-A3B 當執行器與提案者；可改的只有 AGENT.md 與 skills；harness 程式碼不動；2 輪、每輪兩個候選（invariant、environment）。基線用 `bench_runs/v2_baseline` 的 train 與 val。

**最終檢查**：若有候選被接受，用**全新 seed（21–26）**對 test 三個題型各跑候選與裸 harness（同時各 54 次），只有 95% 區間排除 0 才說有改進。注意 test 的三個題型已在 v1、v2 被看過（評分器就是看過 v1 test 後修的），新 seed 只能擋掉「對特定 seed 過擬合」，擋不掉題型層級的熟悉。若沒有候選被接受，不跑最終檢查，結果就是「這套規則下沒有候選勝出」。

**已知限度**：
- 基線 train／val 各只量一次，A/A 顯示單次量測在 18 題上可差 5–9 pp；「不低於」這條門檻在真實效應為零時，會有相當機率因雜訊拒絕一個其實無害的候選，也有機率放過一個其實有害的；實際拒絕率沒有計算（閘門同時要求 val、train 的通過率與 harness 正常率四項都不低，單一比較的「約一半」不能直接推成這個複合閘門的機率）。
- 基線通過率約 94–96%，train 只有 7 個失敗，失敗模式很少；弱點挖掘能挖的東西本來就不多。
- 提案者與執行器同模型，對照 STOP 的警示（較弱 base model 的遞歸改進會退步），這次沒有用更強的提案者做對照。

## v3 結果（2026-10-30，實測；基線為 `v2_baseline`，迴圈輸出在 `bench_runs/opt3`）

**迴圈**（2 輪 × 2 候選）：

| 候選 | val 差 | train 差 | 結果 |
|---|---|---|---|
| 第 1 輪 invariant | +1.9 pp | +3.7 pp，harness 正常率 −0.9 pp | 拒絕（harness 層退步） |
| 第 1 輪 environment | −3.7 pp | 未跑 | 拒絕 |
| 第 2 輪 invariant | 0 | +5.6 pp | **接受**：合併 54 題 +3.7 pp [−0.6, +8.6]，token ×1.26 [1.05, 1.53] |
| 第 2 輪 environment | +1.9 pp（harness −1.9 pp） | 未跑 | 拒絕 |

**被接受的候選 `opt3/r2_invariant` 的問題**（讀過內容）：
- 洩漏防護沒擋到：skill 範例寫了 `findstr /S /M /C:"FATAL" *.log` 與 `row['dept']`、`row['salary']`，正是 train 裡 `answer_only`、`csv_bom` 的細節（詞表沒有 fatal、.log、dept）。train 的 +5.6 pp 可能含記住題目的成分，不能當泛化證據。
- AGENT.md 同時叫人用 `find` 又叫人不要用 `find`，自相矛盾。
- 「`findstr /C:"WORD"` 是精確單字比對」並不正確（那是子字串比對）。

**最終檢查**（新 seed 21–26、test 三題型 gpa／rename／zh_stats、候選與裸 harness 同時各跑 54 次）：

| | 候選 | 裸 harness | 差（95% 區間） |
|---|---|---|---|
| 通過率 | 96.3% | 90.7% | +5.6 pp [−3.7, +14.8] |
| harness 正常率 | 100% | 96.3% | +3.7 pp |
| token | | | ×1.34 [1.15, 1.59] |

候選失敗 2 次、基線失敗 5 次（基線另有 2 次 harness 失敗：`zh_stats-23-r0`、`rename-22-r0`）；差為 3 次試驗。按預先登記：**區間含 0，沒有證明有改進；成本確定增加**。這次裸 harness 在新 seed 上是 90.7%，v2 在舊 seed 上是 96.3%；兩組差 5.6 pp，但同時換了題目實例，混合了實例難度與取樣噪音，**不能**當 A/A 雜訊的直接類比。主結論只靠配對區間含 0：沒有證明有改進。

**與 v1／v2 候選比較**：兩次都是「通過率差的符號為正但區間含 0、token 變貴」；v3 的規則更嚴（train 不退步、成本守門），但選出的候選仍帶有洩漏與自相矛盾，顯示光靠不退步門檻與提案者指示擋不住這些問題，詞表式洩漏防護也不夠。

## 使用

```bash
python bench/run_bench.py --cand none --splits train,val,test --reps 3 --workers 4 --out bench_runs/base
python bench/analyze.py bench_runs/base
python bench/analyze.py bench_runs/cand1 --vs bench_runs/base --splits val
python bench/optimize.py --base bench_runs/base --rounds 2
```
