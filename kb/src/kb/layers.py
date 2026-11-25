"""Which tier every kb module belongs to (Gate 2 section 2.1). tests/test_layers.py enforces it with a syntax-tree scan:
a module may import only modules of its own tier or a lower one, T1 and T2 may not import each other, nothing imports APP,
and a module missing from this table fails the test (so a new file must declare its tier before it can be merged).

  T0   core: ids, db, cas, profile, audit, doctor ... (standard library + SQLite only)
  T1   dense retrieval (needs numpy and an embedding backend): embedder, vecindex, dense
  T2   modality converters (each optional): conv_pdf (needs PyMuPDF)
  T3   the question-and-answer layer (stage S4): ask_*, which only STARTS the harness as a child process and imports nothing of it
  EVAL evaluation bridge
  APP  command-line entry points (may import everything, nothing imports it)
"""

LEVEL = {"T0": 0, "T1": 1, "T2": 1, "T3": 2, "EVAL": 3, "APP": 4}

MODULES = {
    "__init__": "T0", "__main__": "APP", "errors": "T0", "canonical": "T0", "ids": "T0", "keys": "T0", "faults": "T0", "schema": "T0",
    "db": "T0", "writer": "T0", "home": "T0", "cas": "T0", "audit": "T0", "profile": "T0", "colls": "T0", "layers": "T0",
    "doctor": "T0", "durable": "T0", "cli": "APP",
    "tokenize": "T0", "conv_base": "T0", "conv_text": "T0", "conv_markdown": "T0", "conv_html": "T0", "conv_csv": "T0",
    "conv_registry": "T0", "pathsafe": "T0", "fetch": "T0", "inbox": "T0",
    "pipeline": "T0", "chunker": "T0", "display": "T0", "ftsindex": "T0", "commit": "T0",
    "relations": "T0", "search": "T0", "inventory": "T0",
    "review": "T0", "patches": "T0", "evidence": "T0", "ai_edit": "T0", "upgrade": "T0",
    "conv_pdf": "T2", "embedder": "T1", "vecindex": "T1", "dense": "T1", "opener": "T0",
    "ask_sanitize": "T3", "ask_frame": "T3", "ask_screen": "T3", "ask_verify": "T3", "ask_retrieve": "T3", "ask_outbox": "T3", "ask_hook": "T3",
    "ask_runner": "T3", "ask": "T3",
}


def import_allowed(src_tier, dst_tier):
    if src_tier == dst_tier:
        return True
    return LEVEL[dst_tier] < LEVEL[src_tier]      # T1 and T2 share a level, so siblings never import each other
