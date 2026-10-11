"""Multi-condition rule matching -- the "멀티 로그 서처".

A firewall or ACL review arrives as a table: one row per rule, columns for
source, destination, protocol, port.  The question asked of the log is always
the same shape -- *which rules does this log line satisfy, and which rules never
matched anything?* -- so the rules are AND within a row and OR across rows.

Evaluating fifty rules against every log line would mean fifty predicate chains
per row.  Instead each rule registers under one cheap, highly selective field
value (a protocol, a port) and a row only tests the handful of rules filed under
the values it actually carries.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import re
from array import array
from bisect import bisect_left
from dataclasses import dataclass, field
from itertools import islice, permutations
from typing import Callable, Iterable, Iterator, Optional

from .table import Table

Progress = Optional[Callable[[int, int], bool]]

# How a rule cell is compared against a log cell.
KINDS = ("ip", "ports", "equals", "contains", "regex", "number", "ignore")

_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?\b")
_PORTS = re.compile(r"^\s*\d{1,5}(\s*[,/]\s*\d{1,5})*\s*$")
_NUMERIC = re.compile(r"^\s*[-+]?\d+(\.\d+)?\s*$")


# --------------------------------------------------------------------------
# parsing a pasted table


def parse_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Split text copied from Excel, Word or a CSV into header + rows.

    Excel and Word paste tab-separated; a CSV export pastes comma-separated.
    Whichever separator produces a consistent column count wins.
    """
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return [], []
    best: tuple[int, list[list[str]]] = (0, [])
    for delimiter in ("\t", ",", ";", "|"):
        rows = _split_rows(lines, delimiter)
        widths = {len(r) for r in rows}
        width = max(widths) if widths else 0
        if width < 2:
            continue
        # Prefer the separator giving the most columns with the least raggedness.
        score = width * 10 - (len(widths) - 1)
        if score > best[0]:
            best = (score, rows)
    rows = best[1] or [[line.strip()] for line in lines]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    if _looks_like_header(rows[0], rows[1:]):
        return [c.strip() for c in rows[0]], [r for r in rows[1:]]
    return [f"열{i + 1}" for i in range(width)], rows


