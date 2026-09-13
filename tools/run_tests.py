#!/usr/bin/env python3
"""Run the test suite with a hard timeout that prints where it got stuck.

``unittest`` has no timeout, so a test that blocks forever turns into a CI job
that hangs until the runner is killed, with nothing to show for it.  This
wrapper arms ``faulthandler``: if the suite outlives the deadline, every
thread's stack is dumped and the process exits non-zero.

    python tools/run_tests.py [--timeout SECONDS] [unittest args...]
"""

import argparse
import faulthandler
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--verbose", "-v", action="store_true")
    args, _rest = parser.parse_known_args(argv)

    faulthandler.enable()
    if args.timeout > 0:
        faulthandler.dump_traceback_later(args.timeout, exit=True)

    suite = unittest.defaultTestLoader.discover(
        start_dir=os.path.join(ROOT, "tests"), top_level_dir=ROOT
    )
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    faulthandler.cancel_dump_traceback_later()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
