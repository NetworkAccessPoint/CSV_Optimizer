"""Tiny background-job registry so slow scans can report progress and be cancelled."""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from typing import Any, Callable, Optional


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind: str, label: str = ""):
        self.id = f"job{next(self._ids)}"
        self.kind = kind
        self.label = label
        self.status = "running"  # running | done | error | cancelled
        self.done = 0
        self.total = 0
        self.result: Any = None
        self.error: str = ""
        self.started = time.time()
        self.finished: Optional[float] = None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def progress(self, done: int, total: int) -> bool:
        """Progress callback handed to the scanning code; False aborts it."""
        self.done, self.total = done, total
        return not self._cancel.is_set()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "ratio": (self.done / self.total) if self.total else 0.0,
            "elapsed": (self.finished or time.time()) - self.started,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, keep: int = 40):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.keep = keep
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Job], Any], label: str = "") -> Job:
        job = Job(kind, label)
        with self._lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > self.keep:
                self.jobs.pop(self.order.pop(0), None)

        def run() -> None:
            try:
                job.result = fn(job)
                job.status = "cancelled" if job.cancelled else "done"
            except Exception as exc:  # surfaced to the UI, not swallowed
                job.status = "cancelled" if job.cancelled else "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.traceback = traceback.format_exc()
            finally:
                job.finished = time.time()

        threading.Thread(target=run, name=f"csvopt-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)