def _split_rows(lines: list[str], delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    return [[cell.strip() for cell in row] for row in reader if any(c.strip() for c in row)]


def _looks_like_header(first: list[str], rest: list[list[str]]) -> bool:
    if not rest:
        return False
    # A header has no addresses, ports or numbers in it.
    for cell in first:
        if not cell:
            continue
        if _IPV4.search(cell) or _PORTS.match(cell):
            return False
    return True


# Columns that describe a rule rather than constrain it.
DESCRIPTION_NAMES = (
    "purpose", "description", "desc", "comment", "note", "remark", "reason",
    "비고", "용도", "설명", "메모", "목적",
)


def guess_kind(values: Iterable[str], name: str = "") -> str:
    """Pick the comparison that fits a rule column, from its name and values."""
    if name and name.strip().lower() in DESCRIPTION_NAMES:
        return "ignore"
    seen = [v.strip() for v in values if v and v.strip()]
    if not seen:
        return "ignore"
    if all(_IPV4.search(v) for v in seen):
        return "ip"
    if all(_PORTS.match(v) for v in seen):
        return "ports"
    if all(_NUMERIC.match(v) for v in seen):
        return "number"
    if any(len(v) > 40 for v in seen):
        return "ignore"      # free text like "Purpose" is description, not a filter
    return "equals"


# --------------------------------------------------------------------------
# field matchers


def ipv4_to_int(text: str) -> Optional[int]:
    """First IPv4 address in a cell as an integer (None when there is none)."""
    match = _IPV4.search(text)
    if not match:
        return None
    try:
        a, b, c, d = (int(part) for part in match.group(1).split("."))
    except ValueError:
        return None
    if a > 255 or b > 255 or c > 255 or d > 255:
        return None
    return (a << 24) | (b << 16) | (c << 8) | d


def parse_networks(text: str) -> list[tuple[int, int]]:
    """Address ranges a rule cell covers: ``10.0.0.0/26`` or a bare address."""
    out: list[tuple[int, int]] = []
    for piece in re.split(r"[,\s]+", text.strip()):
        if not piece:
            continue
        try:
            net = ipaddress.ip_network(piece, strict=False)
        except ValueError:
            value = ipv4_to_int(piece)
            if value is not None:
                out.append((value, value))
            continue
        if net.version != 4:
            continue
        out.append((int(net.network_address), int(net.broadcast_address)))
    return out


def parse_ports(text: str) -> set[str]:
    return {p.strip() for p in re.split(r"[,/\s]+", text.strip()) if p.strip()}


# Addresses repeat heavily in a log -- a few hundred distinct values across
# millions of rows -- so the text->int conversion is memoised.
_IP_CACHE: dict[str, Optional[int]] = {}
_IP_CACHE_LIMIT = 1 << 17


def ip_value(cell: str) -> Optional[int]:
    """:func:`ipv4_to_int` with a cache keyed on the raw cell text."""
    try:
        return _IP_CACHE[cell]
    except KeyError:
        pass
    value = ipv4_to_int(cell)
    if len(_IP_CACHE) >= _IP_CACHE_LIMIT:
        _IP_CACHE.clear()
    _IP_CACHE[cell] = value
    return value


@dataclass
class Condition:
    """One cell of a rule row: which log column, compared how."""

    column: int          # column id in the table
    kind: str
    value: str
    index: int = -1      # resolved position in a projected row

    networks: list[tuple[int, int]] = field(default_factory=list)
    ports: set[str] = field(default_factory=set)
    needle: str = ""
    pattern: Optional[re.Pattern] = None
    number: Optional[float] = None

    def compile(self, position: int) -> "Condition":
        self.index = position
        if self.kind == "ip":
            self.networks = parse_networks(self.value)
        elif self.kind == "ports":
            self.ports = parse_ports(self.value)
        elif self.kind == "regex":
            self.pattern = re.compile(self.value, re.IGNORECASE)
        elif self.kind == "number":
            try:
                self.number = float(self.value.strip())
            except ValueError:
                self.number = None
        else:
            self.needle = self.value.strip().lower()
        return self

    @property
    def exact_values(self) -> list[str]:
        """Values this condition can be bucketed under, if any."""
        if self.kind == "ports":
            return sorted(self.ports)
        if self.kind == "equals" and self.needle:
            return [self.needle]
        return []

    def predicate(self) -> Callable[[list[str]], bool]:
        """A closure testing this one cell of a row.

        Deciding *how* to compare once, here, keeps the scan free of the
        per-row ``if kind == ...`` ladder; across tens of millions of rows
        times a few conditions each, that dispatch is most of the cost.
        """
        index = self.index
        if index < 0:
            return lambda row: False
        kind = self.kind
        if kind == "ip":
            networks = self.networks
            if not networks:
                return lambda row: False
            if len(networks) == 1:
                low, high = networks[0]

                def one_network(row: list[str]) -> bool:
                    value = ip_value(row[index])
                    return value is not None and low <= value <= high

                return one_network

            def any_network(row: list[str]) -> bool:
                value = ip_value(row[index])
                if value is None:
                    return False
                return any(lo <= value <= hi for lo, hi in networks)

            return any_network
        if kind == "ports":
            ports = self.ports
            return lambda row: row[index].strip() in ports
        if kind == "equals":
            needle = self.needle
            return lambda row: row[index].strip().lower() == needle
        if kind == "contains":
            needle = self.needle
            return lambda row: needle in row[index].lower()
        if kind == "regex":
            search = self.pattern.search
            return lambda row: search(row[index]) is not None
        if kind == "number":
            number = self.number
            if number is None:
                return lambda row: False

            def same_number(row: list[str]) -> bool:
                try:
                    return float(row[index].strip()) == number
                except ValueError:
                    return False

            return same_number
        return lambda row: True  # "ignore"

    def matches(self, row: list[str]) -> bool:
        """Single-cell test for callers outside a compiled scan."""
        if self.index < 0 or self.index >= len(row):
            return False
        return self.predicate()(row)


@dataclass
class Rule:
    """One row of the pasted table."""

    label: str
    conditions: list[Condition]
    source: list[str] = field(default_factory=list)   # original cells, for display

    def test(self) -> Callable[[list[str]], bool]:
        """A closure testing every condition of this rule against a row."""
        tests = tuple(condition.predicate() for condition in self.conditions)
        if len(tests) == 1:
            only = tests[0]

            def one(row: list[str]) -> bool:
                try:
                    return only(row)
                except IndexError:
                    return False     # a short record cannot satisfy the rule

            return one

        def every(row: list[str]) -> bool:
            try:
                for check in tests:
                    if not check(row):
                        return False
            except IndexError:
                return False
            return True

        return every

    def matches(self, row: list[str]) -> bool:
        return self.test()(row)


class RuleSet:
    """Compiled rules plus the bucket index that keeps matching cheap."""

    def __init__(self, rules: list[Rule], table: Table):
        self.rules = rules
        self.columns = [c.id for c in table.columns]
        positions = {col.id: i for i, col in enumerate(table.columns)}
        for rule in rules:
            kept = []
            for condition in rule.conditions:
                if condition.kind == "ignore" or condition.column not in positions:
                    continue
                kept.append(condition.compile(positions[condition.column]))
            rule.conditions = kept
        self.buckets: dict[tuple[int, str], list[int]] = {}
        self.key_columns: list[int] = []
        self.always: list[int] = []
        self._build_index()
        self._tests = [rule.test() for rule in rules]
        # One key column and no catch-all rule is the normal shape of a policy
        # table, and that case costs a single dict lookup per row.
        self._single_key = (
            self.key_columns[0]
            if len(self.key_columns) == 1 and not self.always
            else None
        )

    def _build_index(self) -> None:
        """File each rule under its most selective exact value, when it has one.

        Selectivity has to be judged against the whole table, not one rule:
        in a 500-line policy every rule names a protocol, so filing by protocol
        would put 250 rules in one bucket and test them all on every TCP line.
        Counting how many rules share each value first picks the port instead.
        """
        options: list[list[Condition]] = []
        shared: dict[tuple[int, str], int] = {}
        for rule in self.rules:
            usable = [c for c in rule.conditions if c.exact_values]
            options.append(usable)
            for condition in usable:
                for value in condition.exact_values:
                    key = (condition.index, value)
                    shared[key] = shared.get(key, 0) + 1
        for index, usable in enumerate(options):
            chosen: Optional[Condition] = None
            best: Optional[tuple[int, int]] = None
            for condition in usable:
                values = condition.exact_values
                # What filing here would cost: the most rules a single value
                # of it would drag into one bucket, then the bucket count.
                cost = (max(shared[(condition.index, v)] for v in values), len(values))
                if best is None or cost < best:
                    best, chosen = cost, condition
            if chosen is None:
                self.always.append(index)
                continue
            for value in chosen.exact_values:
                self.buckets.setdefault((chosen.index, value), []).append(index)
            if chosen.index not in self.key_columns:
                self.key_columns.append(chosen.index)

    @property
    def empty(self) -> bool:
        return not self.rules

    def candidates(self, row: list[str]) -> list[int]:
        """The rules worth testing against this row, in rule order."""
        found: Optional[list[int]] = None
        buckets = self.buckets
        for position in self.key_columns:
            try:
                cell = row[position]
            except IndexError:
                continue
            hit = buckets.get((position, cell.strip().lower()))
            if hit:
                found = hit if found is None else sorted(set(found).union(hit))
        if found is None:
            return self.always
        if self.always:
            return sorted(set(found).union(self.always))
        return found

    def match(self, row: list[str]) -> int:
        """Index of the first rule the row satisfies, or -1.

        First match, like a firewall: the table is read top to bottom and the
        earliest rule that fits owns the row.
        """
        tests = self._tests
        position = self._single_key
        if position is not None:
            try:
                cell = row[position]
            except IndexError:
                return -1
            candidates = self.buckets.get((position, cell.strip().lower()))
            if not candidates:
                return -1
        else:
            candidates = self.candidates(row)
        for index in candidates:
            if tests[index](row):
                return index
        return -1


# --------------------------------------------------------------------------
# guessing which log column each pasted column means

# Words that mark which end of a connection a column describes.
_ROLE_WORDS = {
    "src": ("src", "source", "from", "client", "sender", "출발", "출발지", "소스",
            "발신", "송신", "원본"),
    "dst": ("dst", "dest", "destination", "to", "target", "server", "receiver",
            "목적", "목적지", "대상", "수신", "도착"),
}
# Words that mark what kind of value a column holds.
_KIND_WORDS = {
    "ip": ("ip", "ipv4", "addr", "address", "host", "주소", "아이피", "호스트"),
    "ports": ("port", "ports", "포트", "svc", "service", "서비스"),
    "equals": ("protocol", "proto", "프로토콜"),
}
# Columns a firewall adds beside the real one: the address after translation,
# the port after translation.  A policy table talks about the traffic as the
# rule sees it, so a name the rule did not ask for counts against the column.
_QUALIFIER_WORDS = ("nat", "snat", "dnat", "xlate", "translated", "mapped",
                    "post", "변환", "변환후")


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^0-9A-Za-z가-힣]+", name.lower()) if t}


