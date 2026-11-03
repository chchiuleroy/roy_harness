"""Slice 3, S1: the reply of a turn, read from a harness process' events. Pure functions.

Each test's docstring says WHY the behaviour matters.
"""
import random
import unittest

import superagent_support as sup  # noqa: F401  (puts src/ on the path)
from superagent.turnreply import REPLY_CAP, ReplyTracker, TurnEnd, clean_reply, reply_payload


def run(events):
    t, out = ReplyTracker(), []
    for ev in events:
        r = t.feed(ev if isinstance(ev, dict) else {"kind": ev})
        if r is not None:
            out.append(r.text)
    return out


def say(text):
    return {"kind": "assistant", "text": text}


class TestReplyTracker(unittest.TestCase):
    def test_the_reply_is_the_text_after_the_last_tool_event(self):
        """'Let me check...' before a tool call is thinking aloud; the answer comes after the tools."""
        self.assertEqual(run(["turn_start", say("let me look"), "tool_call", "tool_result", say("done: 3 files"),
                              "turn_end"]), ["done: 3 files"])

    def test_text_followed_by_a_tool_call_is_void(self):
        """A turn that ends right after a tool result has no answer; reporting the earlier text
        would pass off an intention as a result."""
        self.assertEqual(run(["turn_start", say("I will run it"), "tool_call", "tool_result", "turn_end"]), [None])

    def test_the_last_text_wins_and_nothing_leaks_into_the_next_turn(self):
        self.assertEqual(run(["turn_start", say("a"), say("b"), "turn_end",
                              "turn_start", "turn_end"]), ["b", None])

    def test_a_turn_with_no_text_at_all(self):
        """A model call that failed ends the turn with no assistant event."""
        self.assertEqual(run(["turn_start", "turn_end"]), [None])

    def test_events_that_end_nothing_return_none(self):
        t = ReplyTracker()
        for kind in ("turn_start", "assistant", "tool_call", "tool_result", "ask", "log", "stderr", "ready"):
            self.assertIsNone(t.feed({"kind": kind, "text": "x"}))

    def test_an_empty_assistant_text_is_text_not_nothing(self):
        """The model said '' : that is an empty reply, and the caller decides what that means."""
        self.assertEqual(run([say(""), "turn_end"]), [""])

    def test_reset_forgets_the_text(self):
        t = ReplyTracker()
        t.feed(say("old"))
        t.reset()
        self.assertEqual(t.feed({"kind": "turn_end"}), TurnEnd(None))

    def test_same_answers_as_the_rule_the_model_session_used_before(self):
        """The rule was moved out of MainSession.poll so that ticket executors can share it. A
        change of behaviour in a refactor would be silent, so the old rule is written out here as
        the reference and the two are compared on many random event streams."""
        def old(events):
            reply, out = None, []
            for ev in events:
                kind = ev["kind"]
                if kind == "turn_start":
                    reply = None
                elif kind == "assistant":
                    reply = str(ev.get("text", ""))
                elif kind in ("tool_call", "tool_result"):
                    reply = None
                elif kind == "turn_end":
                    out.append(reply)
                    reply = None
            return out

        rng = random.Random(7)
        kinds = ["turn_start", "assistant", "tool_call", "tool_result", "turn_end", "log", "ask", "stderr"]
        for _ in range(2000):
            events = [{"kind": rng.choice(kinds), "text": rng.choice(["", "a", "bb", "ccc"])}
                      for _ in range(rng.randint(0, 14))]
            self.assertEqual(run(events), old(events), events)


class TestReplyPayload(unittest.TestCase):
    def test_text_becomes_untrusted_data_in_one_field(self):
        """The consumer is told in the message itself that the words are not to be obeyed."""
        p = reply_payload(3, 17, "all done")
        self.assertEqual(p, {"kind": "executor_output", "untrusted": True, "ticket_id": 3, "input_id": 17,
                             "text": "all done", "truncated_chars": 0})

    def test_no_text_is_still_a_message_and_says_so(self):
        """A failed model call is not silence. None, empty and whitespace-only all count as no text."""
        for text in (None, "", "   \n\t "):
            with self.subTest(text=text):
                p = reply_payload(3, 17, text)
                self.assertEqual((p["kind"], p["ticket_id"], p["input_id"]), ("status", 3, 17))
                self.assertIn("without a reply", p["text"])
                self.assertNotIn("untrusted", p)

    def test_the_text_is_cleaned_and_capped_before_it_leaves(self):
        p = reply_payload(1, 1, "a\x1b" + "b" * (REPLY_CAP + 3))
        self.assertEqual((len(p["text"]), p["truncated_chars"], "\x1b" in p["text"]), (REPLY_CAP, 4, False))


class TestCleanReply(unittest.TestCase):
    def test_control_characters_are_removed_but_lines_and_tabs_stay(self):
        """A reply is data for a reader. Escape sequences and NULs must not reach a terminal or a
        parser; newlines and tabs are the text's own layout."""
        self.assertEqual(clean_reply("a\x1b[31mred\x00\x07 b\r\nc\td\x7f"), ("a[31mred b\nc\td", 0))

    def test_c1_controls_and_text_direction_tricks_are_removed(self):
        """Found by review: only the ASCII controls were taken out. U+009B is a one-character CSI
        (a terminal reads it as the start of an escape sequence) and U+202E reverses what follows,
        so a reply could rewrite what a reader sees. All of these are removed."""
        tricks = {"C1 CSI": "\u009b", "C1 NEL": "\u0085", "right-to-left override": "‮",
                  "left-to-right embedding": "‪", "isolate": "⁦", "pop isolate": "⁩",
                  "right-to-left mark": "‏", "zero-width space": "​", "BOM": "﻿",
                  "soft hyphen": "­", "word joiner": "⁠", "line separator": " ",
                  "paragraph separator": " "}
        for name, ch in tricks.items():
            with self.subTest(name):
                self.assertEqual(clean_reply(f"a{ch}b"), ("ab", 0))

    def test_a_lone_surrogate_is_removed_because_it_cannot_be_written(self):
        self.assertEqual(clean_reply("x\ud800y\udfffz"), ("xyz", 0))

    def test_real_text_is_left_alone(self):
        """Over-cleaning would be its own harm: Chinese, accents built from combining marks, emoji
        that use the zero-width joiner, a tab, and line breaks must come through."""
        text = "匯出失敗 café (é) \U0001F468‍\U0001F469‍\U0001F467 مرحبا\n\tline 2"
        self.assertEqual(clean_reply(text), (text, 0))

    def test_text_is_cut_to_the_cap_and_the_loss_is_counted(self):
        text, dropped = clean_reply("x" * (REPLY_CAP + 25))
        self.assertEqual((len(text), dropped), (REPLY_CAP, 25))

    def test_exactly_at_the_cap_nothing_is_dropped(self):
        self.assertEqual(clean_reply("y" * REPLY_CAP), ("y" * REPLY_CAP, 0))

    def test_the_cap_counts_characters_not_bytes(self):
        text, dropped = clean_reply("匯" * (REPLY_CAP + 1))
        self.assertEqual((len(text), dropped), (REPLY_CAP, 1))

    def test_dropped_counts_only_what_the_cap_removed(self):
        """Removed control characters are not 'truncated'; only the cut is."""
        text, dropped = clean_reply("\x00" * 50 + "z" * 10, cap=4)
        self.assertEqual((text, dropped), ("zzzz", 6))


if __name__ == "__main__":
    unittest.main()
