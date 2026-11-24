"""The superagent layer as a GUI panel: what is going on (read through `status`) and the three things a
person does by hand (answer a question y/n, stop a ticket, resume a parked one). Every action is the
command line the README already documents; this file only calls it."""
import json
import os

from .cli import run_cli

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # roy_harness/src
DEFAULT_HOME = "C:/Users/roy/superagent_home"


class SuperagentLayer:
    key = "superagent"
    title = "superagent"
    description = ("疊在 harness 上的編排層：收件匣的事件經准入變成 Ticket，每張 Ticket 一個受限的 harness 行程。"
                   "這個面板顯示它的狀態，並讓你回答它的問題、停止或續跑 Ticket（它不會替你啟動 orchestrator）。")
    default_enabled = True
    fields = [{"key": "home", "label": "superagent home 資料夾", "default": DEFAULT_HOME,
               "hint": "state.sqlite、inbox、outbox 所在的資料夾"}]

    def __init__(self, src=SRC, runner=run_cli):
        self.src, self.runner = src, runner

    def installed(self):
        return os.path.isdir(os.path.join(self.src, "superagent"))

    def _cli(self, home, *args, wait=None, timeout=45):
        pre = ["-m", "superagent", f"--home={home}"] + ([f"--wait={wait}"] if wait else [])
        return self.runner(pre + list(args), self.src, self.src, timeout)

    def panel(self, values):
        home = values.get("home", "").strip()
        if not self.installed():
            return {"title": "superagent", "status": "未安裝", "blocks": [
                {"type": "text", "text": f"找不到 {os.path.join(self.src, 'superagent')}。這一層是可拔除的，沒裝不影響其他功能。"}]}
        if not home:
            return {"title": "superagent", "status": "尚未設定", "blocks": [
                {"type": "text", "text": "請先在設定頁填 superagent home 資料夾。"}]}
        rc, out, err = self._cli(home, "status", timeout=30)
        if rc == 1 and "no state yet" in (out + err):
            return {"title": "superagent", "status": "還沒有資料", "blocks": [
                {"type": "text", "text": f"{home} 裡沒有 state.sqlite：orchestrator 還沒跑過。啟動方式見 docs/superagent.md（python -m superagent run …）。"}]}
        try:
            if rc != 0:
                raise ValueError((err or out).strip() or f"exit {rc}")
            st = json.loads(out)
        except ValueError as e:
            return {"title": "superagent", "status": "讀不到狀態", "blocks": [{"type": "pre", "text": str(e)}]}
        blocks = []
        if not st.get("running"):
            blocks.append({"type": "text", "text": "orchestrator 目前沒在跑：下面的按鈕要等它在跑才會生效（沒人接指令時約 8 秒後回報無回應）。"})
        asks = st.get("open_asks") or []
        blocks.append({"type": "text", "text": f"等你回答的問題：{len(asks)}"})
        blocks.append({"type": "list", "items": [
            {"text": f"#{a['ask_id']}（Ticket {a['ticket_id']}）{a['what']}",
             "buttons": [{"label": "允許一次", "action": "answer", "args": {"ask_id": a["ask_id"], "yes": True}},
                         {"label": "拒絕", "action": "answer", "args": {"ask_id": a["ask_id"], "yes": False}}]}
            for a in asks]})
        tickets = st.get("tickets") or []
        blocks.append({"type": "text", "text": f"Ticket：{len(tickets)}"})
        blocks.append({"type": "list", "items": [
            {"text": f"#{t['id']} {t['state']} · {t['thread']}" + (f" · {t['note']}" if t.get("note") else ""),
             "buttons": [{"label": "停止", "action": "stop", "args": {"ticket": t["id"]},
                          "confirm": f"永久停止 Ticket {t['id']}？"},
                         {"label": "續跑", "action": "resume", "args": {"ticket": t["id"]}}]}
            for t in tickets]})
        counts = lambda d: "、".join(f"{k} {v}" for k, v in sorted((d or {}).items())) or "（無）"
        blocks.append({"type": "text", "text": f"outbox：{counts(st.get('outbox'))}"
                       + (f"　模型審核：{counts(st.get('eval'))}" if "eval" in st else "")})
        return {"title": "superagent", "status": "orchestrator 執行中" if st.get("running") else "orchestrator 未執行",
                "blocks": blocks}

    def act(self, values, action, args):
        home = values.get("home", "").strip()
        if not home or not self.installed():
            raise ValueError("superagent 未安裝或沒設定 home")
        if action == "answer":
            ask_id, yes = args.get("ask_id"), args.get("yes")
            if not isinstance(ask_id, int) or isinstance(ask_id, bool) or not isinstance(yes, bool):
                raise ValueError("answer needs an integer ask_id and a boolean yes")
            cmd = ["answer", str(ask_id), "y" if yes else "n"]        # only y or n: never the permanent 'a'
        elif action in ("stop", "resume"):
            ticket = args.get("ticket")
            if not isinstance(ticket, int) or isinstance(ticket, bool):
                raise ValueError(f"{action} needs an integer ticket")
            cmd = [action, str(ticket)]
        else:
            raise ValueError(f"unknown action {action!r}")
        rc, out, err = self._cli(home, *cmd, wait=8, timeout=30)
        text = (out or err).strip() or ("沒有回應" if rc is None else f"exit {rc}")
        return {"ok": rc == 0, "message": text, "refresh": True}
