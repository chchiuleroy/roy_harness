"""The GUI's optional layers (superagent, kb) as panels, and the launcher that adds them.

Why this package exists and is not part of `src/harness/`: the harness (and so its GUI) must not know
that superagent or kb exist: no import of them, no command that runs them (tests/test_superagent_*
and kb/tests/test_layers.py scan `src/harness/` for exactly that). So `harness_gui.py` only offers a
generic hook (`register_extension`: a switch, a few settings, a panel described as data, a few
actions), and this package, which depends on the GUI and never the other way round, registers the
two layers through it. Delete this folder and the GUI is as before, with a settings page that
lists no layer.

Both layers are reached the way they already reach the harness and each other: as child processes
of their own command line (`python -m superagent ...`, `python -m kb ...`). Nothing is imported.
"""
