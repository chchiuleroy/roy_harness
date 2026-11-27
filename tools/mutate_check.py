"""Mutation check for the harness code (standard library only).

Break the code on purpose, in a throwaway copy, and require the named test to go red. A mutant that survives
means that test does not guard the behaviour it claims to guard.

    python tools/mutate_check.py --list            show the mutants
    python tools/mutate_check.py                   run all of them (4 at a time)
    python tools/mutate_check.py NAME [NAME ...]   run only these

Each mutant is (name, file under the repository root, old text, new text, test id). The old text must occur exactly
once in the file. "killed" means the test failed an assertion; a mutant that makes it crash for another reason is
"errored" and counts as not killed. The test id is what `python -m unittest` takes, run from the copy's `tests/` folder. Before any
mutant runs, the same test is run on the unmutated copy and must pass: otherwise "red" would say nothing.

kb has its own tool (kb/tools/mutate.py) that knows kb's layout; this one is for src/harness and src/superagent.
It is slow (a few seconds per mutant) and is not part of verify.py. Exit code 0 only if every mutant was killed.
"""
import concurrent.futures as cf
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COPY_DIRS = ("src", "tests", "bench")
TIMEOUT_S = 300

PROV = "test_harness_provenance.TestHostsIn.test_new_patterns_miss_no_host_the_old_ones_found"
ORIGIN = "src/harness/harness_origin.py"
MUTANTS = [
    ("origin_url_scheme_must_end_in_a_letter", ORIGIN, "(?<=[a-z0-9+.-])://", "(?<=[a-z])://", PROV),
    ("origin_url_host_loses_digits", ORIGIN, r"(\[[0-9a-f:.]+\]|[\w.-]+)", r"(\[[0-9a-f:.]+\]|[a-z.-]+)", PROV),
    ("origin_at_host_needs_a_word_char_before_the_at", ORIGIN, r"(?<=[\w.+-])@", r"(?<=[\w])@", PROV),
    ("origin_at_host_needs_two_dots", ORIGIN, r'(?:\.[a-z0-9-]+)+)", re.I)    # mail', r'(?:\.[a-z0-9-]+){2,})", re.I)    # mail', PROV),
    ("origin_at_host_label_loses_digits", ORIGIN, r"@([a-z0-9-]+(?:", r"@([a-z0-]+(?:", PROV),
    ("origin_keeps_the_trailing_dot", ORIGIN, '.lower().strip(".")', ".lower()", PROV),
    ("origin_drops_every_host", ORIGIN, "if h and any(c.isalnum() for c in h)", "if h and False", PROV),
]