def _role(name: str) -> str:
    """Which end of the connection a column name describes, if it says."""
    tokens = _tokens(name)
    for role, words in _ROLE_WORDS.items():
        if tokens & set(words):
            return role
    return ""


def _compatible(rule_kind: str, log_kind: str) -> bool:
    """Whether a log column could hold the values this rule column compares."""
    if rule_kind == "ip":
        return log_kind == "ip"
    if rule_kind in ("ports", "number"):
        return log_kind in ("ports", "number")
    return True


def sample_columns(table: Table, sample: int = 200) -> list[list[str]]:
    """The first few values of every log column, for guessing what it holds."""
    _ids, rows = table.read_range(0, sample)
    return [
        [row[i] for row in rows if i < len(row)]
        for i in range(len(table.columns))
    ]


def column_kinds(table: Table, sample: int = 200) -> list[str]:
    """Classify each log column from a sample of its values."""
    return [guess_kind(values) for values in sample_columns(table, sample)]


def _wanted_values(kind: str, cells: Iterable[str]) -> set:
    """The literal values a rule column is looking for, where it has any."""
    out: set = set()
    for cell in cells:
        text = str(cell).strip()
        if not text:
            continue
        if kind == "ports":
            out |= {p.lower() for p in parse_ports(text)}
        elif kind in ("equals", "number"):
            out.add(text.lower())
    return out


