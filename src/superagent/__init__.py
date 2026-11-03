"""superagent: an event-driven orchestration layer that can sit on top of roy_harness.

It talks to the harness only through a child process (`harness_vllm.py --jsonl`), a per-ticket
`.harness.hooks.json` and the environment it builds. Nothing here imports `src/harness/`, and
nothing in `src/harness/` imports this package, so deleting this folder leaves the harness
exactly as it was.
"""
