# S0 measurements (2026-11-16)

Single machine (Windows 11, 16 threads), single sessions. Raw numbers: `s0_ghost_cost.json`, `s0_o2_noise.json`, `s0_removal_compare.json`.

## Ghost check: verify everything at every commit (frozen)
Re-hashing the text and the float16 vector of every chunk of the real PDF corpus (31,980 chunks) takes **0.21 s** in total (6.5 microseconds per chunk;
5 repeats, data already in memory, disk reads not included). Extrapolated: about 6.5 s per million chunks. Decision frozen:
the content layer of the ghost check verifies **all** chunks at every commit; sampling is only considered if the cost per million chunks ever exceeds 30 s.

## O2 tolerance for startup time (frozen)
Method (`tools/measure_o2.py`): 40 alternating runs each of "kb not importable" and "kb importable but not attached", import time of the harness.
* median without kb 290.1 ms, with kb installed 299.8 ms, difference **+9.75 ms**.
* noise (scaled median absolute deviation of the without-kb runs) **51.9 ms**, i.e. about 18 percent of the median: the harness import is a noisy quantity on this machine.
* smallest N whose false-alarm rate (random half-splits of the without-kb runs) is at most 5 percent: **N = 1** (1.8 percent); tolerance = N x noise = **51.9 ms**. The measured difference is inside it.
Honest reading: the timing criterion can only detect a slowdown of roughly 50 ms or more. The exact, noise-free half of OPT2 (module set, subprocesses,
sockets, opened files, prompt and tool-schema hashes) is asserted by `tests/test_zero_cost.py` and does not depend on this number.
Limits: this is import time of the harness only, not a full session start; it was measured on a machine with the usual desktop load.

## Harness suite with and without kb
`tools/removal_compare.py` runs the harness's own full test suite twice and compares per-test outcomes: see `s0_removal_compare.json` (written when the run finishes).