def auto_mapping(
    table: Table,
    header: list[str],
    rows: list[list[str]],
    kinds: Optional[list[str]] = None,
) -> list[dict]:
    """Guess a column mapping for a freshly pasted table.

    Names decide it where they can -- ``Source`` next to ``src_ip`` is not a
    coincidence -- and the sampled values veto anything impossible, so a
    ``Port`` column never lands on a timestamp.  The result is a suggestion the
    user can correct in the panel, not a verdict.
    """
    rule_kinds = kinds or [
        guess_kind([row[i] for row in rows if i < len(row)], header[i] if i < len(header) else "")
        for i in range(len(header))
    ]
    columns = sample_columns(table)
    log_kinds = [guess_kind(values) for values in columns]
    log_values = [{v.strip().lower() for v in values if v.strip()} for values in columns]
    scores: list[tuple[int, int, int]] = []        # (score, pasted, log column)
    for position, rule_kind in enumerate(rule_kinds):
        if rule_kind == "ignore":
            continue
        name = header[position] if position < len(header) else ""
        rule_tokens = _tokens(name)
        rule_role = _role(name)
        wanted = _wanted_values(
            rule_kind, [row[position] for row in rows if position < len(row)]
        )
        for index, column in enumerate(table.columns):
            if not _compatible(rule_kind, log_kinds[index]):
                continue
            log_tokens = _tokens(column.name)
            log_role = _role(column.name)
            # An address or a port column is distinctive enough to be worth
            # guessing from the values alone -- a log without a header row has
            # no names to go on.  A plain string column is not.
            score = 5 if rule_kind in ("ip", "ports", "number") else 0
            score += 10 * len(rule_tokens & log_tokens)
            if rule_tokens and rule_tokens == log_tokens:
                score += 40
            if rule_role and log_role:
                score += 30 if rule_role == log_role else -40
            if log_tokens & set(_KIND_WORDS.get(rule_kind, ())):
                score += 10
            if wanted and log_values[index] & wanted:
                # The log really does carry these values -- the strongest hint
                # there is, and the only one left when the log has no header.
                score += 25
            if not rule_role and log_role == "dst":
                # A policy table's bare "Port" means the destination port.
                score += 6
            qualifiers = set(_QUALIFIER_WORDS)
            if (log_tokens & qualifiers) and not (rule_tokens & qualifiers):
                # "NAT Source IP" is not what a rule's "Source" means.
                score -= 15
            if score > 0:
                scores.append((score, position, index))
    # Greedy one-to-one assignment: the most confident pair wins first.
    mapping: list[dict] = [
        {"column": None, "kind": kind} for kind in rule_kinds
    ]
    ranked: dict[int, list[int]] = {}
    taken_logs: set[int] = set()
    taken_rules: set[int] = set()
    for _score, position, index in sorted(scores, key=lambda s: (-s[0], s[1], s[2])):
        ranked.setdefault(position, []).append(table.columns[index].id)
        if position in taken_rules or index in taken_logs:
            continue
        mapping[position]["column"] = table.columns[index].id
        taken_rules.add(position)
        taken_logs.add(index)
    return repair_mapping(table, rows, mapping, ranked)