LAB = "bench/labeling/labeling.py"
LT = "test_labeling_pipeline."
MUTANTS += [
    ("lab_blind_leaks_the_repo_to_annotators", LAB, 'lines.append({"id": cid, "text": row["text"]})',
     'lines.append({"id": cid, "text": row["text"], "repo": row["repo"]})', LT + "TestBlind.test_annotators_see_an_opaque_id_and_the_text_only"),
    ("lab_blind_ignores_id_start", LAB, "start=id_start + k + 1", "start=k + 1",
     LT + "TestBlind.test_a_second_pool_continues_the_numbering_and_the_chunk_files"),
    ("lab_blind_ignores_first_chunk", LAB, 'f"chunk{first_chunk + k // chunk}.jsonl"', 'f"chunk{1 + k // chunk}.jsonl"',
     LT + "TestBlind.test_a_second_pool_continues_the_numbering_and_the_chunk_files"),
    ("lab_merge_ignores_uncertain", LAB, 'if a["label"] == b["label"] and not a["uncertain"] and not b["uncertain"]:',
     'if a["label"] == b["label"]:', LT + "TestMerge.test_a_row_either_annotator_doubted_is_never_scored"),
    ("lab_merge_checks_only_one_uncertain", LAB, ' and not a["uncertain"] and not b["uncertain"]:', ' and not a["uncertain"]:',
     LT + "TestMerge.test_a_row_either_annotator_doubted_is_never_scored"),
    ("lab_merge_kappa_denominator", LAB, "(po - pe) / (1 - pe) if pe < 1", "(po - pe) / (1 + pe) if pe < 1",
     LT + "TestMerge.test_agreement_numbers_match_a_hand_computation"),
    ("lab_merge_accepts_any_id_order", LAB, "if got != want:", "if sorted(got) != sorted(want):",
     LT + "TestMerge.test_an_annotator_file_that_is_wrong_in_any_way_is_refused"),
    ("lab_merge_accepts_any_label", LAB, 'if x["label"] not in allowed or not isinstance(x.get("uncertain"), bool):',
     'if not isinstance(x.get("uncertain"), bool):', LT + "TestMerge.test_an_annotator_file_that_is_wrong_in_any_way_is_refused"),
    ("lab_merge_pool_stratum_overrides", LAB, 'd.setdefault("stratum", stratum)', 'd["stratum"] = stratum',
     LT + "TestMerge.test_pool_stratum_is_the_default_but_a_rows_own_stratum_wins"),
    ("lab_fetch_keeps_pull_requests", LAB, 'if it.get("pull_request") is not None or (it.get("user") or {}).get("type") == "Bot":',
     'if (it.get("user") or {}).get("type") == "Bot":', LT + "TestFetch.test_only_real_human_issues_with_a_body_after_the_start_date_are_drawn"),
    ("lab_fetch_keeps_bots", LAB, 'if it.get("pull_request") is not None or (it.get("user") or {}).get("type") == "Bot":',
     'if it.get("pull_request") is not None:', LT + "TestFetch.test_only_real_human_issues_with_a_body_after_the_start_date_are_drawn"),
    ("lab_fetch_keeps_short_bodies", LAB, "if len(body) < 80 or rid in exclude:", "if rid in exclude:",
     LT + "TestFetch.test_only_real_human_issues_with_a_body_after_the_start_date_are_drawn"),
    ("lab_fetch_ignores_the_exclusion_list", LAB, "if len(body) < 80 or rid in exclude:", "if len(body) < 80:",
     LT + "TestFetch.test_ids_from_an_earlier_trial_are_left_out"),
    ("lab_fetch_ignores_the_start_date", LAB, 'if it["created_at"] < since:', "if False:",
     LT + "TestFetch.test_only_real_human_issues_with_a_body_after_the_start_date_are_drawn"),
    ("lab_fetch_ignores_the_cap", LAB, '(item["title"].strip() + "\\n\\n" + body)[:cap]', '(item["title"].strip() + "\\n\\n" + body)',
     LT + "TestFetch.test_text_is_title_blank_line_body_cut_to_the_cap"),
    ("lab_fetch_ignores_the_quota", LAB, "rows.extend(pool[:want])\n    rng.shuffle(rows)\n    return rows\n\n\ndef fetch_usage",
     "rows.extend(pool)\n    rng.shuffle(rows)\n    return rows\n\n\ndef fetch_usage", LT + "TestFetch.test_same_seed_same_draw_and_the_quota_is_respected"),
    ("lab_fetch_never_shuffles_the_pool", LAB, 'rng.shuffle(pool)\n        print(repo, "pool", len(pool), "take", min(want, len(pool)))\n        rows.extend(pool[:want])',
     'print(repo, "pool", len(pool), "take", min(want, len(pool)))\n        rows.extend(pool[:want])', LT + "TestFetch.test_same_seed_same_draw_and_the_quota_is_respected"),
    ("lab_blind_overwrites_silently", LAB, "    if not force:", "    if False:",
     LT + "TestBlind.test_a_file_that_already_exists_with_other_content_is_not_overwritten"),
    ("lab_merge_lets_a_later_mapping_replace_an_earlier_one", LAB, "        if clash:", "        if False:",
     LT + "TestMerge.test_two_batches_that_reuse_blind_ids_are_refused"),
    ("lab_merge_lets_a_blind_id_sit_in_two_chunks", LAB, 'if x["id"] in store:', "if False:",
     LT + "TestMerge.test_a_blind_id_in_two_chunks_is_refused"),
    ("lab_merge_ignores_text_changed_after_blinding", LAB, 'if seen_text[i] != cands[mapping[i]]["text"]:', "if False:",
     LT + "TestMerge.test_text_that_changed_after_blinding_is_refused"),
    ("lab_merge_lets_two_blind_ids_name_one_issue", LAB, "if len(set(mapping.values())) != len(mapping):", "if False:",
     LT + "TestMerge.test_a_mapping_that_names_no_known_issue_or_one_issue_twice_is_refused"),
    ("lab_usage_ignores_seen_ids", LAB, 'if it["user"] == "Bot" or len(body) < 80 or rid in seen:', 'if it["user"] == "Bot" or len(body) < 80:',
     LT + "TestFetch.test_usage_draw_marks_its_strata_skips_seen_ids_and_carries_old_rows"),
    ("lab_usage_carries_duplicates", LAB, 'if o["id"] in seen:', "if False:",
     LT + "TestFetch.test_usage_draw_marks_its_strata_skips_seen_ids_and_carries_old_rows"),
]


