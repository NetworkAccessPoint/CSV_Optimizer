#!/usr/bin/env python3
"""Run the worker-pool tests with a __main__ that is safe to re-import.

``multiprocessing``'s spawn start method imports the program's __main__ module
in every worker.  Running those tests through ``python -m unittest`` therefore
re-enters the unittest runner inside each worker, which deadlocks on some
platforms.  This script is an ordinary module with the usual guard, so the
workers import something harmless.

    python tools/run_pool_tests.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    os.environ["CSVOPT_TEST_POOL"] = "1"
    from tests import test_parallel

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_parallel)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
