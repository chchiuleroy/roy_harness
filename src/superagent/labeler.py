"""A single offline HTML page for labelling messages by hand, and the reader that feeds it.

Input: a file of messages that have NOT been answered by the model -- the blind export of `decisions
--for-labeling`, a JSONL file with at least a `text` per line, or a plain text file with one message per
line. Output: a self-contained page (no network, no server) that shows one message at a time with one
button per rule, "no rule" and "unsure", and downloads the result as a corpus `eval` can read.

Choices that matter:
* Text is cut to `card_cap` here, the same way the blind export does, so the person labels what the model
  will read, and the cut is recorded (`truncated_chars`, `card_cap`).
* The page never receives anything the model said. It only gets what is in the input rows.
* Message text is shown with `textContent` (never as HTML) and unsafe characters (controls, direction
  changes, hidden characters) are spelled as \\uXXXX on screen; the downloaded text is the original.
* A row nobody labelled is downloaded WITHOUT a `label` key, so `eval` refuses it instead of counting it
  as 'no rule applies'. The page offers a labelled-only download for `eval` and an all-rows download to
  resume later (labels already in the file are shown again).
"""
import hashlib
import json

from . import sanitize

MAX_ROWS = 5000


class LabelerError(ValueError):
    pass


def read_unlabelled(path, card_cap):
    """-> list of row dicts: id, text, sender, thread, truncated_chars, card_cap, label (key present only if
    the file already had one), uncertain."""
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln.rstrip("\r") for ln in f.read().split("\n") if ln.strip()]   # not splitlines(): see corpus.load
    if not lines:
        raise LabelerError("the file has no messages")
    if len(lines) > MAX_ROWS:
        raise LabelerError(f"{len(lines)} messages; the page takes at most {MAX_ROWS}")
    rows, used, problems = [], set(), []
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError as e:
                problems.append(f"line {n}: not valid JSON ({e})")
                continue
            text = obj.get("text") if isinstance(obj, dict) else None
        else:
            obj, text = {}, line
        if not isinstance(text, str) or not text.strip():
            problems.append(f"line {n}: no text")
            continue
        rid = obj.get("id")
        if rid is not None and (not isinstance(rid, str) or not rid.strip()):
            problems.append(f"line {n}: id must be a non-empty string")
            continue
        if rid is not None:
            if rid in used:
                problems.append(f"line {n}: id {rid!r} is used twice")
                continue
            used.add(rid)
        exported_cut = obj.get("truncated_chars", 0)
        if isinstance(exported_cut, bool) or not isinstance(exported_cut, int) or exported_cut < 0:
            problems.append(f"line {n}: truncated_chars must be a whole number, 0 or more")
            continue
        rows.append({"id": rid, "text": text, "sender": obj.get("sender"), "thread": obj.get("thread"),
                     "exported_cut": exported_cut, "label_in_file": "label" in obj, "label": obj.get("label"),
                     "uncertain": obj.get("uncertain") is True, "line": n})
    if problems:
        raise LabelerError("; ".join(problems[:20]) + (f"; and {len(problems) - 20} more" if len(problems) > 20 else ""))
    counter = 0
    for r in rows:                                                 # give ids to the rows that have none
        if r["id"] is None:
            while True:
                counter += 1
                cand = f"c{counter:04d}"
                if cand not in used:
                    break
            r["id"] = cand
            used.add(cand)
    out = []
    for r in rows:
        shown = r["text"][:card_cap]
        cut = r["exported_cut"] + len(r["text"]) - len(shown)
        item = {"id": r["id"], "text": shown, "sender": r["sender"] if isinstance(r["sender"], str) else "labeler",
                "thread": r["thread"] if isinstance(r["thread"], str) else f"t-{r['id']}",
                "truncated_chars": cut, "card_cap": card_cap, "uncertain": r["uncertain"]}
        if r["label_in_file"]:
            item["label"] = r["label"]
        out.append(item)
    return out


