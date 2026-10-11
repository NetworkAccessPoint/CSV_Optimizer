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
from concurrent.futures import (
    BrokenExecutor,
    ProcessPoolExecutor,
    TimeoutError as FutureTimeout,
    as_completed,
)
from typing import Callable, Optional, Sequence

from . import rules as rulelib
from .index import Aborted
from .ops import Condition, _pick_literal, _prefilter_literals, compile_conditions, scan_range
from .table import Table

# Below this the single process is already fast and the pool would just add
# start-up latency.
MIN_PARALLEL_ROWS = 2_000_000
MIN_PARALLEL_BYTES = 256 << 20
SEGMENTS_PER_WORKER = 4
MIN_SEGMENT_ROWS = 100_000
# How long a brand new pool gets to answer a trivial task. Some hosts can start
# processes but never get them running usefully (a __main__ that re-runs itself,
# a locked-down sandbox); rather than hang, csvopt gives up and scans inline.
POOL_PROBE_TIMEOUT = 20.0


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


def _ping() -> str:
    """Trivial task used to check that a worker can actually run."""
    return "ok"


def _pool_ready(executor: ProcessPoolExecutor, timeout: float) -> bool:
    try:
        return executor.submit(_ping).result(timeout=timeout) == "ok"
    except (BrokenExecutor, FutureTimeout, OSError, ValueError, RuntimeError, EOFError):
        return False


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
    probe_timeout: float = POOL_PROBE_TIMEOUT,
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
    if not _pool_ready(executor, probe_timeout):
        _POOL_UNAVAILABLE = True
        _terminate(executor)
        executor.shutdown(wait=False)
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


def _rule_worker(payload) -> tuple[int, int, bytes, bytes]:
    """Match one row range against a rule table; returns packed ids + rules."""
    (index, path, use_cache, rows, mapping, labels, start, stop) = payload
    table = _worker_table(path, use_cache)
    ruleset = rulelib.build_ruleset(table, rows, mapping, labels)
    ids, rule_of, _scanned = rulelib.scan_rules_range(
        table, ruleset, start_rid=start, stop_rid=stop
    )
    return index, stop - start, ids.tobytes(), rule_of.tobytes()


def run_rules_parallel(
    table: Table,
    rows: list,
    mapping: list,
    labels: list,
    workers: int,
    limit: Optional[int] = None,
    min_segment_rows: int = MIN_SEGMENT_ROWS,
    probe_timeout: float = POOL_PROBE_TIMEOUT,
    progress: Optional[Callable[[int, int], bool]] = None,
):
    """Run a rule table across processes, or return None to scan inline.

    The rules travel as the pasted text plus the mapping rather than as a
    compiled :class:`~csvopt.rules.RuleSet`: the compiled form holds closures,
    which do not pickle, and rebuilding it in a worker costs microseconds.
    """
    total = table.row_count
    segments = _segments(total, workers, min_segment_rows)
    if len(segments) < 2:
        return None
    # Rule rows that carry no condition are dropped at compile time, so the
    # labels the result is reported under come from the compiled set, not from
    # the pasted rows.  Workers compile the same way and agree on the indices.
    compiled = [
        rule.label
        for rule in rulelib.build_ruleset(table, rows, mapping, labels).rules
    ]
    payloads = [
        (i, table.path, True, rows, mapping, labels, start, stop)
        for i, (start, stop) in enumerate(segments)
    ]
    global _POOL_UNAVAILABLE
    results: dict[int, tuple[bytes, bytes]] = {}
    done = 0
    context = multiprocessing.get_context("spawn")
    try:
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=context)
    except (OSError, ValueError, RuntimeError, ImportError):
        _POOL_UNAVAILABLE = True
        return None
    if not _pool_ready(executor, probe_timeout):
        _POOL_UNAVAILABLE = True
        _terminate(executor)
        executor.shutdown(wait=False)
        return None
    try:
        futures = [executor.submit(_rule_worker, payload) for payload in payloads]
        for future in as_completed(futures):
            index, scanned, packed_ids, packed_rules = future.result()
            results[index] = (packed_ids, packed_rules)
            done += scanned
            if progress is not None and not progress(done, total):
                _terminate(executor)
                raise Aborted()
    except BrokenExecutor:
        _POOL_UNAVAILABLE = True
        _terminate(executor)
        return None
    finally:
        executor.shutdown(wait=True)

    parts = []
    for index, (start, stop) in enumerate(segments):
        packed = results.get(index)
        if not packed:
            continue
        ids = array("q")
        rule_of = array("i")
        if packed[0]:
            ids.frombytes(packed[0])
        if packed[1]:
            rule_of.frombytes(packed[1])
        parts.append((ids, rule_of, stop - start))
    return rulelib.merge_runs(parts, compiled, limit)
