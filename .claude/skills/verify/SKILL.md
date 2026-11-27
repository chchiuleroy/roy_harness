---
name: verify
description: Verify a change in this repository before saying it works. Use when about to report a change as tested, verified or done.
---

Run `python verify.py` (harness suite, then kb suite; both always run). The rules for what counts as verified, what you may not change to get a green result, and what to do after three failures of the same check are in [docs/VERIFY.md](../../../docs/VERIFY.md); read it first. `--only harness` or `--only kb` is for iteration, not for the final report.
