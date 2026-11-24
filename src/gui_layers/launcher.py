#!/usr/bin/env python3
"""The web GUI with the optional layers (superagent, kb) available in its settings page.

    python src/gui_layers/launcher.py [--port 8791] [--no-browser]

Same as `python src/harness/harness_gui.py`, plus the two layers registered through the GUI's
generic hook. Whether a layer is on is decided in the settings page (superagent starts on, kb off).
"""
import os
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [SRC, os.path.join(SRC, "harness")]

import harness_gui                                   # noqa: E402
from gui_layers.kb_panel import KbLayer              # noqa: E402
from gui_layers.superagent_panel import SuperagentLayer   # noqa: E402


def main():
    harness_gui.register_extension(SuperagentLayer())
    harness_gui.register_extension(KbLayer())
    harness_gui.main()


if __name__ == "__main__":
    main()