def _sample_hits(table: Table, rows: list[list[str]], mapping: list[dict],
                 data: list[list[str]]) -> int:
    """How many of the sampled log rows this mapping would catch."""
    ruleset = build_ruleset(table, rows, mapping)
    if ruleset.empty:
        return 0
    match = ruleset.match
    return sum(1 for row in data if match(row) >= 0)


def _reassign(mapping: list[dict], position: int, column: int) -> list[dict]:
    """Hand one rule column a different log column, swapping with its owner."""
    trial = [dict(spec) for spec in mapping]
    for other, spec in enumerate(trial):
        if other != position and spec["column"] == column:
            spec["column"] = trial[position]["column"]
    trial[position]["column"] = column
    return trial


def repair_mapping(
    table: Table,
    rows: list[list[str]],
    mapping: list[dict],
    ranked: dict,
    sample: int = 2000,
    tries: int = 4,
    passes: int = 2,
) -> list[dict]:
    """Try the runners-up when the guessed mapping catches nothing at all.

    A mapping that is merely wrong looks exactly like a clean bill of health:
    every rule reports zero hits, which a policy review reads as "none of this
    is in use".  Names cannot always settle it -- a log may call its columns
    ``f1`` and ``f2``, or offer ``Source address`` beside ``NAT Source IP`` --
    so when nothing matches, the next-best candidates are worth the moment it
    takes to try them against a sample.

    A mapping that already catches rows is never touched, and if no
    arrangement catches anything the original guess stands: the rules may
    simply be unused, which is an answer in itself.
    """
    _ids, data = table.read_range(0, sample)
    if not data or _sample_hits(table, rows, mapping, data):
        return mapping
    current = mapping
    for _ in range(passes):
        best: Optional[list[dict]] = None
        best_hits = 0
        for position, candidates in ranked.items():
            if current[position].get("kind") == "ignore":
                continue
            for column in candidates[:tries]:
                if column == current[position]["column"]:
                    continue
                trial = _reassign(current, position, column)
                hits = _sample_hits(table, rows, trial, data)
                if hits > best_hits:
                    best, best_hits = trial, hits
        if best is None:
            break
        current = best
    return current if _sample_hits(table, rows, current, data) else mapping


def build_ruleset(
    table: Table,
    rows: list[list[str]],
    mapping: list[dict],
    labels: Optional[list[str]] = None,
) -> RuleSet:
    """Turn pasted rows plus a column mapping into a compiled rule set.

    ``mapping`` has one entry per pasted column::

        {"column": <log column id or None>, "kind": "ip" | "ports" | ...}

    An empty rule cell means "any", so it simply contributes no condition.
    """
    rules: list[Rule] = []
    for position, cells in enumerate(rows):
        conditions: list[Condition] = []
        for cell, spec in zip(cells, mapping):
            column = spec.get("column")
            kind = spec.get("kind", "equals")
            if column is None or kind == "ignore" or not str(cell).strip():
                continue
            conditions.append(Condition(column=int(column), kind=kind, value=str(cell)))
        if not conditions:
            continue  # a row of nothing but description would match everything
        label = labels[position] if labels and position < len(labels) else ""
        rules.append(Rule(label=label or f"규칙 {position + 1}", conditions=conditions,
                          source=list(cells)))
    return RuleSet(rules, table)


# --------------------------------------------------------------------------
# scanning


