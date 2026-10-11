"""Byte-offset indexing and format sniffing for large delimited files.

The index is what makes random access into a multi-gigabyte log possible: we
scan the file once, remember where every record starts, and afterwards any row
can be read with a single seek.  The scan is quote-aware, so records that embed
newlines inside quoted fields stay a single row.
"""

from __future__ import annotations

import csv
import hashlib
import mmap
import os
import struct
import tempfile
from array import array
from dataclasses import dataclass
from itertools import accumulate, islice
from typing import Callable, Iterable, Optional

CHUNK = 4 << 20
INDEX_MAGIC = b"CSVOPTIX2"
INDEX_SUFFIX = ".csvidx"
# The offset table starts on a 64 KiB boundary so it can be mmapped directly on
# Windows too, where the mapping offset must be a multiple of the allocation
# granularity.
HEADER_SIZE = 1 << 16
# Offsets buffered in memory before the index starts streaming to disk.
SPILL_ROWS = 1 << 20

# Order matters: the first encoding that decodes the sample cleanly wins.
# cp949/cp932 cover the Korean and Japanese logs that Windows tools still emit.
ENCODING_CANDIDATES = ("utf-8", "cp949", "cp932", "cp1252", "latin-1")
BOM_ENCODINGS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
DELIMITER_CANDIDATES = (",", "\t", ";", "|")


@dataclass
class Dialect:
    encoding: str
    delimiter: str
    quotechar: str = '"'
    has_header: bool = True
    newline: str = os.linesep  # line terminator used when writing the file back

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "quotechar": self.quotechar,
            "has_header": self.has_header,
            "newline": "crlf" if self.newline == "\r\n" else "lf",
        }


def _decode_sample(data: bytes) -> tuple[str, str]:
    # A byte order mark is authoritative; without one, never guess an encoding
    # that would *add* a BOM when the file is written back.
    for bom, enc in BOM_ENCODINGS:
        if data.startswith(bom):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                break
    for enc in ENCODING_CANDIDATES:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), "latin-1"


def sniff(path: str, sample_size: int = 256 * 1024) -> Dialect:
    """Guess encoding / delimiter / header presence from the head of a file."""
    with open(path, "rb") as fh:
        raw = fh.read(sample_size)
    # Never cut a multi-byte character in half when the file is bigger.
    text, encoding = _decode_sample(raw)
    if len(raw) == sample_size:
        text = text[: text.rfind("\n") + 1] or text

    delimiter = _sniff_delimiter(text)
    has_header = _sniff_header(text, delimiter)
    # Preserve the file's own line endings so a CSV written on Windows stays
    # CRLF and one written on macOS stays LF, whichever machine edits it.
    if b"\r\n" in raw:
        newline = "\r\n"
    elif b"\n" in raw:
        newline = "\n"
    else:
        newline = "\r\n" if os.name == "nt" else "\n"
    return Dialect(
        encoding=encoding, delimiter=delimiter, has_header=has_header, newline=newline
    )


def _sniff_delimiter(text: str) -> str:
    head = "\n".join(text.splitlines()[:20])
    if not head.strip():
        return ","
    try:
        return csv.Sniffer().sniff(head, delimiters="".join(DELIMITER_CANDIDATES)).delimiter
    except csv.Error:
        pass
    # Fall back to "the candidate that appears equally often on every line".
    best, best_score = ",", -1.0
    lines = [ln for ln in head.splitlines() if ln.strip()][:20]
    for cand in DELIMITER_CANDIDATES:
        counts = [ln.count(cand) for ln in lines]
        if not counts or max(counts) == 0:
            continue
        avg = sum(counts) / len(counts)
        spread = max(counts) - min(counts)
        score = avg - spread * 2
        if score > best_score:
            best, best_score = cand, score
    return best


def _sniff_header(text: str, delimiter: str) -> bool:
    rows = list(csv.reader(text.splitlines()[:50], delimiter=delimiter))
    rows = [r for r in rows if r]
    if len(rows) < 2:
        return bool(rows)
    head, body = rows[0], rows[1:]
    if any(not c.strip() for c in head):
        return False
    if len(set(head)) != len(head):
        return False

    def numeric_ratio(row: Iterable[str]) -> float:
        cells = [c for c in row if c.strip()]
        if not cells:
            return 0.0
        num = 0
        for c in cells:
            try:
                float(c.replace(",", ""))
                num += 1
            except ValueError:
                pass
        return num / len(cells)

    body_numeric = sum(numeric_ratio(r) for r in body) / len(body)
    return numeric_ratio(head) < 0.3 <= body_numeric or numeric_ratio(head) == 0.0


