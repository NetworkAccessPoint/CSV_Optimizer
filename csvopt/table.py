"""Editable, randomly addressable view over a large delimited file.

Nothing is loaded eagerly: the file stays on disk, an offset index gives O(1)
access to any record, and edits live in a small overlay (changed cells,
inserted rows, deleted rows, column plan) until the user saves.  That is what
lets a 5 GB log be opened, filtered and edited with a few dozen megabytes of
process memory.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import sys
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

from .bitset import POPCOUNT, Bitset
from .index import Aborted, Dialect, RowIndex, build_index, drop_cached_index, sniff

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

HEAD_ANCHOR = -1  # insertion anchor meaning "before the first row"


class SaveError(Exception):
    """Raised when a file cannot be written back to disk."""


class Fenwick:
    """Fenwick tree over per-row slot counts, used for position <-> row lookup."""

    def __init__(self, size: int, initial: int = 1):
        self.size = size
        self.tree = array("l", [0]) * (size + 1)
        if initial:
            # Build in O(n): every node starts holding `initial` per covered leaf.
            for i in range(1, size + 1):
                self.tree[i] += initial * (i & -i)
        self.total = size * initial

    def add(self, i: int, delta: int) -> None:
        self.total += delta
        i += 1
        while i <= self.size:
            self.tree[i] += delta
            i += i & -i

    def prefix(self, i: int) -> int:
        """Sum of slots for rows [0, i)."""
        s = 0
        while i > 0:
            s += self.tree[i]
            i -= i & -i
        return s

    def find(self, k: int) -> tuple[int, int]:
        """Return (row, offset) for the k-th slot (0-based)."""
        idx = 0
        bit = 1 << (self.size.bit_length())
        remaining = k
        while bit:
            nxt = idx + bit
            if nxt <= self.size and self.tree[nxt] <= remaining:
                idx = nxt
                remaining -= self.tree[nxt]
            bit >>= 1
        return idx, remaining


BLOCK_SHIFT = 13                  # 8192 rows per block
BLOCK_ROWS = 1 << BLOCK_SHIFT
BLOCK_BYTES = BLOCK_ROWS >> 3


class RowSequence:
    """Display order over physical rows, with deletions and insertions applied.

    Physical rows are identified by their index in the file (>= 0); inserted
    rows get negative ids.  Until the first structural edit this is a plain
    identity mapping and costs nothing.

    Deletions live in a bitset and positions are resolved through a Fenwick tree
    over 8192-row blocks, so deleting fifty million rows costs a few megabytes
    and one rebuild rather than fifty million tree updates.
    """

    def __init__(self, base_count: int):
        self.base_count = base_count
        self.inserts: dict[int, list[int]] = {}  # anchor row -> ids placed after it
        self.head: list[int] = []                # ids placed before the first row
        self.alive: Optional[Bitset] = None
        self._fen: Optional[Fenwick] = None
        self._insert_blocks: set[int] = set()

    # -------------------------------------------------------------- structure

    @property
    def blocks(self) -> int:
        return max(1, (self.base_count + BLOCK_ROWS - 1) // BLOCK_ROWS)

    @property
    def dirty(self) -> bool:
        return bool(self.deleted_count or self.inserts or self.head)

    @property
    def deleted_count(self) -> int:
        return 0 if self.alive is None else self.base_count - self.alive.count

    @property
    def count(self) -> int:
        if self._fen is None:
            return self.base_count
        return self._fen.total + len(self.head)

    def _ensure(self) -> None:
        """Materialise the bitset and block tree on the first structural edit."""
        if self._fen is not None:
            return
        self.alive = Bitset(self.base_count, True)
        fen = Fenwick(self.blocks, 0)
        full_blocks, tail = divmod(self.base_count, BLOCK_ROWS)
        for block in range(full_blocks):
            fen.add(block, BLOCK_ROWS)
        if tail:
            fen.add(full_blocks, tail)
        self._fen = fen

    def _rebuild(self) -> None:
        """Recount every block from the bitset (used after a bulk change)."""
        self._ensure()
        fen = Fenwick(self.blocks, 0)
        bits = self.alive.bits
        for block in range(self.blocks):
            lo = block * BLOCK_BYTES
            segment = bits[lo : lo + BLOCK_BYTES]
            total = sum(POPCOUNT[b] for b in segment)
            if total:
                fen.add(block, total)
        for anchor, ids in self.inserts.items():
            fen.add(anchor >> BLOCK_SHIFT, len(ids))
        self._fen = fen

    def is_deleted(self, row: int) -> bool:
        return self.alive is not None and not self.alive.get(row)

    # ------------------------------------------------------------- addressing

    def at(self, pos: int) -> int:
        """Row id shown at display position ``pos``."""
        if self._fen is None:
            if 0 <= pos < self.base_count:
                return pos
            raise IndexError(pos)
        if pos < 0:
            raise IndexError(pos)
        if pos < len(self.head):
            return self.head[pos]
        remaining = pos - len(self.head)
        if remaining >= self._fen.total:
            raise IndexError(pos)
        block, offset = self._fen.find(remaining)
        return self._resolve(block, offset)

    def _resolve(self, block: int, offset: int) -> int:
        row = block << BLOCK_SHIFT
        if block in self._insert_blocks:
            end = min(row + BLOCK_ROWS, self.base_count)
            while row < end:
                if self.alive.get(row):
                    if offset == 0:
                        return row
                    offset -= 1
                ids = self.inserts.get(row)
                if ids:
                    if offset < len(ids):
                        return ids[offset]
                    offset -= len(ids)
                row += 1
            raise IndexError(block)
        # No inserted rows in this block: walk the bitset a byte at a time.
        bits = self.alive.bits
        index = block * BLOCK_BYTES
        limit = min(index + BLOCK_BYTES, len(bits))
        while index < limit:
            popcount = POPCOUNT[bits[index]]
            if popcount > offset:
                break
            offset -= popcount
            index += 1
        if index >= limit:
            raise IndexError(block)
        byte = bits[index]
        for bit in range(8):
            if byte >> bit & 1:
                if offset == 0:
                    return (index << 3) + bit
                offset -= 1
        raise IndexError(block)

    def _walk(self, row: int, skip: int = 0) -> Iterator[int]:
        """Yield ids from physical row ``row`` on, dropping the first ``skip``."""
        n = self.base_count
        bits = self.alive.bits
        inserts = self.inserts
        while row < n:
            block = row >> BLOCK_SHIFT
            end = min((block + 1) << BLOCK_SHIFT, n)
            if block in self._insert_blocks:
                while row < end:
                    if bits[row >> 3] >> (row & 7) & 1:
                        if skip:
                            skip -= 1
                        else:
                            yield row
                    for rid in inserts.get(row, ()):
                        if skip:
                            skip -= 1
                        else:
                            yield rid
                    row += 1
            else:
                while row < end:
                    if (row & 7) == 0 and bits[row >> 3] == 0:
                        row += 8  # eight deleted rows in a row: skip the byte
                        continue
                    if bits[row >> 3] >> (row & 7) & 1:
                        if skip:
                            skip -= 1
                        else:
                            yield row
                    row += 1

    def iter_from(self, pos: int) -> Iterator[int]:
        """Ids in display order starting at display position ``pos``."""
        if self._fen is None:
            yield from range(max(0, pos), self.base_count)
            return
        pos = max(0, pos)
        if pos < len(self.head):
            yield from self.head[pos:]
            yield from self._walk(0)
            return
        remaining = pos - len(self.head)
        if remaining >= self._fen.total:
            return
        block, offset = self._fen.find(remaining)
        yield from self._walk(block << BLOCK_SHIFT, offset)

    def slice(self, start: int, count: int) -> list[int]:
        if self._fen is None:
            stop = min(self.base_count, max(0, start) + count)
            return list(range(max(0, start), max(0, stop)))
        out = []
        for rid in self.iter_from(start):
            out.append(rid)
            if len(out) >= count:
                break
        return out

    def iter_ids(self) -> Iterator[int]:
        """Every row id in display order (streaming, used when saving)."""
        if self._fen is None:
            yield from range(self.base_count)
            return
        yield from self.head
        yield from self._walk(0)

    def position_of(self, rid: int) -> int:
        """Display position of a row id, or -1 when it is not visible."""
        if self._fen is None:
            return rid if 0 <= rid < self.base_count else -1
        if rid < 0:
            if rid in self.head:
                return self.head.index(rid)
            for anchor, ids in self.inserts.items():
                if rid in ids:
                    base = self._position_of_row(anchor)
                    if base < 0:
                        return -1
                    offset = 0 if self.is_deleted(anchor) else 1
                    return base + offset + ids.index(rid)
            return -1
        return self._position_of_row(rid)

    def _position_of_row(self, row: int) -> int:
        if row < 0 or row >= self.base_count or self.is_deleted(row):
            return -1
        block = row >> BLOCK_SHIFT
        pos = len(self.head) + self._fen.prefix(block)
        block_start = block << BLOCK_SHIFT
        pos += self.alive.count_between(block_start, row)
        if block in self._insert_blocks:
            for anchor, ids in self.inserts.items():
                if block_start <= anchor < row:
                    pos += len(ids)
        return pos

    # ---------------------------------------------------------------- edits

    def delete(self, row: int) -> bool:
        if row < 0:
            for anchor, ids in self.inserts.items():
                if row in ids:
                    ids.remove(row)
                    self._fen.add(anchor >> BLOCK_SHIFT, -1)
                    if not ids:
                        del self.inserts[anchor]
                        self._refresh_insert_blocks()
                    return True
            if row in self.head:
                self.head.remove(row)
                return True
            return False
        if row >= self.base_count:
            return False
        self._ensure()
        if not self.alive.discard(row):
            return False
        self._fen.add(row >> BLOCK_SHIFT, -1)
        return True

    def undelete(self, row: int) -> None:
        if self.alive is None or not (0 <= row < self.base_count):
            return
        if self.alive.add(row):
            self._fen.add(row >> BLOCK_SHIFT, 1)

    def delete_where(self, marks: Bitset) -> int:
        """Delete every row marked in ``marks`` (bulk, one rebuild)."""
        self._ensure()
        before = self.alive.count
        self.alive.difference_update(marks)
        self._rebuild()
        return before - self.alive.count

    def keep_where(self, marks: Bitset) -> int:
        """Delete every row *not* marked in ``marks`` (bulk, one rebuild)."""
        self._ensure()
        before = self.alive.count
        self.alive.intersect_update(marks)
        self._rebuild()
        return before - self.alive.count

    def snapshot(self) -> dict:
        """Compact state for undoing a bulk structural change."""
        return {
            "alive": None if self.alive is None else self.alive.snapshot(),
            "head": list(self.head),
            "inserts": {k: list(v) for k, v in self.inserts.items()},
        }

    def restore(self, state: dict) -> None:
        if state["alive"] is None:
            self.alive = None
            self._fen = None
        else:
            self._ensure()
            self.alive.restore(state["alive"])
        self.head = list(state["head"])
        self.inserts = {k: list(v) for k, v in state["inserts"].items()}
        self._refresh_insert_blocks()
        if self.alive is not None:
            self._rebuild()

    def _refresh_insert_blocks(self) -> None:
        self._insert_blocks = {anchor >> BLOCK_SHIFT for anchor in self.inserts}

    def insert_after(self, anchor: int, rid: int, offset: Optional[int] = None) -> None:
        """Place ``rid`` after ``anchor`` (``HEAD_ANCHOR`` = before row 0)."""
        self._ensure()
        if anchor == HEAD_ANCHOR:
            self.head.insert(len(self.head) if offset is None else offset, rid)
            return
        ids = self.inserts.setdefault(anchor, [])
        ids.insert(len(ids) if offset is None else offset, rid)
        self._insert_blocks.add(anchor >> BLOCK_SHIFT)
        self._fen.add(anchor >> BLOCK_SHIFT, 1)

    def anchor_for(self, pos: int) -> tuple[int, Optional[int]]:
        """Anchor + offset that would place a new row at display position ``pos``."""
        pos = min(max(pos, 0), self.count)
        if pos <= 0:
            return HEAD_ANCHOR, 0
        prev = self.at(pos - 1)
        if prev >= 0:
            return prev, 0
        for anchor, ids in self.inserts.items():
            if prev in ids:
                return anchor, ids.index(prev) + 1
        return HEAD_ANCHOR, self.head.index(prev) + 1


@dataclass
class Column:
    id: int
    name: str
    src: Optional[int]  # index in the physical record, None for a new column

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "src": self.src}


@dataclass
class Op:
    kind: str
    payload: dict = field(default_factory=dict)
    label: str = ""


class Table:
    """A large delimited file plus an in-memory edit overlay."""

    MAX_UNDO = 100
    MAX_BULK_UNDO = 3

    def __init__(
        self,
        path: str,
        dialect: Optional[Dialect] = None,
        index: Optional[RowIndex] = None,
        use_cache: bool = True,
        progress: Optional[Callable[[int, int], bool]] = None,
    ):
        self.path = os.path.abspath(path)
        self.dialect = dialect or sniff(self.path)
        self.index = index or build_index(
            self.path,
            quotechar=self.dialect.quotechar,
            delimiter=self.dialect.delimiter,
            use_cache=use_cache,
            progress=progress,
        )
        self.header_rows = 1 if self.dialect.has_header else 0
        self.base_count = max(0, len(self.index) - self.header_rows)
        self.seq = RowSequence(self.base_count)
        self.columns: list[Column] = []
        self._next_col_id = 0
        self._next_row_id = -2
        self.edits: dict[int, dict[int, str]] = {}
        self.new_rows: dict[int, dict[int, str]] = {}
        self.undo_stack: list[Op] = []
        self.redo_stack: list[Op] = []
        self.view: Optional[array] = None      # row ids of the active filter/sort
        self.view_label: str = ""
        self.marks = Bitset(self.base_count)   # bookmarked physical rows
        self.virtual_marks: set[int] = set()   # bookmarked inserted rows
        self._quote_byte = (
            self.dialect.quotechar.encode(self.dialect.encoding, "replace")
            if self.dialect.quotechar
            else b""
        )
        self._init_columns()
        self._orig_names = [c.name for c in self.columns]

    # ------------------------------------------------------------------ setup

    def _init_columns(self) -> None:
        names: list[str] = []
        if self.header_rows and len(self.index):
            names = self._parse(self._raw_record(0))
        width = len(names)
        if not width:
            sample = self.read_physical(0) if self.base_count else []
            width = len(sample)
        if not self.dialect.has_header:
            names = [f"col{i + 1}" for i in range(width)]
        seen: dict[str, int] = {}
        for i, name in enumerate(names):
            name = name.strip() or f"col{i + 1}"
            if name in seen:
                seen[name] += 1
                name = f"{name}_{seen[name]}"
            else:
                seen[name] = 0
            self.columns.append(Column(self._new_col_id(), name, i))

    def _new_col_id(self) -> int:
        self._next_col_id += 1
        return self._next_col_id

    def _new_row_id(self) -> int:
        rid = self._next_row_id
        self._next_row_id -= 1
        return rid

    def close(self) -> None:
        """Release the memory-mapped index; the overlay is discarded with it."""
        self.index.close()

    # ------------------------------------------------------------------- read

    def _raw_record(self, physical: int) -> str:
        start, end = self.index.span(physical)
        with open(self.path, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start)
        return data.decode(self.dialect.encoding, "replace")

    def _parse(self, text: str) -> list[str]:
        reader = csv.reader(
            io.StringIO(text, newline=""),
            delimiter=self.dialect.delimiter,
            quotechar=self.dialect.quotechar,
        )
        for row in reader:
            return row
        return []

    def parse_bytes(self, data: bytes) -> list[str]:
        """Parse one raw record.

        Records without a quote character -- the overwhelming majority in a log
        -- are split directly, which is several times cheaper than handing them
        to :mod:`csv` and matters when a filter parses millions of candidates.
        """
        if self._quote_byte and self._quote_byte in data:
            return self._parse(data.decode(self.dialect.encoding, "replace"))
        text = data.decode(self.dialect.encoding, "replace")
        if text.endswith("\n"):
            text = text[:-2] if text.endswith("\r\n") else text[:-1]
        return text.split(self.dialect.delimiter)

    def read_physical(self, row: int) -> list[str]:
        """Raw fields of a base row (0-based, header excluded)."""
        return self._parse(self._raw_record(row + self.header_rows))

    def read_physical_many(self, rows: Iterable[int]) -> dict[int, list[str]]:
        """Read several base rows, coalescing consecutive ones into one read."""
        rows = sorted(set(rows))
        out: dict[int, list[str]] = {}
        if not rows:
            return out
        with open(self.path, "rb") as fh:
            run: list[int] = []
            for row in rows + [None]:  # sentinel flushes the last run
                if run and (row is None or row != run[-1] + 1 or len(run) >= 4096):
                    first, last = run[0], run[-1]
                    start = self.index.start(first + self.header_rows)
                    end = self.index.span(last + self.header_rows)[1]
                    fh.seek(start)
                    blob = fh.read(end - start)
                    for r in run:
                        s = self.index.start(r + self.header_rows) - start
                        e = self.index.span(r + self.header_rows)[1] - start
                        out[r] = self._parse(
                            blob[s:e].decode(self.dialect.encoding, "replace")
                        )
                    run = []
                if row is not None:
                    run.append(row)
        return out

    @property
    def _identity_columns(self) -> bool:
        return [c.src for c in self.columns] == list(range(len(self.columns)))

    def project(self, rid: int, raw: Optional[list[str]]) -> list[str]:
        """Apply the column plan and pending edits to a row."""
        overlay = self.new_rows.get(rid) if rid < 0 else self.edits.get(rid)
        if overlay is None and raw is not None and self._identity_columns:
            # Untouched row, untouched column plan: hand back the parsed record.
            if len(raw) == len(self.columns):
                return raw
        out = []
        for col in self.columns:
            if overlay is not None and col.id in overlay:
                out.append(overlay[col.id])
            elif rid >= 0 and raw is not None and col.src is not None and col.src < len(raw):
                out.append(raw[col.src])
            else:
                out.append("")
        return out

    @property
    def row_count(self) -> int:
        return len(self.view) if self.view is not None else self.seq.count

    def row_id_at(self, pos: int) -> int:
        if self.view is not None:
            return self.view[pos]
        return self.seq.at(pos)

    def ids_in_range(self, start: int, count: int) -> list[int]:
        start = max(0, start)
        stop = min(self.row_count, start + count)
        if self.view is not None:
            return list(self.view[start:stop])
        return self.seq.slice(start, stop - start)

    def read_range(self, start: int, count: int) -> tuple[list[int], list[list[str]]]:
        ids = self.ids_in_range(start, count)
        raws = self.read_physical_many([r for r in ids if r >= 0])
        return ids, [self.project(rid, raws.get(rid)) for rid in ids]

    def all_ids(self) -> Iterator[int]:
        """Every row id in display order, ignoring any active filter."""
        return self.seq.iter_ids()

    def read_row(self, rid: int) -> list[str]:
        raw = self.read_physical(rid) if rid >= 0 else None
        return self.project(rid, raw)

    def iter_all(
        self,
        ids: Optional[Iterable[int]] = None,
        progress: Optional[Callable[[int, int], bool]] = None,
        chunk: int = 4096,
    ) -> Iterator[tuple[int, list[str]]]:
        """Stream (row id, projected row) over the whole table or a subset.

        The common case -- an untouched file read front to back -- is served by
        a single sequential pass over the bytes instead of per-row seeks.
        """
        total = self.row_count
        if ids is None and self.view is not None:
            ids = iter(self.view)
        if ids is None and self.view is None and not self.seq.dirty:
            done = 0
            for rid, raw in self._stream_physical():
                yield rid, self.project(rid, raw)
                done += 1
                if progress is not None and done % 8192 == 0:
                    if not progress(done, total):
                        raise Aborted()
            return
        source = self.seq.iter_ids() if ids is None else iter(ids)
        buf: list[int] = []
        done = 0
        while True:
            buf = []
            for rid in source:
                buf.append(rid)
                if len(buf) >= chunk:
                    break
            if not buf:
                return
            raws = self.read_physical_many([r for r in buf if r >= 0])
            for rid in buf:
                yield rid, self.project(rid, raws.get(rid))
            done += len(buf)
            if progress is not None and not progress(done, total):
                raise Aborted()

    def can_stream_raw(self) -> bool:
        """True when the file can be streamed as raw bytes in display order."""
        return (
            self.view is None
            and not self.seq.dirty
            and not self.edits
            and not self.new_rows
            and self._identity_columns
        )

    def iter_blocks(
        self,
        progress: Optional[Callable[[int, int], bool]] = None,
        chunk: int = 4 << 20,
    ) -> Iterator[tuple[int, int, bytes]]:
        """Stream the file as record-aligned blocks: (first row id, file offset, bytes).

        A block always ends on a record boundary, so a literal search over the
        block bytes can be mapped straight back to a row through the index.
        """
        offsets = self.index.offsets
        first = self.header_rows
        last = len(self.index)
        total = self.index.size
        with open(self.path, "rb") as fh:
            row = first
            while row < last:
                start = offsets[row]
                fh.seek(start)
                blob = fh.read(chunk)
                if not blob:
                    return
                end_limit = start + len(blob)
                # Largest row whose start is still inside the block; rows
                # [row, stop) are therefore complete records.
                stop = bisect_right(offsets, end_limit, row, last + 1) - 1
                if stop <= row:
                    # A single record longer than one chunk: read it whole.
                    rec_start, rec_end = self.index.span(row)
                    fh.seek(rec_start)
                    yield row - first, rec_start, fh.read(rec_end - rec_start)
                    row += 1
                else:
                    yield row - first, start, blob[: offsets[stop] - start]
                    row = stop
                if progress is not None and not progress(
                    min(offsets[min(row, last)], total), total
                ):
                    raise Aborted()

    def row_at_offset(self, byte_offset: int, lo: int = 0) -> int:
        """Row id of the record containing ``byte_offset`` (header excluded).

        ``lo`` is a row id known to be at or before the answer; passing the
        first row of the current block keeps the search short.
        """
        row = bisect_right(
            self.index.offsets, byte_offset, lo + self.header_rows, len(self.index) + 1
        ) - 1
        return row - self.header_rows

    def record_end(self, rid: int) -> int:
        return self.index.span(rid + self.header_rows)[1]

    def _stream_physical(self) -> Iterator[tuple[int, list[str]]]:
        with open(self.path, "rb") as fh:
            stream = io.TextIOWrapper(
                io.BufferedReader(fh, 1 << 20), encoding=self.dialect.encoding,
                errors="replace", newline="",
            )
            reader = csv.reader(
                stream, delimiter=self.dialect.delimiter, quotechar=self.dialect.quotechar
            )
            for i, raw in enumerate(reader):
                if i < self.header_rows:
                    continue
                yield i - self.header_rows, raw

    # ------------------------------------------------------------------ edits

    @property
    def dirty(self) -> bool:
        return bool(self.edits or self.new_rows or self.seq.dirty or self._columns_changed)

    @property
    def _columns_changed(self) -> bool:
        return [c.src for c in self.columns] != list(range(len(self.columns))) or any(
            c.name != n for c, n in zip(self.columns, self._original_names())
        )

    def _original_names(self) -> list[str]:
        return getattr(self, "_orig_names", [c.name for c in self.columns])

    def _push(self, op: Op, bulk: bool = False) -> None:
        self.undo_stack.append(op)
        if len(self.undo_stack) > self.MAX_UNDO:
            self.undo_stack.pop(0)
        if bulk:
            # Bulk snapshots are megabytes each; keep only the most recent few.
            bulk_ops = [i for i, o in enumerate(self.undo_stack) if o.kind == "bulk"]
            for index in bulk_ops[: -self.MAX_BULK_UNDO]:
                self.undo_stack.pop(index)
        self.redo_stack.clear()

    def _apply_cells(self, cells: list[tuple[int, int, str]]) -> None:
        for rid, cid, value in cells:
            store = self.new_rows if rid < 0 else self.edits
            store.setdefault(rid, {})[cid] = value

    def _drop_cells(self, cells: list[tuple[int, int]]) -> None:
        """Forget overlay entries again, so an undone edit leaves no trace."""
        for rid, cid in cells:
            store = self.new_rows if rid < 0 else self.edits
            row = store.get(rid)
            if row is None:
                continue
            row.pop(cid, None)
            if not row and rid >= 0:
                store.pop(rid, None)

    def get_cell(self, rid: int, cid: int) -> str:
        row = self.read_row(rid)
        for i, col in enumerate(self.columns):
            if col.id == cid:
                return row[i]
        return ""

    def set_cells(self, cells: list[tuple[int, int, str]], label: str = "edit") -> int:
        """Set (row id, column id, value) triples as one undoable operation."""
        before: list[tuple[int, int, str]] = []
        by_row: dict[int, list[tuple[int, int, str]]] = {}
        for rid, cid, value in cells:
            by_row.setdefault(rid, []).append((rid, cid, value))
        raws = self.read_physical_many([r for r in by_row if r >= 0])
        changed: list[tuple[int, int, str]] = []
        for rid, group in by_row.items():
            current = self.project(rid, raws.get(rid))
            pos = {col.id: i for i, col in enumerate(self.columns)}
            for _, cid, value in group:
                if cid not in pos:
                    continue
                old = current[pos[cid]]
                if old == value:
                    continue
                before.append((rid, cid, old))
                changed.append((rid, cid, value))
        if not changed:
            return 0
        fresh = [
            (rid, cid)
            for rid, cid, _ in changed
            if cid not in (self.new_rows if rid < 0 else self.edits).get(rid, {})
        ]
        self._apply_cells(changed)
        self._push(Op("cells", {"before": before, "after": changed, "fresh": fresh}, label))
        return len(changed)

    def insert_row(self, pos: int, values: Optional[list[str]] = None) -> int:
        anchor, offset = self.seq.anchor_for(pos)
        rid = self._new_row_id()
        data = {}
        if values:
            for col, value in zip(self.columns, values):
                if value:
                    data[col.id] = value
        self.new_rows[rid] = data
        self.seq.insert_after(anchor, rid, offset)
        if self.view is not None:
            at = min(max(pos, 0), len(self.view))
            self.view.insert(at, rid)
        self._push(Op("insert", {"rid": rid, "anchor": anchor, "offset": offset, "pos": pos}, "insert row"))
        return rid

    def delete_rows(self, ids: list[int]) -> int:
        removed: list[dict] = []
        for rid in ids:
            if rid < 0:
                anchor, offset = None, None
                for a, lst in self.seq.inserts.items():
                    if rid in lst:
                        anchor, offset = a, lst.index(rid)
                        break
                if anchor is None and rid in self.seq.head:
                    anchor, offset = HEAD_ANCHOR, self.seq.head.index(rid)
                if anchor is None:
                    continue
                data = self.new_rows.pop(rid, {})
                self.seq.delete(rid)
                removed.append({"rid": rid, "anchor": anchor, "offset": offset, "data": data})
            elif self.seq.delete(rid):
                removed.append({"rid": rid})
        if not removed:
            return 0
        for entry in removed:
            self.set_mark(entry["rid"], False)
        if self.view is not None:
            gone = {r["rid"] for r in removed}
            self.view = array("q", [r for r in self.view if r not in gone])
        self._push(Op("delete", {"rows": removed}, f"delete {len(removed)} row(s)"))
        return len(removed)

    # --------------------------------------------------------------- columns

    def _column_snapshot(self) -> list[Column]:
        return [Column(c.id, c.name, c.src) for c in self.columns]

    def _set_columns(self, columns: list[Column], label: str) -> None:
        before = self._column_snapshot()
        self.columns = columns
        self._push(Op("columns", {"before": before, "after": self._column_snapshot()}, label))

    def rename_column(self, cid: int, name: str) -> None:
        cols = self._column_snapshot()
        for col in cols:
            if col.id == cid:
                col.name = name
        self._set_columns(cols, "rename column")

    def add_column(self, name: str, at: Optional[int] = None) -> int:
        cols = self._column_snapshot()
        col = Column(self._new_col_id(), name, None)
        cols.insert(len(cols) if at is None else at, col)
        self._set_columns(cols, "add column")
        return col.id

    def delete_column(self, cid: int) -> None:
        cols = [c for c in self._column_snapshot() if c.id != cid]
        self._set_columns(cols, "delete column")

    def move_column(self, cid: int, to: int) -> None:
        cols = self._column_snapshot()
        idx = next((i for i, c in enumerate(cols) if c.id == cid), None)
        if idx is None:
            return
        col = cols.pop(idx)
        cols.insert(max(0, min(to, len(cols))), col)
        self._set_columns(cols, "move column")

    def column_index(self, cid: int) -> int:
        for i, col in enumerate(self.columns):
            if col.id == cid:
                return i
        return -1

    # ------------------------------------------------------------ bookmarks

    @property
    def mark_count(self) -> int:
        return self.marks.count + len(self.virtual_marks)

    def is_marked(self, rid: int) -> bool:
        return rid in self.virtual_marks if rid < 0 else self.marks.get(rid)

    def set_mark(self, rid: int, on: Optional[bool] = None) -> bool:
        """Set, clear or (when ``on`` is None) toggle one bookmark."""
        if rid < 0:
            state = rid in self.virtual_marks if on is None else not on
            if state:
                self.virtual_marks.discard(rid)
            else:
                self.virtual_marks.add(rid)
            return not state
        if on is None:
            return self.marks.toggle(rid)
        if on:
            self.marks.add(rid)
        else:
            self.marks.discard(rid)
        return bool(on)

    def mark_ids(self, ids: Iterable[int], on: bool = True) -> int:
        changed = 0
        for rid in ids:
            if rid < 0:
                if on and rid not in self.virtual_marks:
                    self.virtual_marks.add(rid)
                    changed += 1
                elif not on and rid in self.virtual_marks:
                    self.virtual_marks.discard(rid)
                    changed += 1
            elif on:
                changed += self.marks.add(rid)
            else:
                changed += self.marks.discard(rid)
        return changed

    def clear_marks(self) -> None:
        self.marks.clear()
        self.virtual_marks.clear()

    def invert_marks(self) -> None:
        """Bookmark exactly the rows that are not bookmarked (deleted rows stay out)."""
        self.marks.invert()
        if self.seq.alive is not None:
            self.marks.intersect_update(self.seq.alive)
        virtual = {rid for rid in self._virtual_ids()}
        self.virtual_marks = virtual - self.virtual_marks

    def _virtual_ids(self) -> Iterator[int]:
        yield from self.seq.head
        for ids in self.seq.inserts.values():
            yield from ids

    def iter_marked(self) -> Iterator[int]:
        """Bookmarked, still-visible row ids in display order."""
        if not self.seq.inserts and not self.seq.head:
            alive = self.seq.alive
            for rid in self.marks.iter_set():
                if alive is None or alive.get(rid):
                    yield rid
            return
        for rid in self.seq.iter_ids():
            if self.is_marked(rid):
                yield rid

    def marked_ids(self) -> array:
        return array("q", self.iter_marked())

    def find_mark(self, pos: int, forward: bool = True) -> int:
        """Display position of the next/previous bookmark, wrapping around."""
        if self.mark_count == 0:
            return -1
        if self.view is not None:
            total = len(self.view)
            for step in range(1, total + 1):
                probe = (pos + step) % total if forward else (pos - step) % total
                if self.is_marked(self.view[probe]):
                    return probe
            return -1
        try:
            current = self.seq.at(pos) if 0 <= pos < self.seq.count else -1
        except IndexError:
            current = -1
        if self.virtual_marks:  # inserted rows: fall back to a display-order walk
            ids = list(self.iter_marked())
            positions = sorted(p for p in (self.seq.position_of(r) for r in ids) if p >= 0)
            if not positions:
                return -1
            if forward:
                for p in positions:
                    if p > pos:
                        return p
                return positions[0]
            for p in reversed(positions):
                if p < pos:
                    return p
            return positions[-1]
        start = (current + 1) if current >= 0 else 0
        alive = self.seq.alive
        row = self.marks.next_set(start) if forward else self.marks.previous_set(max(current - 1, -1))
        seen = 0
        while row >= 0 and alive is not None and not alive.get(row) and seen < 64:
            seen += 1
            row = self.marks.next_set(row + 1) if forward else self.marks.previous_set(row - 1)
        if row < 0:
            return -1
        return self.seq.position_of(row)

    # --------------------------------------------------------- bulk deletes

    def _bulk_state(self) -> dict:
        return {
            "seq": self.seq.snapshot(),
            "marks": self.marks.snapshot(),
            "virtual_marks": set(self.virtual_marks),
        }

    def _restore_bulk(self, state: dict) -> None:
        self.seq.restore(state["seq"])
        self.marks.restore(state["marks"])
        self.virtual_marks = set(state["virtual_marks"])
        self._prune_view()

    def _prune_view(self) -> None:
        if self.view is None:
            return
        alive = self.seq.alive
        if alive is None:
            return
        visible = [r for r in self.view if r < 0 or alive.get(r)]
        self.view = array("q", visible)

    def delete_marked(self, keep: bool = False) -> int:
        """Delete bookmarked rows, or with ``keep`` the ones that are not.

        This is the bulk path: state is remembered as bitset snapshots, so
        deleting fifty million rows stays undoable without listing them.
        """
        before = self._bulk_state()
        if keep:
            removed = self.seq.keep_where(self.marks)
            kept_virtual = self.virtual_marks
            for rid in list(self._virtual_ids()):
                if rid not in kept_virtual:
                    self.seq.delete(rid)
                    self.new_rows.pop(rid, None)
        else:
            removed = self.seq.delete_where(self.marks)
            for rid in list(self.virtual_marks):
                if self.seq.delete(rid):
                    self.new_rows.pop(rid, None)
            self.virtual_marks.clear()
        if not keep:
            self.marks.clear()
        self._prune_view()
        label = "delete unmarked rows" if keep else "delete bookmarked rows"
        self._push(Op("bulk", {"before": before, "after": self._bulk_state()}, label), bulk=True)
        return removed

    # ------------------------------------------------------------ undo/redo

    def undo(self) -> Optional[str]:
        if not self.undo_stack:
            return None
        op = self.undo_stack.pop()
        self._invert(op, forward=False)
        self.redo_stack.append(op)
        return op.label

    def redo(self) -> Optional[str]:
        if not self.redo_stack:
            return None
        op = self.redo_stack.pop()
        self._invert(op, forward=True)
        self.undo_stack.append(op)
        return op.label

    def _invert(self, op: Op, forward: bool) -> None:
        if op.kind == "cells":
            if forward:
                self._apply_cells(op.payload["after"])
            else:
                self._apply_cells(op.payload["before"])
                self._drop_cells(op.payload.get("fresh", []))
        elif op.kind == "columns":
            self.columns = [
                Column(c.id, c.name, c.src)
                for c in op.payload["after" if forward else "before"]
            ]
        elif op.kind == "insert":
            rid = op.payload["rid"]
            if forward:
                self.new_rows.setdefault(rid, op.payload.get("data", {}))
                self.seq.insert_after(op.payload["anchor"], rid, op.payload["offset"])
                if self.view is not None:
                    self.view.insert(min(op.payload["pos"], len(self.view)), rid)
            else:
                op.payload["data"] = self.new_rows.pop(rid, {})
                self.seq.delete(rid)
                if self.view is not None:
                    self.view = array("q", [r for r in self.view if r != rid])
        elif op.kind == "bulk":
            self._restore_bulk(op.payload["after" if forward else "before"])
        elif op.kind == "delete":
            rows = op.payload["rows"]
            if forward:
                for entry in rows:
                    rid = entry["rid"]
                    if rid < 0:
                        self.new_rows.pop(rid, None)
                    self.seq.delete(rid)
                if self.view is not None:
                    gone = {r["rid"] for r in rows}
                    self.view = array("q", [r for r in self.view if r not in gone])
            else:
                for entry in reversed(rows):
                    rid = entry["rid"]
                    if rid < 0:
                        self.new_rows[rid] = entry.get("data", {})
                        self.seq.insert_after(entry["anchor"], rid, entry["offset"])
                    else:
                        self.seq.undelete(rid)
                self.view = None
                self.view_label = ""

    # ------------------------------------------------------------------ save

    def rows_for_output(self, ids: Optional[Iterable[int]] = None) -> Iterator[list[str]]:
        for _rid, row in self.iter_all(ids=ids):
            yield row

    def write_to(
        self,
        out_path: str,
        ids: Optional[Iterable[int]] = None,
        include_header: bool = True,
        delimiter: Optional[str] = None,
        encoding: Optional[str] = None,
        newline: Optional[str] = None,
        progress: Optional[Callable[[int, int], bool]] = None,
    ) -> int:
        delimiter = delimiter or self.dialect.delimiter
        encoding = encoding or self.dialect.encoding
        newline = newline or self.dialect.newline
        tmp = out_path + ".csvopt.tmp"
        self._check_free_space(out_path)
        written = 0
        total = self.row_count
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            writer = csv.writer(
                fh,
                delimiter=delimiter,
                quotechar=self.dialect.quotechar,
                lineterminator=newline,
            )
            if include_header and self.dialect.has_header:
                writer.writerow([c.name for c in self.columns])
            for _rid, row in self.iter_all(ids=ids, progress=progress):
                writer.writerow(row)
                written += 1
        try:
            os.replace(tmp, out_path)
        except OSError as exc:  # Windows refuses this while the file is open elsewhere
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise SaveError(
                f"{out_path} could not be replaced ({exc}). "
                "On Windows this usually means the file is still open in Excel "
                "or another program - close it and save again."
            ) from exc
        return written

    def _check_free_space(self, out_path: str) -> None:
        """Refuse to start writing a file the disk cannot hold."""
        try:
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(out_path)) or ".").free
        except OSError:
            return
        needed = int(self.index.size * 1.05) + (1 << 20)
        if free < needed:
            raise SaveError(
                f"저장할 공간이 부족합니다. 약 {needed / 2**30:.1f} GB가 필요하지만 "
                f"{free / 2**30:.1f} GB만 남아 있습니다."
            )

    def save(self, backup: bool = True, progress: Optional[Callable[[int, int], bool]] = None) -> int:
        if backup:
            try:
                shutil.copy2(self.path, self.path + ".bak")
            except OSError:
                pass
        written = self.write_to(self.path, progress=progress)
        self.reload()
        return written

    def save_as(
        self,
        path: str,
        view_only: bool = False,
        delimiter: Optional[str] = None,
        encoding: Optional[str] = None,
        newline: Optional[str] = None,
        progress=None,
    ) -> int:
        ids = list(self.view) if (view_only and self.view is not None) else None
        return self.write_to(
            path,
            ids=ids,
            delimiter=delimiter,
            encoding=encoding,
            newline=newline,
            progress=progress,
        )

    def reload(self) -> None:
        """Re-index the file on disk and drop the overlay (used after saving)."""
        self.index.close()  # release the mapping before the stale cache is removed
        drop_cached_index(self.path)
        self.index = build_index(
            self.path,
            quotechar=self.dialect.quotechar,
            delimiter=self.dialect.delimiter,
            use_cache=True,
        )
        self.base_count = max(0, len(self.index) - self.header_rows)
        self.seq = RowSequence(self.base_count)
        self.marks = Bitset(self.base_count)
        self.virtual_marks.clear()
        self.edits.clear()
        self.new_rows.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.view = None
        self.view_label = ""
        for i, col in enumerate(self.columns):
            col.src = i
        self._orig_names = [c.name for c in self.columns]