@dataclass
class RuleRunResult:
    ids: array                 # matched row ids, ascending
    rule_of: array             # rule index per matched row (parallel to ids)
    counts: list[int]          # hits per rule
    scanned: int
    labels: list[str]

    def rule_for(self, rid: int) -> int:
        """Which rule a matched row belongs to (-1 if it is not in the result)."""
        position = bisect_left(self.ids, rid)
        if position < len(self.ids) and self.ids[position] == rid:
            return self.rule_of[position]
        return -1

    def ids_for(self, rule: Optional[int]) -> array:
        if rule is None or rule < 0:
            return self.ids
        return array("q", (rid for rid, r in zip(self.ids, self.rule_of) if r == rule))

    def as_dict(self) -> dict:
        return {
            "matched": len(self.ids),
            "scanned": self.scanned,
            "rules": [
                {"index": i, "label": label, "count": count}
                for i, (label, count) in enumerate(zip(self.labels, self.counts))
            ],
        }


def scan_rules_range(
    table: Table,
    ruleset: RuleSet,
    start_rid: int = 0,
    stop_rid: Optional[int] = None,
    limit: Optional[int] = None,
    progress: Progress = None,
) -> tuple[array, array, int]:
    """Match a row range straight off disk, block by block.

    Parsing a 4 MB block in one pass costs far less than seeking per row, and
    bounding the work to ``[start_rid, stop_rid)`` is what lets several
    processes share one file.
    """
    matched = array("q")
    rule_of = array("i")
    match = ruleset.match
    append_id = matched.append
    append_rule = rule_of.append
    scanned = 0
    for first_rid, _base, blob in table.iter_blocks(start_rid, stop_rid, progress=progress):
        rid = first_rid
        stop = stop_rid if stop_rid is not None else -1
        for row in table.parse_block(blob):
            if stop >= 0 and rid >= stop:
                break
            scanned += 1
            index = match(row)
            if index >= 0:
                append_id(rid)
                append_rule(index)
                if limit and len(matched) >= limit:
                    return matched, rule_of, scanned
            rid += 1
    return matched, rule_of, scanned


def run_rules(
    table: Table,
    ruleset: RuleSet,
    ids: Optional[Iterable[int]] = None,
    start_rid: int = 0,
    stop_rid: Optional[int] = None,
    limit: Optional[int] = None,
    progress: Progress = None,
) -> RuleRunResult:
    """Scan once, recording which rule each matching row belongs to."""
    labels = [rule.label for rule in ruleset.rules]
    counts = [0] * len(ruleset.rules)
    if ruleset.empty:
        return RuleRunResult(array("q"), array("i"), counts, 0, labels)
    if ids is None and table.can_stream_raw():
        matched, rule_of, scanned = scan_rules_range(
            table, ruleset, start_rid, stop_rid, limit, progress
        )
    else:
        # An edited table, or an explicit subset: go through the overlay so
        # unsaved changes and the active view are both honoured.
        matched = array("q")
        rule_of = array("i")
        scanned = 0
        rows = table.iter_all(ids=ids, progress=progress)
        if start_rid or stop_rid is not None:
            rows = islice(rows, start_rid, stop_rid)
        match = ruleset.match
        for rid, row in rows:
            scanned += 1
            index = match(row)
            if index >= 0:
                matched.append(rid)
                rule_of.append(index)
                if limit and len(matched) >= limit:
                    break
    for index in rule_of:
        counts[index] += 1
    return RuleRunResult(
        ids=matched,
        rule_of=rule_of,
        counts=counts,
        scanned=scanned,
        labels=labels,
    )


def merge_runs(
    parts: Iterable[tuple[array, array, int]],
    labels: list[str],
    limit: Optional[int] = None,
) -> RuleRunResult:
    """Join segment results, which arrive already in ascending row order."""
    ids = array("q")
    rule_of = array("i")
    counts = [0] * len(labels)
    scanned = 0
    for part_ids, part_rules, part_scanned in parts:
        scanned += part_scanned
        ids.extend(part_ids)
        rule_of.extend(part_rules)
        if limit and len(ids) >= limit:
            del ids[limit:]
            del rule_of[limit:]
            break
    for index in rule_of:
        counts[index] += 1
    return RuleRunResult(ids=ids, rule_of=rule_of, counts=counts,
                         scanned=scanned, labels=labels)


def iter_unmatched_rules(result: RuleRunResult) -> Iterator[tuple[int, str]]:
    """Rules nothing matched -- usually the interesting half of a review."""
    for index, (label, count) in enumerate(zip(result.labels, result.counts)):
        if count == 0:
            yield index, label
