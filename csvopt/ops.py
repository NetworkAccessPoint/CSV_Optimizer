"""Query operations over a :class:`~csvopt.table.Table`: filter, sort, search,
replace and per-column statistics.

Every operation streams the table instead of materialising it, and takes an
optional ``progress`` callback so the UI can show a progress bar and cancel a
scan that is taking too long on a multi-gigabyte log.
"""

from __future__ import annotations

import re
from array import array
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .table import Table

Progress = Optional[Callable[[int, int], bool]]

TEXT_OPS = ("contains", "equals", "starts", "ends", "regex")
NUM_OPS = ("gt", "ge", "lt", "le", "between")
NULL_OPS = ("empty", "not_empty")
ALL_OPS = TEXT_OPS + NUM_OPS + NULL_OPS + ("in", "time_between")

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%d/%b/%Y:%H:%M:%S",
    "%Y-%m-%d",
    "%H:%M:%S",
)


def parse_number(value: str) -> Optional[float]:
    if not value:
        return None
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        # Trailing units are common in logs: "12ms", "3.4MB".
        match = re.match(r"^[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def parse_timestamp(value: str) -> Optional[float]:
    """Best-effort timestamp -> epoch seconds, for time-range filtering."""
    text = (value or "").strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        num = float(text)
        if num > 1e11:  # milliseconds
            num /= 1000.0
        if 0 < num < 4e10:
            return num
    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(text[:32], fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


@dataclass
class Condition:
    col: Optional[int]          # column id, None = every column
    op: str
    value: str = ""
    value2: str = ""
    case_sensitive: bool = False
    negate: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Condition":
        col = data.get("col")
        return cls(
            col=None if col in (None, "", "*") else int(col),
            op=str(data.get("op", "contains")),
            value=str(data.get("value", "")),
            value2=str(data.get("value2", "")),
            case_sensitive=bool(data.get("case_sensitive")),
            negate=bool(data.get("negate")),
        )


class CompiledCondition:
    def __init__(self, cond: Condition, indexes: list[int]):
        self.cond = cond
        self.indexes = indexes  # positions in a projected row, [] = all columns
        self.op = cond.op
        self.negate = cond.negate
        needle = cond.value if cond.case_sensitive else cond.value.lower()
        self.needle = needle
        self.regex: Optional[re.Pattern] = None
        self.number = parse_number(cond.value)
        self.number2 = parse_number(cond.value2)
        self.time = parse_timestamp(cond.value)
        self.time2 = parse_timestamp(cond.value2)
        self.values = {v.strip() if cond.case_sensitive else v.strip().lower()
                       for v in cond.value.split(",") if v.strip()}
        if cond.op == "regex":
            flags = 0 if cond.case_sensitive else re.IGNORECASE
            self.regex = re.compile(cond.value, flags)

    def _cell_match(self, cell: str) -> bool:
        op = self.op
        if op == "empty":
            return not cell.strip()
        if op == "not_empty":
            return bool(cell.strip())
        if op == "regex":
            return bool(self.regex.search(cell))
        if op in NUM_OPS:
            num = parse_number(cell)
            if num is None:
                return False
            if op == "gt":
                return self.number is not None and num > self.number
            if op == "ge":
                return self.number is not None and num >= self.number
            if op == "lt":
                return self.number is not None and num < self.number
            if op == "le":
                return self.number is not None and num <= self.number
            if op == "between":
                if self.number is None or self.number2 is None:
                    return False
                lo, hi = sorted((self.number, self.number2))
                return lo <= num <= hi
        if op == "time_between":
            ts = parse_timestamp(cell)
            if ts is None:
                return False
            lo = self.time if self.time is not None else float("-inf")
            hi = self.time2 if self.time2 is not None else float("inf")
            return lo <= ts <= hi
        hay = cell if self.cond.case_sensitive else cell.lower()
        if op == "contains":
            return self.needle in hay
        if op == "equals":
            return hay == self.needle
        if op == "starts":
            return hay.startswith(self.needle)
        if op == "ends":
            return hay.endswith(self.needle)
        if op == "in":
            return hay.strip() in self.values
        return False

    def matches(self, row: list[str]) -> bool:
        if self.indexes:
            cells = [row[i] for i in self.indexes if i < len(row)]
        else:
            cells = row
        hit = any(self._cell_match(cell) for cell in cells)
        return not hit if self.negate else hit


def compile_conditions(table: Table, conditions: Iterable[Condition]) -> list[CompiledCondition]:
    compiled = []
    for cond in conditions:
        if cond.op not in ALL_OPS:
            raise ValueError(f"unknown filter operator: {cond.op}")
        if cond.op not in NULL_OPS and not cond.value and cond.op != "time_between":
            continue
        if cond.col is None:
            indexes: list[int] = []
        else:
            idx = table.column_index(cond.col)
            if idx < 0:
                continue
            indexes = [idx]
        compiled.append(CompiledCondition(cond, indexes))
    return compiled


try:  # the regex parser moved in 3.11
    from re import _constants as _re_constants
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - Python 3.9/3.10
    import sre_constants as _re_constants
    import sre_parse as _re_parser

PREFILTERABLE = ("contains", "equals", "starts", "ends", "regex")


def regex_literals(pattern: str, flags: int = 0) -> list[str]:
    """Substrings that every string matching ``pattern`` must contain.

    ``ERROR.*timeout`` must contain both "ERROR" and "timeout"; ``a(b|c)`` only
    guarantees "a".  Handing one of these to the block scanner lets a regex
    filter skip the records it cannot possibly match.  When nothing is
    guaranteed -- ``reset|expired`` -- the caller falls back to a full scan, so
    an empty result is always safe.
    """
    if "(?i" in pattern or "(?m" in pattern or "(?s" in pattern:
        return []  # inline flags could change how the literals match
    try:
        parsed = _re_parser.parse(pattern, flags)
    except Exception:
        return []
    runs: list[str] = []
    current: list[str] = []
    _collect_literals(parsed, runs, current)
    if current:
        runs.append("".join(current))
    return [run for run in runs if run]


def _collect_literals(sequence, runs: list[str], current: list[str]) -> None:
    def flush() -> None:
        if current:
            runs.append("".join(current))
            current.clear()

    for op, value in sequence:
        if op is _re_constants.LITERAL:
            current.append(chr(value))
        elif op is _re_constants.AT:
            continue  # anchors are zero width and never break a literal run
        elif op is _re_constants.SUBPATTERN:
            _group, add_flags, del_flags, subpattern = value
            if add_flags or del_flags:
                flush()
                continue
            _collect_literals(subpattern, runs, current)
        elif op in (_re_constants.MAX_REPEAT, _re_constants.MIN_REPEAT):
            minimum, _maximum, subpattern = value
            if minimum >= 1:
                # The final repetition is adjacent to whatever follows it, so
                # the run continues through the body.
                _collect_literals(subpattern, runs, current)
            else:
                flush()
        else:
            flush()


def _prefilter_literals(table: Table, compiled: list["CompiledCondition"]) -> list[tuple[bytes, bool]]:
    """Literals that must appear in a record's raw bytes for it to match.

    Only positive text conditions whose literal cannot be reshaped by CSV
    quoting contribute -- exactly the ``level = ERROR`` style condition that
    dominates log filtering.  Conditions that cannot contribute are simply
    skipped: with AND semantics, any subset of the required literals is still
    a sound pre-filter.
    """
    encoding = table.dialect.encoding
    specials = {table.dialect.delimiter, table.dialect.quotechar, "\n", "\r"}
    literals: list[tuple[bytes, bool]] = []
    for c in compiled:
        cond = c.cond
        if cond.negate or cond.op not in PREFILTERABLE or not cond.value:
            continue
        if cond.op == "regex":
            flags = 0 if cond.case_sensitive else re.IGNORECASE
            candidates = regex_literals(cond.value, flags)
        else:
            candidates = [cond.value]
        for text in candidates:
            if any(ch in text for ch in specials):
                continue
            # bytes.lower() only folds ASCII, so a case-insensitive literal has
            # to be ASCII for the byte-level search to stay correct.
            if not cond.case_sensitive:
                if not text.isascii():
                    continue
                text = text.lower()
            try:
                literals.append((text.encode(encoding), cond.case_sensitive))
            except (UnicodeEncodeError, LookupError):
                continue
    return literals


def run_filter(
    table: Table,
    conditions: Iterable[Condition],
    match_all: bool = True,
    base_ids: Optional[Iterable[int]] = None,
    limit: Optional[int] = None,
    workers: Optional[int] = 1,
    progress: Progress = None,
) -> array:
    """Return the ids of rows matching ``conditions`` in display order.

    ``workers`` may be a process count, or None to let the machine decide;
    1 keeps everything in this process.
    """
    conditions = list(conditions)
    compiled = compile_conditions(table, conditions)
    out = array("q")
    if not compiled:
        for rid, _row in table.iter_all(ids=base_ids, progress=progress):
            out.append(rid)
        return out
    test = all if match_all else any
    if match_all and base_ids is None and table.can_stream_raw():
        if workers != 1:
            from .parallel import filter_parallel, plan_workers

            count = plan_workers(table, workers)
            if count > 1:
                found = filter_parallel(
                    table, conditions, count, match_all=match_all,
                    limit=limit, progress=progress,
                )
                if found is not None:
                    return found
        literals = _prefilter_literals(table, compiled)
        chosen = _pick_literal(table, literals) if literals else None
        return scan_range(table, compiled, chosen, limit=limit, progress=progress)
    for rid, row in table.iter_all(ids=base_ids, progress=progress):
        if test(c.matches(row) for c in compiled):
            out.append(rid)
            if limit and len(out) >= limit:
                break
    return out


SELECTIVITY_LIMIT = 0.4  # a literal in most records is not worth searching for


def _pick_literal(
    table: Table, literals: list[tuple[bytes, bool]], start_rid: int = 0
) -> Optional[tuple[bytes, bool]]:
    """Choose the most selective literal, or None if none of them helps.

    A literal that occurs in nearly every record (``host-0`` in a host column)
    would make the block scan slower than a straight sequential parse, so it is
    measured on the first block before committing to a strategy.
    """
    try:
        _first_rid, _base, blob = next(iter(table.iter_blocks(start_rid)))
    except StopIteration:
        return None
    average = max(1, table.index.size // max(1, table.row_count))
    records = max(1, len(blob) // average)
    lowered: Optional[bytes] = None
    best: Optional[tuple[int, bytes, bool]] = None
    for literal, case_sensitive in literals:
        if case_sensitive:
            hits = blob.count(literal)
        else:
            if lowered is None:
                lowered = blob.lower()
            hits = lowered.count(literal)
        if best is None or hits < best[0]:
            best = (hits, literal, case_sensitive)
    if best is None or best[0] > SELECTIVITY_LIMIT * records:
        return None
    return best[1], best[2]


def scan_range(
    table: Table,
    compiled: list[CompiledCondition],
    chosen: Optional[tuple[bytes, bool]],
    start_rid: int = 0,
    stop_rid: Optional[int] = None,
    limit: Optional[int] = None,
    progress: Progress = None,
) -> array:
    """Filter a row range straight off disk, block by block.

    With ``chosen`` the block bytes are searched for a literal and only the
    records it lands on are parsed; without one every record in the block is
    parsed.  Either way the work is bounded to ``[start_rid, stop_rid)``, which
    is what lets several processes share one file.
    """
    out = array("q")
    offsets = table.index.offsets
    header = table.header_rows
    for first_rid, base, blob in table.iter_blocks(start_rid, stop_rid, progress=progress):
        if chosen is not None:
            literal, case_sensitive = chosen
            haystack = blob if case_sensitive else blob.lower()
            pos = 0
            rid = first_rid
            while True:
                hit = haystack.find(literal, pos)
                if hit < 0:
                    break
                rid = table.row_at_offset(base + hit, lo=rid)
                row = table.parse_bytes(
                    blob[offsets[rid + header] - base : offsets[rid + header + 1] - base]
                )
                if all(c.matches(row) for c in compiled):
                    out.append(rid)
                    if limit and len(out) >= limit:
                        return out
                pos = offsets[rid + header + 1] - base  # continue after this record
        else:
            rid = first_rid
            for row in table.parse_block(blob):
                if all(c.matches(row) for c in compiled):
                    out.append(rid)
                    if limit and len(out) >= limit:
                        return out
                rid += 1
    return out


def sort_ids(
    table: Table,
    col_id: int,
    descending: bool = False,
    ids: Optional[Iterable[int]] = None,
    max_rows: int = 5_000_000,
    progress: Progress = None,
) -> array:
    """Sort rows by one column, using numeric order when the column looks numeric."""
    idx = table.column_index(col_id)
    if idx < 0:
        raise ValueError("unknown column")
    if table.row_count > max_rows and ids is None:
        raise ValueError(
            f"sorting {table.row_count:,} rows at once is disabled "
            f"(limit {max_rows:,}); filter the view down first"
        )
    keys: list[tuple] = []
    numeric = True
    checked = 0
    for rid, row in table.iter_all(ids=ids, progress=progress):
        cell = row[idx] if idx < len(row) else ""
        num = parse_number(cell)
        if numeric and checked < 2000:
            checked += 1
            if num is None and cell.strip():
                numeric = False
        keys.append((num, cell, rid))
    if numeric:
        keys.sort(key=lambda k: (k[0] is None, k[0] if k[0] is not None else 0.0),
                  reverse=descending)
    else:
        keys.sort(key=lambda k: k[1].lower(), reverse=descending)
    return array("q", [k[2] for k in keys])


@dataclass
class ColumnStats:
    column: str
    total: int
    empty: int
    distinct: int
    top: list[tuple[str, int]]
    numeric_count: int
    minimum: Optional[float]
    maximum: Optional[float]
    mean: Optional[float]
    truncated: bool

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "total": self.total,
            "empty": self.empty,
            "distinct": self.distinct,
            "top": [{"value": v, "count": c} for v, c in self.top],
            "numeric_count": self.numeric_count,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.mean,
            "truncated": self.truncated,
        }


def column_stats(
    table: Table,
    col_id: int,
    top_k: int = 25,
    max_distinct: int = 200_000,
    ids: Optional[Iterable[int]] = None,
    progress: Progress = None,
) -> ColumnStats:
    """Value distribution + numeric summary for one column (a facet panel)."""
    idx = table.column_index(col_id)
    if idx < 0:
        raise ValueError("unknown column")
    counter: Counter = Counter()
    total = empty = numeric_count = 0
    minimum = maximum = None
    total_sum = 0.0
    truncated = False
    for _rid, row in table.iter_all(ids=ids, progress=progress):
        cell = row[idx] if idx < len(row) else ""
        total += 1
        if not cell.strip():
            empty += 1
        if len(counter) < max_distinct or cell in counter:
            counter[cell] += 1
        else:
            truncated = True
        num = parse_number(cell)
        if num is not None:
            numeric_count += 1
            total_sum += num
            minimum = num if minimum is None else min(minimum, num)
            maximum = num if maximum is None else max(maximum, num)
    return ColumnStats(
        column=table.columns[idx].name,
        total=total,
        empty=empty,
        distinct=len(counter),
        top=counter.most_common(top_k),
        numeric_count=numeric_count,
        minimum=minimum,
        maximum=maximum,
        mean=(total_sum / numeric_count) if numeric_count else None,
        truncated=truncated,
    )


def find_matches(
    table: Table,
    needle: str,
    col_id: Optional[int] = None,
    use_regex: bool = False,
    case_sensitive: bool = False,
    whole_cell: bool = False,
    ids: Optional[Iterable[int]] = None,
    limit: int = 10_000,
    progress: Progress = None,
) -> list[tuple[int, int]]:
    """Locate cells matching ``needle`` -> list of (row id, column index)."""
    pattern = _needle_pattern(needle, use_regex, case_sensitive, whole_cell)
    cols = range(len(table.columns)) if col_id is None else [table.column_index(col_id)]
    cols = [c for c in cols if c >= 0]
    hits: list[tuple[int, int]] = []
    for rid, row in table.iter_all(ids=ids, progress=progress):
        for c in cols:
            if c < len(row) and pattern.search(row[c]):
                hits.append((rid, c))
                if len(hits) >= limit:
                    return hits
    return hits


def _needle_pattern(needle: str, use_regex: bool, case_sensitive: bool, whole_cell: bool):
    flags = 0 if case_sensitive else re.IGNORECASE
    body = needle if use_regex else re.escape(needle)
    if whole_cell:
        body = rf"\A(?:{body})\Z"
    return re.compile(body, flags)


def replace_all(
    table: Table,
    needle: str,
    replacement: str,
    col_id: Optional[int] = None,
    use_regex: bool = False,
    case_sensitive: bool = False,
    whole_cell: bool = False,
    ids: Optional[Iterable[int]] = None,
    max_changes: int = 500_000,
    progress: Progress = None,
) -> int:
    """Replace across the current view as a single undoable operation."""
    pattern = _needle_pattern(needle, use_regex, case_sensitive, whole_cell)
    cols = range(len(table.columns)) if col_id is None else [table.column_index(col_id)]
    cols = [c for c in cols if c >= 0]
    repl = replacement if use_regex else replacement.replace("\\", "\\\\")
    changes: list[tuple[int, int, str]] = []
    for rid, row in table.iter_all(ids=ids, progress=progress):
        for c in cols:
            if c >= len(row):
                continue
            cell = row[c]
            if not pattern.search(cell):
                continue
            new = pattern.sub(repl, cell)
            if new != cell:
                changes.append((rid, table.columns[c].id, new))
                if len(changes) >= max_changes:
                    break
        if len(changes) >= max_changes:
            break
    if not changes:
        return 0
    return table.set_cells(changes, label=f"replace '{needle}'")


def dedupe(
    table: Table,
    col_ids: Optional[list[int]] = None,
    ids: Optional[Iterable[int]] = None,
    max_keys: int = 5_000_000,
    progress: Progress = None,
) -> list[int]:
    """Row ids whose key (all columns, or the given ones) was already seen.

    Duplicate detection has to remember every distinct key, so it is capped:
    on a hundred-million-row log the caller is told to narrow the view instead
    of watching the process run out of memory.
    """
    if col_ids:
        idxs = [table.column_index(c) for c in col_ids]
        idxs = [i for i in idxs if i >= 0]
    else:
        idxs = list(range(len(table.columns)))
    seen: set = set()
    dupes: list[int] = []
    for rid, row in table.iter_all(ids=ids, progress=progress):
        key = tuple(row[i] if i < len(row) else "" for i in idxs)
        if key in seen:
            dupes.append(rid)
        else:
            if len(seen) >= max_keys:
                raise ValueError(
                    f"중복 검사는 서로 다른 값 {max_keys:,}개까지만 지원합니다. "
                    "필터로 범위를 좁힌 뒤 다시 실행하세요."
                )
            seen.add(key)
    return dupes


def trim_whitespace(
    table: Table,
    col_id: Optional[int] = None,
    ids: Optional[Iterable[int]] = None,
    progress: Progress = None,
) -> int:
    cols = range(len(table.columns)) if col_id is None else [table.column_index(col_id)]
    cols = [c for c in cols if c >= 0]
    changes: list[tuple[int, int, str]] = []
    for rid, row in table.iter_all(ids=ids, progress=progress):
        for c in cols:
            if c < len(row) and row[c] != row[c].strip():
                changes.append((rid, table.columns[c].id, row[c].strip()))
    if not changes:
        return 0
    return table.set_cells(changes, label="trim whitespace")