def prepare(root, work, copy_dirs=COPY_DIRS):
    for d in copy_dirs:
        shutil.copytree(os.path.join(root, d), os.path.join(work, d), ignore=shutil.ignore_patterns("__pycache__"))


def run_test(work, test_id, timeout=TIMEOUT_S):
    env = {k: v for k, v in os.environ.items() if not k.endswith("_LIVE") and k != "SUPERAGENT_REMOVAL"}
    p = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", test_id], cwd=os.path.join(work, "tests"),
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-3000:]


def check_mutant(mutant, root=ROOT, copy_dirs=COPY_DIRS, timeout=TIMEOUT_S):
    """-> (name, verdict, detail). verdict: killed | SURVIVED | invalid | baseline-failed | errored"""
    name, rel, old, new, test_id = mutant
    work = tempfile.mkdtemp(prefix="harness_mut_")
    try:
        prepare(root, work, copy_dirs)
        path = os.path.join(work, rel)
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        if "\r\n" in text:        # a Windows checkout: a multi-line old/new text must use the file's own line ends
            old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        if text.count(old) != 1:
            return name, "invalid", f"old text occurs {text.count(old)} times in {rel}"
        if old == new:
            return name, "invalid", "old and new text are the same"
        try:
            code, tail = run_test(work, test_id, timeout)
        except subprocess.TimeoutExpired:
            return name, "baseline-failed", f"the unmutated test timed out after {timeout} s"
        if code != 0:
            return name, "baseline-failed", tail
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new))
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            return name, "invalid", f"the mutant does not compile: {e}"
        code, tail = run_test(work, test_id, timeout)
        if code == 0:
            return name, "SURVIVED", tail
        if "AssertionError" not in tail:      # red for another reason (import error, crash): not evidence the test guards the behaviour
            return name, "errored", tail
        return name, "killed", tail
    except subprocess.TimeoutExpired:
        return name, "killed", f"timed out after {timeout} s (counts as red: the change made the test hang)"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list" in argv:
        for m in MUTANTS:
            print(f"{m[0]}  ->  {m[4]}")
        return 0
    chosen = [m for m in MUTANTS if not argv or m[0] in argv]
    unknown = [a for a in argv if a not in {m[0] for m in MUTANTS}]
    if unknown or not chosen:
        print("unknown mutant name(s):", ", ".join(unknown) or "(none selected)")
        return 2
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(check_mutant, chosen))
    bad = 0
    for name, verdict, detail in results:
        print(f"{verdict:16} {name}")
        if verdict != "killed":
            bad += 1
            print("    " + detail.replace("\n", "\n    "))
    print(f"{len(results) - bad} of {len(results)} mutants killed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
