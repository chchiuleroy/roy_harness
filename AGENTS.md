# Notes for coding agents

Before you say anything in this repository is "verified", read [docs/VERIFY.md](docs/VERIFY.md) and follow it. Short version:

- The check is `python verify.py` (harness suite, then kb suite, both always). `--only` is for iteration; if you ran only one stage, say which.
- Do not delete, skip, loosen or hard-code a test to make it pass. Do not add `sleep` or retries to a flaky test; find the cause or report it.
- Three failures of the same check in a row: stop, keep the output, report. Only new evidence that the cause changed restarts the count.
- Do not lower a completion condition quietly. If it must change, say what changed and why.
- If one change edits code and also the thing that checks it (`verify.py` stages or `LIVE_SWITCHES`, `.github/workflows/ci.yml`, branch protection), say so in the pull request.
- Label every result as measured (output exists), inferred, or guessed.