class RowIndex:
    """Byte offsets of every physical record in a file.

    ``offsets`` is either an ``array('q')`` held in memory (small files) or a
    memoryview over a memory-mapped index file (large ones).  Both index in
    O(1); the mapped variant keeps resident memory flat, which is what makes a
    10 GB log with 100M+ rows practical on an ordinary laptop.
    """

    def __init__(self, offsets, size: int, mtime_ns: int, mm=None, path: Optional[str] = None):
        self.offsets = offsets  # len == record count + 1 (last entry == EOF)
        self.size = size
        self.mtime_ns = mtime_ns
        self._mm = mm
        self.cache_path = path

    def __len__(self) -> int:
        return max(0, len(self.offsets) - 1)

    @property
    def count(self) -> int:
        return len(self)

    @property
    def mapped(self) -> bool:
        return self._mm is not None

    @property
    def memory_bytes(self) -> int:
        """Resident bytes used by the offset table (0 when memory-mapped)."""
        return 0 if self._mm is not None else len(self.offsets) * 8

    def span(self, row: int) -> tuple[int, int]:
        return self.offsets[row], self.offsets[row + 1]

    def start(self, row: int) -> int:
        return self.offsets[row]

    def close(self) -> None:
        """Release the mapping (required on Windows before the file can go)."""
        if self._mm is not None:
            try:
                if isinstance(self.offsets, memoryview):
                    self.offsets.release()
            finally:
                self._mm.close()
                self._mm = None
                self.offsets = array("q", [0])

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _map_offsets(path: str, count: int):
    """Memory-map ``count`` int64 offsets stored after the header of ``path``."""
    fh = open(path, "rb")
    try:
        mm = mmap.mmap(fh.fileno(), count * 8, access=mmap.ACCESS_READ, offset=HEADER_SIZE)
    finally:
        fh.close()  # the mapping keeps its own reference to the file
    return memoryview(mm).cast("q"), mm


def _write_header(fh, size: int, mtime_ns: int, count: int) -> None:
    fh.seek(0)
    fh.write(INDEX_MAGIC)
    fh.write(struct.pack("<qqq", size, mtime_ns, count))
    fh.write(b"\0" * (HEADER_SIZE - len(INDEX_MAGIC) - 24))


class _OffsetWriter:
    """Accumulates offsets, spilling to an index file once there are many.

    Small files never touch the disk; big ones stream their offsets out as they
    are found, so peak memory stays at the size of one buffer no matter how many
    rows the file has.
    """

    def __init__(self, cache_path: Optional[str], spill_rows: int = SPILL_ROWS):
        self.cache_path = cache_path
        self.spill_rows = spill_rows
        self.buf = array("q")
        self.fh = None
        self.tmp_path: Optional[str] = None
        self.spilled = 0
        self.last_value: Optional[int] = None

    def append(self, value: int) -> None:
        self.buf.append(value)
        self.last_value = value
        if len(self.buf) >= self.spill_rows:
            self._flush()

    def extend(self, values) -> None:
        self.buf.extend(values)
        if self.buf:
            self.last_value = self.buf[-1]
        if len(self.buf) >= self.spill_rows:
            self._flush()

    @property
    def count(self) -> int:
        return self.spilled + len(self.buf)

    def last(self) -> Optional[int]:
        return self.last_value

    def _flush(self) -> None:
        if not self.buf:
            return
        if self.fh is None and not self._open():
            # Nowhere to spill (a read-only location): stay in memory instead.
            self.spill_rows = 1 << 62
            return
        self.buf.tofile(self.fh)
        self.spilled += len(self.buf)
        self.buf = array("q")

    def _open(self) -> bool:
        if not self.cache_path:
            return False
        self.tmp_path = self.cache_path + ".tmp"
        try:
            self.fh = open(self.tmp_path, "wb+")
            self.fh.write(b"\0" * HEADER_SIZE)  # patched with real values later
        except OSError:
            self.fh = None
            self.tmp_path = None
            return False
        return True

    def finish(self, size: int, mtime_ns: int) -> RowIndex:
        count = self.count
        if self.fh is None and self.cache_path:
            self._open()  # persist even small indexes, so reopening is instant
        if self.fh is None:
            return RowIndex(self.buf, size, mtime_ns)
        self._flush()
        _write_header(self.fh, size, mtime_ns, count)
        self.fh.flush()
        self.fh.close()
        self.fh = None
        os.replace(self.tmp_path, self.cache_path)
        self.tmp_path = None
        offsets, mm = _map_offsets(self.cache_path, count)
        return RowIndex(offsets, size, mtime_ns, mm=mm, path=self.cache_path)

    def abort(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        if self.tmp_path:
            try:
                os.unlink(self.tmp_path)
            except OSError:
                pass
            self.tmp_path = None


def scan_offsets(
    path: str,
    quotechar: str = '"',
    delimiter: str = ",",
    progress: Optional[Callable[[int, int], bool]] = None,
    cache_path: Optional[str] = None,
) -> RowIndex:
    """Scan a file and return the byte offset of every record start.

    The scan follows RFC 4180 closely enough for real log data: a quote only
    opens a quoted field when it starts one (right after a delimiter or a line
    break), a doubled quote inside a quoted field is an escape, and stray quotes
    in the middle of an unquoted field -- ``12" pipe`` -- are literal.  Newlines
    inside a quoted field therefore do not split a record.

    Between quotes the bytes are split in one C-level call rather than searched
    newline by newline, which roughly doubles throughput on quote-light logs.

    ``progress(done_bytes, total_bytes)`` may return ``False`` to abort, in
    which case :class:`Aborted` is raised.
    """
    st = os.stat(path)
    total = st.st_size
    quote = quotechar.encode("utf-8") if quotechar else b""
    openers = {b"\n", b"\r", b"", delimiter.encode("utf-8")}
    writer = _OffsetWriter(cache_path)
    writer.append(0)

    in_quote = False
    escape_pending = False  # a quote closed on the very last byte of a chunk
    prev_last = b""         # last byte of the previous chunk
    pos = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                n = len(chunk)
                i = 0
                if escape_pending:
                    escape_pending = False
                    if chunk[0:1] == quote:
                        in_quote = True
                        i = 1
                while i < n:
                    if in_quote:
                        j = chunk.find(quote, i) if quote else -1
                        if j < 0:
                            break
                        if j + 1 < n:
                            if chunk[j + 1 : j + 2] == quote:
                                i = j + 2  # escaped quote, still inside the field
                                continue
                            in_quote = False
                            i = j + 1
                            continue
                        in_quote = False
                        escape_pending = True
                        break
                    q = chunk.find(quote, i) if quote else -1
                    segment = chunk[i : n if q < 0 else q]
                    if b"\n" in segment:
                        lines = segment.split(b"\n")
                        writer.extend(
                            islice(
                                accumulate(
                                    (len(part) + 1 for part in lines[:-1]), initial=pos + i
                                ),
                                1,
                                None,
                            )
                        )
                    if q < 0:
                        break
                    prev = chunk[q - 1 : q] if q > 0 else prev_last
                    if prev in openers:
                        in_quote = True
                    i = q + 1
                pos += n
                prev_last = chunk[-1:]
                if progress is not None and not progress(pos, total):
                    raise Aborted()

        # ``writer`` now holds a start offset per record; append the EOF sentinel
        # so every record has a well defined end.  A file ending in a newline
        # already has its last offset sitting exactly at EOF.
        if total == 0:
            writer.abort()
            return RowIndex(array("q", [0]), 0, st.st_mtime_ns)
        if writer.last() != total:
            writer.append(total)
        return writer.finish(total, st.st_mtime_ns)
    except BaseException:
        writer.abort()
        raise


class Aborted(Exception):
    """Raised when a long-running scan is cancelled by its caller."""


def index_cache_path(path: str) -> str:
    return path + INDEX_SUFFIX


def _fallback_cache_path(path: str) -> str:
    """Index location for sources whose own directory is not writable."""
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"csvopt-{digest}{INDEX_SUFFIX}")


