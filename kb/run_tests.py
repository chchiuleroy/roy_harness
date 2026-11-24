"""Run the kb test suite: python run_tests.py [unittest args]. Puts src/ on the path (and passes it to subprocesses)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
sys.path.insert(0, SRC)
os.environ["PYTHONPATH"] = SRC + os.pathsep + os.environ.get("PYTHONPATH", "")
os.environ.setdefault("PYTHONUTF8", "1")

if __name__ == "__main__":
    args = sys.argv[1:] or ["discover", "-s", os.path.join(HERE, "tests"), "-t", HERE]
    unittest.main(module=None, argv=["unittest"] + args)
