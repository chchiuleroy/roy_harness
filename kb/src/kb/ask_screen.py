"""Screen a passage for text that tries to give orders to a model (stage S4, defence step 2).

A heuristic, on purpose: it catches clumsy and common attacks, not careful ones, and the layer says so wherever it reports a screening. It never
returns the text that matched (a reason is a fixed label), so a flagged passage cannot smuggle itself into a log or a message through its reason.
"""
import re

from . import ask_sanitize as sanitize

# (label, pattern). Labels are fixed strings.
# The text is whatever a source document says, so no pattern may rescan the same stretch from many starting points (that made a run of
# blank lines, a long "rm -rrrr..." flag or many "curl" words take seconds). Three patterns were rewritten to the same set of matches:
#   "^\s*#" -> "^[^\S\n]*#"   (a line start inside the blank run matches just as well, so only the line's own blanks are read)
#   "curl[^\n|]*|" -> the stretch may not contain another curl/wget (the last one before the pipe is enough)
#   "-[a-z]*r[a-z]*f" -> "-[a-qs-z]*r[a-z]*f"   (the first r is the one that counts)
_PATTERNS = [
    ("tells the reader to ignore earlier instructions",
     r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|preceding|all)\b[^.\n]{0,30}\b(instructions?|prompts?|rules?|messages?)\b"),
    ("addresses the model directly with new orders", r"\b(you must now|from now on,? you|you are now|new instructions?:|your new task)\b"),
    ("names a prompt role or chat template marker", r"(<\|im_(start|end)\|>|\[/?INST\]|<\|(system|user|assistant)\|>|^[^\S\n]*#{1,3}\s*(system|instructions?)\b)"),
    ("mentions the system or developer prompt", r"\b(system|developer) prompt\b"),
    ("names a harness tool", r"\b(run_command|write_file|edit_file|read_file|view_image|ask_user|schedule_wakeup|monitor)\b"),
    ("contains a shell command that downloads and runs code", r"\b(curl|wget)\b(?:(?!\b(?:curl|wget)\b)[^\n|])*\|\s*(ba|z)?sh\b"),
    ("contains a destructive shell command", r"\brm\s+-[a-qs-z]*r[a-z]*f\b|\bpowershell(\.exe)?\s+-(enc|encodedcommand)\b|\bformat\s+[a-z]:"),
    ("tells the reader to ignore earlier instructions (Chinese)", r"(忽略|無視|无视|忘記|忘记)(以上|上面|先前|之前|前面|所有)[^。\n]{0,12}(指示|指令|提示|規則|规则|訊息|消息)"),
    ("addresses the model directly with new orders (Chinese)", r"(你現在|你现在|從現在起|从现在起|新的指令|你必須立刻|你必须立刻)"),
    ("mentions the system prompt (Chinese)", r"(系統提示|系统提示|開發者提示|开发者提示)"),
]
_COMPILED = [(label, re.compile(p, re.IGNORECASE | re.MULTILINE)) for label, p in _PATTERNS]
INVISIBLE_LIMIT = 3          # more unsafe characters than this in one passage is itself a reason


def screen(text):
    """-> list of fixed reason labels; empty when nothing was found."""
    text = str(text)
    reasons = [label for label, pattern in _COMPILED if pattern.search(text)]
    if sanitize.count_unsafe(text) > INVISIBLE_LIMIT:
        reasons.append("contains control or invisible characters that change how text is shown")
    return reasons
