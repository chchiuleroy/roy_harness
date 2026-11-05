"""Run another checkout's test suite and print {test id: outcome} as JSON.

Used by TestRemovable: the same runner is pointed at a copy of the working tree and at another
copy with superagent deleted, and the two dictionaries (the first one without the superagent
tests) must be equal. It records only
the test id and one word, never messages or timings, so nothing machine- or run-specific
(temp folders, process ids) can make two identical suites look different.

    python superagent_removal_runner.py <checkout root>
"""
import json
import os
import sys
import unittest


class Recorder(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.outcomes = {}

    def addSuccess(self, test):
        self.outcomes[test.id()] = "ok"

    def addFailure(self, test, err):
        self.outcomes[test.id()] = "FAIL"

    def addError(self, test, err):
        self.outcomes[test.id()] = "ERROR"

    def addSkip(self, test, reason):
        self.outcomes[test.id()] = "skipped"

    def addExpectedFailure(self, test, err):
        self.outcomes[test.id()] = "expected failure"

    def addUnexpectedSuccess(self, test):
        self.outcomes[test.id()] = "unexpected success"

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcomes[test.id()] = "FAIL"


def main(root):
    root = os.path.abspath(root)
    os.chdir(root)
    tests_dir = os.path.join(root, "tests")
    sys.path.insert(0, tests_dir)
    result = Recorder()
    unittest.defaultTestLoader.discover(tests_dir, top_level_dir=tests_dir).run(result)
    print(json.dumps(result.outcomes, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1])
