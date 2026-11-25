"""Build the review prompt for the S1 code (head text + review contract + files). Usage: python tools/make_review_prompt.py <out file>"""
import sys

K = "C:/Users/roy/kb/"
CONTRACT_FROM = "C:/Users/roy/AppData/Local/Temp/claude/e3d9b3db15bef488/b692781f-8524-445c-acf3-26da2a3a51b2/scratchpad/gate2/head.md"
h = open(CONTRACT_FROM, encoding="utf-8").read()
contract = h[h.index("## 輸出格式（審查輸出契約 v1.1"):h.index("---\n\n## 文件一")].rstrip()

head = """這是小c（Claude Code）委派的唯讀審查任務（S1 程式碼審查：kb 的文字類全流程——收件、轉換、切塊、提交協定、FTS5 搜尋、確定性關係、CLI）。不要執行任何 shell 命令、不要上網、不要寫檔；你的最後一則訊息必須是完整回答。不要加提醒存檔的話。

## 背景
Roy 的可選知識層 kb。S0（骨架）你已審過。S1 的完成條件：文字／markdown／HTML／CSV 從收件到全文搜尋；對 roy_km/wiki 與試點資料夾可建索引；T-IN、T-W2、T-W3、T-K、T-C、T-U、T-WIN 測試全過且每個配突變；確定性關係（wikilink）當知識圖譜第一步。小c 的結果：269 個測試過；180 個突變正在背景跑（本材料不含結果）。真實資料：wiki 878 檔全部轉換成功、12,399 個 chunk；與舊系統 BM25 變體同法比較，三組基線查詢分別為 24（門檻 21）、36（門檻 36）、33（門檻 34，**未達**）。

## 請審查（依重要性）
1. **提交協定（`commit.py`）的正確性**：崩潰與復原的每個狀態（staging／verified／publishing／active／abandoned）、啟用交易、`recover()` 在各種殘留下是否一定留下可讀的 active、`rollback`、`gc`、ghost check 是否真的涵蓋 manifest 的承諾、`assemble()` 的規則（墓碑、profile、display、只計目前轉換的陳述、多值鍵）。
2. **收件（`inbox.py`、`pathsafe.py`）**：改名／重複／更新的身分規則、穩定視窗、鎖定檔、讀取期間變動、`intake_skips` 的生命週期、Windows 路徑風險（reparse point、保留名、ADS、長路徑、大小寫碰撞）。
3. **管線與單位身分（`pipeline.py`、`chunker.py`、`display.py`、schema 的 migration 2 與 3）**：單位在版本間的身分保留、offset 與身分分離是否一致、chunk 不變式（每個字元恰在一個 chunk、不跨區段）。
4. **轉換器的不可信輸入處理**（`conv_html.py`、`conv_csv.py`、`conv_markdown.py`、`conv_base.py`）：HTML 腳本與外部資源、CSV 公式、編碼回退、超大輸入。
5. **`fetch.py`**：http／https 限制、重新導向、私有位址檢查、大小上限。
6. **搜尋的誠實度（`search.py`）與關係（`relations.py`）**：狀態與過期判定、重複收合、候選與已提交的區隔、邊的拒絕與恢復。
7. **測試是否真的守護行為**：哪些測試即使實作壞掉也會綠；突變清單（`tools/mutations_s1.py`）遺漏哪些重要行為。

## 小c 已自陳的限制／偏離（不要當新發現重報，除非認為被低估並說明）
- `fetch.check_url` 先解析主機名檢查位址、之後 urllib 連線時會再解析一次：存在 DNS rebinding 的時間窗，未處理。
- 單位身分以結構鍵（種類＋標題路徑＋同標題下第 n 個同種單位）比對；在中間插入段落會使其後同標題下的單位重新版本化（已在 pipeline 文件字串說明；內容相似度比對留給 S2）。
- 本階段沒有壓縮檔解壓與媒體轉換，因此 zip slip 與損毀媒體的案例不適用（收件會把這些副檔名記為 unsupported_type）；大小寫碰撞只用純函式測試（NTFS 上無法建立兩個僅大小寫不同的名稱）。
- 候選搜尋用簡單的詞重疊計分（非 BM25），且每次都在記憶體重新轉換未收錄的來源。
- `build` 判斷「無變更」前要完整組裝一次（wiki 規模約 3 秒）；manifest 列出所有 chunk（wiki 約 6 MB），每個行程解析一次。
- 與 A0 的同法比較在 blind2 差 1 題（33 對 34）；診斷（`docs/s1_diag.json`）顯示 4 個差異全是 gold 排第 6–7 名；小c 不在基線組上調參。
- 只在 Windows 11、Python 3.13.7 測過；POSIX 路徑未跑。
- chunk 參數（800／2400）是沿用舊系統的預設，沒有調過。

""" + contract + "\n\n---\n\n"

files = ["src/kb/schema.py", "src/kb/db.py", "src/kb/tokenize.py", "src/kb/conv_base.py", "src/kb/conv_text.py", "src/kb/conv_markdown.py",
         "src/kb/conv_html.py", "src/kb/conv_csv.py", "src/kb/conv_registry.py", "src/kb/pathsafe.py", "src/kb/fetch.py", "src/kb/inbox.py",
         "src/kb/pipeline.py", "src/kb/chunker.py", "src/kb/display.py", "src/kb/ftsindex.py", "src/kb/commit.py", "src/kb/relations.py",
         "src/kb/search.py", "src/kb/inventory.py", "src/kb/cli.py", "tests/world.py", "tests/test_commit.py", "tests/test_inbox.py",
         "tests/test_pathsafe.py", "tools/mutations_s1.py"]
body = "".join(f"## 檔案：{f}\n\n```python\n{open(K + f, encoding='utf-8').read()}\n```\n\n" for f in files)
open(sys.argv[1], "w", encoding="utf-8", newline="").write(head + body)
print(len(head + body))
