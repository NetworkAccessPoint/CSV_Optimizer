"""Filtering across several processes.

Each worker opens the same file and the same memory-mapped index and scans a
distinct range of rows, so a multi-gigabyte log is filtered at the speed of all
the cores rather than one.  The pool is only used where it is safe and worth it:
the file must be unedited (workers read the file, not the in-memory overlay) and
big enough that process start-up disappears next to the scan.

``spawn`` is used everywhere so the behaviour is identical on Windows, macOS and
Linux; that also means the worker payload has to be picklable, which is why the
workers rebuild a :class:`~csvopt.table.Table` from the file path instead of
receiving one.
"""

from __future__ import annotations

import multiprocessing
import os
from array import array
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, as_completed
from typing import Callable, Optional, Sequence

from .index import Aborted
from .ops import Condition, _pick_literal, _prefilter_literals, compile_conditions, scan_range
from .table import Table

# Below this the single process is already fast and the pool would just add
# start-up latency.
MIN_PARALLEL_ROWS = 2_000_000
MIN_PARALLEL_BYTES = 256 << 20
SEGMENTS_PER_WORKER = 4
MIN_SEGMENT_ROWS = 100_000


# Set once a pool has proved impossible here (a sandbox, a frozen build without
# spawn support, an interpreter started from stdin); afterwards filtering simply
# stays in this process instead of trying again on every query.
_POOL_UNAVAILABLE = False


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # respects container limits
    except AttributeError:
        return os.cpu_count() or 1


def plan_workers(table: Table, requested: Optional[int]) -> int:
    """How many processes to use, 1 meaning "stay in this process"."""
    if _POOL_UNAVAILABLE or (requested is not None and requested <= 1):
        return 1
    if not table.can_stream_raw():
        return 1  # workers cannot see unsaved edits
    if not table.index.mapped:
        return 1  # without a shared index file each worker would rescan
    if table.row_count < MIN_PARALLEL_ROWS or table.index.size < MIN_PARALLEL_BYTES:
        return 1
    available = max(1, cpu_count() - 1)
    return max(1, min(requested or available, available, 8))


def _segments(rows: int, workers: int, min_rows: int = MIN_SEGMENT_ROWS) -> list[tuple[int, int]]:
    """Split the row range into work units, several per worker for balance."""
    target = max(min_rows, rows // (workers * SEGMENTS_PER_WORKER) + 1)
    return [(start, min(start + target, rows)) for start in range(0, rows, target)]


def _worker(payload) -> tuple[int, int, bytes]:
    """Scan one row range; returns (segment index, rows scanned, packed ids)."""
    (index, path, use_cache, conditions, start, stop) = payload
    table = _worker_table(path, use_cache)
    compiled = compile_conditions(table, conditions)
    # Probe this worker's own slice of the file, not the head of it.
    chosen = _pick_literal(table, _prefilter_literals(table, compiled), start_rid=start)
    found = scan_range(table, compiled, chosen, start_rid=start, stop_rid=stop)
    return index, stop - start, found.tobytes()


_CACHED: dict[str, Table] = {}


def _worker_table(path: str, use_cache: bool) -> Table:
    """One Table per worker process, reused across the segments it handles."""
    table = _CACHED.get(path)
    if table is None:
        table = Table(path, use_cache=use_cache)
        _CACHED[path] = table
    return table


def _terminate(executor: ProcessPoolExecutor) -> None:
    """Stop the pool now: cancelling futures leaves running scans alive."""
    executor.shutdown(wait=False, cancel_futures=True)
    try:
        for process in list(getattr(executor, "_processes", {}).values()):
            process.terminate()
    except Exception:
        pass


def filter_parallel(
    table: Table,
    conditions: Sequence[Condition],
    workers: int,
    match_all: bool = True,
    limit: Optional[int] = None,
    min_segment_rows: int = MIN_SEGMENT_ROWS,
    progress: Optional[Callable[[int, int], bool]] = None,
) -> Optional[array]:
    """Filter with a process pool, or return None if the pool cannot be used."""
    if not match_all:
        return None  # OR conditions are cheap enough to scan in one process
    rows = table.row_count
    segments = _segments(rows, workers, min_segment_rows)
    if len(segments) < 2:
        return None
    # The index cache is what the workers map, which plan_workers already
    # verified is on disk.
    payloads = [
        (i, table.path, True, list(conditions), start, stop)
        for i, (start, stop) in enumerate(segments)
    ]
    global _POOL_UNAVAILABLE
    results: dict[int, bytes] = {}
    done = 0
    context = multiprocessing.get_context("spawn")
    try:
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=context)
    except (OSError, ValueError, RuntimeError, ImportError):
        _POOL_UNAVAILABLE = True  # no processes here; the caller scans inline
        return None
    try:
        futures = [executor.submit(_worker, payload) for payload in payloads]
        for future in as_completed(futures):
            # Errors raised inside a worker are real errors and propagate;
            # only a broken pool means "this machine cannot do it".
            index, scanned, packed = future.result()
            results[index] = packed
            done += scanned
            if progress is not None and not progress(done, rows):
                _terminate(executor)
                raise Aborted()
    except BrokenExecutor:
        _POOL_UNAVAILABLE = True
        _terminate(executor)
        return None
    finally:
        executor.shutdown(wait=True)

    out = array("q")
    for index in range(len(segments)):
        packed = results.get(index)
        if packed:
            chunk = array("q")
            chunk.frombytes(packed)
            out.extend(chunk)
            if limit and len(out) >= limit:
                del out[limit:]
                break
    return out