def build_html(rows, rules, title="標註訊息"):
    """-> the page as a str. `rules` is a list of gate.Rule."""
    fingerprint = hashlib.sha256(json.dumps([(r["id"], r["text"]) for r in rows] + [(r.name, r.instruction) for r in rules],
                                            ensure_ascii=False).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    data = {"fingerprint": fingerprint, "rules": [{"name": r.name, "instruction": r.instruction} for r in rules],
            "rows": rows}
    # JSON inside a <script> block: no "<" may appear raw ("</script>" ends the block, "<!--<script>" changes how
    # the parser reads it), and no U+2028/9; json_safe spells the latter, < is "<" and parses back to it
    blob = sanitize.json_safe(json.dumps(data, ensure_ascii=False)).replace("<", "\\u003c")
    return PAGE.replace("@@TITLE@@", _esc(title)).replace("@@DATA@@", blob)


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1c2230; --muted:#5b6578; --line:#d9dee7; --accent:#2757d6;
          --ok:#1b7f4b; --warn:#b3541e; --btn:#eef1f6; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { --bg:#14171d; --card:#1d222b; --ink:#e7eaf0; --muted:#98a2b5;
          --line:#323a49; --accent:#6f95ff; --ok:#56c88a; --warn:#f0a070; --btn:#272e3a; } }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.55 system-ui,"Noto Sans TC",sans-serif; }
  main { max-width:820px; margin:0 auto; padding:16px; }
  h1 { font-size:1.15rem; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:.9rem; margin-bottom:12px; }
  .bar { height:8px; background:var(--btn); border-radius:4px; overflow:hidden; margin:8px 0 4px; }
  .bar > div { height:100%; background:var(--accent); width:0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin:12px 0; }
  .meta { color:var(--muted); font-size:.85rem; margin-bottom:8px; }
  #text { white-space:pre-wrap; word-break:break-word; font-size:1.02rem; max-height:52vh; overflow:auto; }
  .cut { color:var(--warn); font-size:.9rem; margin-top:8px; }
  .buttons { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
  button { font:inherit; padding:9px 14px; border:1px solid var(--line); border-radius:8px; background:var(--btn);
           color:var(--ink); cursor:pointer; text-align:left; }
  button:hover { border-color:var(--accent); }
  button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  button small { display:block; color:inherit; opacity:.75; font-size:.78rem; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
  .status { color:var(--muted); font-size:.9rem; }
  .keys { color:var(--muted); font-size:.82rem; margin-top:6px; }
  #done { color:var(--ok); }
</style>
</head>
<body>
<main>
  <h1>@@TITLE@@</h1>
  <div class="sub">每則訊息選一個答案。你看到的文字就是模型會看到的文字；這裡沒有模型的任何回答。</div>
  <div class="bar"><div id="fill"></div></div>
  <div class="status" id="progress"></div>
  <div class="card">
    <div class="meta" id="meta"></div>
    <div id="text"></div>
    <div class="cut" id="cut" hidden></div>
    <div class="buttons" id="buttons"></div>
    <div class="keys" id="keys"></div>
  </div>
  <div class="row">
    <button id="prev">← 上一則</button>
    <button id="next">下一則 →</button>
    <button id="nextOpen">跳到下一則未標</button>
    <span class="status" id="saved"></span>
  </div>
  <div class="row">
    <button id="dlLabelled">下載已標註的（給 eval 用）</button>
    <button id="dlAll">下載全部（之後續標用）</button>
    <span class="status" id="done"></span>
  </div>
</main>
<script id="data" type="application/json">@@DATA@@</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("data").textContent);
  var rules = DATA.rules, rows = DATA.rows;
  var KEY = "superagent-labeler:" + DATA.fingerprint;     // content-based: another file with the same ids is another job
  // a Map, not a plain object: an id such as "constructor" or "__proto__" must not look answered
  var answers = new Map();                // id -> {label: name|null, uncertain: bool}
  var cur = 0;

  rows.forEach(function (r) {
    if ("label" in r) { answers.set(r.id, { label: r.label, uncertain: !!r.uncertain }); }
    else if (r.uncertain) { answers.set(r.id, { label: null, uncertain: true }); }
  });
  try {
    var saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved && Array.isArray(saved.answers)) { saved.answers.forEach(function (pair) { answers.set(pair[0], pair[1]); });
      cur = Math.min(saved.cur || 0, rows.length - 1); }
  } catch (e) { /* no storage: the downloads still work */ }

  var UNSAFE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/gu;
  function spell(s) {
    return s.replace(UNSAFE, function (ch) {
      if (ch === "\n" || ch === "\t" || ch === "\u200c" || ch === "\u200d") { return ch; }
      var spelled = "";
      for (var i = 0; i < ch.length; i++) { spelled += "\\u" + ch.charCodeAt(i).toString(16).padStart(4, "0"); }
      return spelled;
    });
  }
  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify({ answers: Array.from(answers.entries()), cur: cur }));
          document.getElementById("saved").textContent = "已自動暫存在這個瀏覽器"; }
    catch (e) { document.getElementById("saved").textContent = "瀏覽器不讓暫存：記得常下載「全部」"; }
  }
  function count() { return rows.filter(function (r) { return answers.has(r.id); }).length; }

  function render() {
    var r = rows[cur], a = answers.get(r.id);
    document.getElementById("meta").textContent = "第 " + (cur + 1) + " / " + rows.length + " 則　" + r.id +
      "　寄件者 " + spell(r.sender) + "　串 " + spell(r.thread);
    document.getElementById("text").textContent = spell(r.text);
    var cut = document.getElementById("cut");
    cut.hidden = !r.truncated_chars;
    cut.textContent = r.truncated_chars ? "這則在 " + r.card_cap + " 字處被截斷，後面 " + r.truncated_chars +
      " 字模型看不到，你也看不到；請只就上面的文字回答。" : "";
    var box = document.getElementById("buttons");
    box.textContent = "";
    function add(label, hint, key, on, fn) {
      var b = document.createElement("button");
      b.className = on ? "on" : "";
      b.textContent = (key ? "[" + key + "] " : "") + label;
      if (hint) { var s = document.createElement("small"); s.textContent = hint; b.appendChild(s); }
      b.addEventListener("click", fn);
      box.appendChild(b);
    }
    rules.forEach(function (rule, i) {
      add(rule.name, rule.instruction, i < 9 ? String(i + 1) : "", a && !a.uncertain && a.label === rule.name, function () { choose(rule.name, false); });
    });
    add("都不是（無規則）", "這則不屬於任何一條規則", "0", a && !a.uncertain && a.label === null, function () { choose(null, false); });
    add("不確定", "照常重放，但不計分", "u", a && a.uncertain, function () { choose(null, true); });
    document.getElementById("keys").textContent = "鍵盤：數字鍵 1–9 選前九條規則（其餘用滑鼠）、0 無規則、u 不確定、← → 換則、Backspace 清除這則的答案";
    var n = count();
    document.getElementById("progress").textContent = "已標 " + n + " / " + rows.length;
    document.getElementById("fill").style.width = (100 * n / rows.length) + "%";
    document.getElementById("done").textContent = n === rows.length ? "全部標完了，可以下載「已標註的」" : "";
  }
  function choose(label, uncertain) {
    answers.set(rows[cur].id, { label: label, uncertain: uncertain });
    persist();
    if (cur < rows.length - 1) { cur += 1; }
    render();
  }
  function go(i) { cur = Math.max(0, Math.min(rows.length - 1, i)); persist(); render(); }
  function nextOpen() {
    for (var k = 1; k <= rows.length; k++) {
      var j = (cur + k) % rows.length;
      if (!answers.has(rows[j].id)) { go(j); return; }
    }
  }
  function lines(onlyLabelled) {
    var out = [];
    rows.forEach(function (r) {
      var a = answers.get(r.id);
      if (onlyLabelled && !a) { return; }
      var o = { id: r.id, text: r.text, sender: r.sender, thread: r.thread };
      if (r.truncated_chars) { o.truncated_chars = r.truncated_chars; }
      o.card_cap = r.card_cap;
      if (a) { o.label = a.uncertain ? null : a.label; o.uncertain = !!a.uncertain; }
      out.push(JSON.stringify(o));
    });
    return out.join("\n") + "\n";
  }
  function download(name, text) {
    var url = URL.createObjectURL(new Blob([text], { type: "application/x-ndjson" }));
    var a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  document.getElementById("prev").addEventListener("click", function () { go(cur - 1); });
  document.getElementById("next").addEventListener("click", function () { go(cur + 1); });
  document.getElementById("nextOpen").addEventListener("click", nextOpen);
  document.getElementById("dlLabelled").addEventListener("click", function () { download("corpus_labelled.jsonl", lines(true)); });
  document.getElementById("dlAll").addEventListener("click", function () { download("corpus_all_progress.jsonl", lines(false)); });
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    if (e.key === "ArrowLeft") { go(cur - 1); }
    else if (e.key === "ArrowRight") { go(cur + 1); }
    else if (e.key === "Backspace") { answers.delete(rows[cur].id); persist(); render(); e.preventDefault(); }
    else if (e.key === "0") { choose(null, false); }
    else if (e.key === "u" || e.key === "U") { choose(null, true); }
    else if (/^[1-9]$/.test(e.key) && Number(e.key) <= rules.length) { choose(rules[Number(e.key) - 1].name, false); }
  });
  render();
})();
</script>
</body>
</html>
"""