def cache_candidates(path: str) -> list[str]:
    return [index_cache_path(path), _fallback_cache_path(path)]


def load_cached_index(path: str) -> Optional[RowIndex]:
    """Map a previously written index if it still matches the file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    for cache in cache_candidates(path):
        try:
            with open(cache, "rb") as fh:
                head = fh.read(len(INDEX_MAGIC) + 24)
                if len(head) < len(INDEX_MAGIC) + 24 or head[: len(INDEX_MAGIC)] != INDEX_MAGIC:
                    continue
                size, mtime_ns, count = struct.unpack("<qqq", head[len(INDEX_MAGIC):])
            if size != st.st_size or mtime_ns != st.st_mtime_ns or count <= 0:
                continue
            if os.path.getsize(cache) < HEADER_SIZE + count * 8:
                continue
            offsets, mm = _map_offsets(cache, count)
            return RowIndex(offsets, st.st_size, st.st_mtime_ns, mm=mm, path=cache)
        except (OSError, ValueError):
            continue
    return None


def drop_cached_index(path: str) -> None:
    for cache in cache_candidates(path):
        try:
            os.unlink(cache)
        except OSError:
            pass


CACHE_MIN_BYTES = 8 * 1024 * 1024


def _writable_cache_path(path: str) -> Optional[str]:
    """First candidate index location we can actually create a file in."""
    for cache in cache_candidates(path):
        directory = os.path.dirname(cache) or "."
        if os.access(directory, os.W_OK):
            return cache
    return None


def build_index(
    path: str,
    quotechar: str = '"',
    delimiter: str = ",",
    use_cache: bool = True,
    progress: Optional[Callable[[int, int], bool]] = None,
) -> RowIndex:
    """Index ``path``, reusing a cached index when the file has not changed."""
    if use_cache:
        cached = load_cached_index(path)
        if cached is not None:
            return cached
    cache_path = None
    if use_cache and os.path.getsize(path) > CACHE_MIN_BYTES:
        cache_path = _writable_cache_path(path)
    return scan_offsets(
        path,
        quotechar=quotechar,
        delimiter=delimiter,
        progress=progress,
        cache_path=cache_path,
    )
