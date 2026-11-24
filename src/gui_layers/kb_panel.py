"""The personal kb as a GUI panel: search it, ask it (retrieval first, the model gets no tools), look at the
answers it recorded and release a held one. The commands are kb's own (`kb search`, `kb ask`,
`kb answers`); this file calls them and shows their text."""
import os
import re

from .cli import run_cli

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the roy_harness checkout
ASK_ID = re.compile(r"^[A-Za-z0-9][\w.-]{0,79}$")
QUESTION_MAX = 1_000


class KbLayer:
    key = "kb"
    title = "personal kb"
    description = ("個人知識庫：搜尋，或先檢索再問模型（模型沒有工具；引文由程式核對、來源列在答案旁）。"
                   "「問」會把檢索到的段落送到你填的模型端點，所以預設關。")
    default_enabled = False
    fields = [
        {"key": "home", "label": "kb 資料夾", "default": "", "hint": "kb --home 指的資料夾（kb 沒有預設位置）"},
        {"key": "base_url", "label": "模型端點（問用）", "default": "", "hint": "例如 http://localhost:8000/v1；不填就只能搜尋"},
        {"key": "model", "label": "模型名稱（問用）", "default": ""},
        {"key": "provider", "label": "provider", "default": "vllm", "hint": "vllm / llamacpp / sglang"},
    ]

    def __init__(self, root=ROOT, runner=run_cli):
        self.root, self.runner = root, runner
        self.src = os.path.join(root, "kb", "src")

    def installed(self):
        return os.path.isdir(os.path.join(self.src, "kb"))

    def _cli(self, home, *args, timeout=60):
        # `=` forms and `--` before free text: a value that starts with "-" can never become an option
        return self.runner(["-m", "kb", f"--home={home}", *args], self.src, self.root, timeout)

    def panel(self, values):
        home = values.get("home", "").strip()
        if not self.installed():
            return {"title": "personal kb", "status": "未安裝", "blocks": [
                {"type": "text", "text": f"找不到 {self.src}。kb 是可選的一層，沒裝不影響其他功能。"}]}
        if not home:
            return {"title": "personal kb", "status": "尚未設定", "blocks": [
                {"type": "text", "text": "請先在設定頁填 kb 資料夾。"}]}
        rc, out, err = self._cli(home, "status", timeout=30)
        if rc != 0:
            return {"title": "personal kb", "status": "讀不到狀態", "blocks": [{"type": "pre", "text": (err or out).strip()}]}
        blocks = [{"type": "pre", "text": out.strip()},
                  {"type": "form", "action": "search", "placeholder": "搜尋（只查 kb，不經過模型）", "submit": "搜尋"}]
        if values.get("base_url", "").strip() and values.get("model", "").strip():
            blocks.append({"type": "form", "action": "ask", "placeholder": "問知識庫（檢索到的段落會送到你設定的模型端點）",
                           "submit": "問", "slow": True})
        else:
            blocks.append({"type": "text", "text": "要用「問」，請先在設定頁填模型端點與模型名稱。"})
        rc, out, err = self._cli(home, "answers", "list", timeout=30)
        items = []
        for line in out.splitlines()[-15:][::-1]:
            parts = line.split(None, 2)
            if len(parts) < 2 or not ASK_ID.match(parts[0]):
                continue
            buttons = [{"label": "看", "action": "show", "args": {"ask_id": parts[0]}}]
            if parts[1] == "held":
                buttons.append({"label": "放行", "action": "release", "args": {"ask_id": parts[0]},
                                "confirm": "你看過它列出的來源段落了嗎？放行是你對這個答案負責的紀錄。"})
            items.append({"text": line.strip(), "buttons": buttons})
        blocks += [{"type": "text", "text": "最近的答案"}, {"type": "list", "items": items}]
        return {"title": "personal kb", "status": "可用", "blocks": blocks}

    def act(self, values, action, args):
        home = values.get("home", "").strip()
        if not home or not self.installed():
            raise ValueError("kb 未安裝或沒設定資料夾")
        if action in ("search", "ask"):
            q = args.get("text")
            if not isinstance(q, str) or not q.strip() or len(q) > QUESTION_MAX:
                raise ValueError(f"請輸入 1 到 {QUESTION_MAX} 字的內容")
            if action == "search":
                rc, out, err = self._cli(home, "search", "-k", "8", "--", q.strip(), timeout=60)
            else:
                url, model = values.get("base_url", "").strip(), values.get("model", "").strip()
                if not (url.startswith(("http://", "https://")) and model):
                    raise ValueError("先在設定頁填模型端點（http:// 或 https://）與模型名稱")
                rc, out, err = self._cli(home, "ask", f"--base-url={url}", f"--model={model}",
                                         f"--provider={values.get('provider', '').strip() or 'vllm'}",
                                         "--timeout=300", "--", q.strip(), timeout=330)
            # `ask` exits 4 when it holds the answer back: that is a result to read, not a failure
            return {"ok": rc in (0, 4), "message": "完成" if rc in (0, 4) else f"失敗（exit {rc}）",
                    "result": (out or err).strip(), "refresh": action == "ask"}
        if action in ("show", "release"):
            ask_id = args.get("ask_id")
            if not isinstance(ask_id, str) or not ASK_ID.match(ask_id):
                raise ValueError("bad answer id")
            rc, out, err = self._cli(home, "answers", action, ask_id, *(["--by=roy (GUI)"] if action == "release" else []),
                                     timeout=30)
            return {"ok": rc == 0, "message": "完成" if rc == 0 else f"失敗（exit {rc}）",
                    "result": (out or err).strip(), "refresh": action == "release"}
        raise ValueError(f"unknown action {action!r}")
