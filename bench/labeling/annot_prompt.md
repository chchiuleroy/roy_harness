你是一位獨立的標註員。任務：替一批 GitHub issue 文字決定「哪一條規則適用」。請只依本檔與你被指定的那一個檔案作答；不要讀、不要搜尋這個資料夾以外的任何檔案，也不要上網查這些 issue。你的標註會被當成測量另一個模型的參考答案，所以請照你自己的判斷標，不要猜別人會怎麼標。

## 兩條規則（原文，不要改寫）
- `bug-report`：someone reports that something is broken, crashes, errors out or gives wrong results
- `usage-question`：someone asks how to use or configure something, or asks for help getting it to work

## 怎麼標
- 每一則訊息，標 `bug-report`、`usage-question`，或 `null`（兩條都不適用，例如功能請求、RFC、設計討論、維護雜務、公告）。
- 一則最多一個標籤。兩條都沾邊時，選更貼近發文者主要意圖的那一條。
- 你看到的文字就是模型看到的全部（已截到固定長度）；不要推測沒看到的部分。標題前綴（如 `[Bug]`、`[Usage]`）只是文字的一部分，不是答案：以內容為準。
- 真的無法決定時，把 `uncertain` 設為 `true`，並仍然給出你最傾向的標籤（或 `null`）。不要為了避免 `uncertain` 而硬選。

## 輸入
一個 JSONL 檔，每行 `{"id": "cNNNN", "text": "..."}`。

## 輸出
先簡短說你怎麼處理邊界案例（三句以內）。最後輸出**一個** JSON 區塊，內容是陣列，每個輸入一項、順序同輸入、不多不少：

```json
[{"id": "c0001", "label": "bug-report", "uncertain": false}, {"id": "c0002", "label": null, "uncertain": false}]
```

`label` 只能是 `"bug-report"`、`"usage-question"` 或 `null`。不要加提醒存檔的話。
