"""Entry point for frozen (PyInstaller) builds.

Kept separate from ``csvopt/__main__.py`` because a frozen entry script runs as
a top-level module, where relative imports do not work, and because
``freeze_support()`` has to be the first thing a re-spawned worker process runs.
"""

import multiprocessing

from csvopt.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
