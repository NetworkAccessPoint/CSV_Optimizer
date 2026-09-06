"""Byte-offset indexing and format sniffing for large delimited files.

The index is what makes random access into a multi-gigabyte log possible: we
scan the file once, remember where every record starts, and afterwards any row
can be read with a single seek.  The scan is quote-aware, so records that embed
newlines inside quoted fields stay a single row.
"""

from __future__ import annotations

import csv
import os
import struct
from array import array
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

CHUNK = 1 << 20
INDEX_MAGIC = b"CSVOPTIX1"
INDEX_SUFFIX = ".csvidx"

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
    """Byte offsets of every physical record in a file."""

    def __init__(self, offsets: array, size: int, mtime_ns: int):
        self.offsets = offsets  # len == record count + 1 (last entry == EOF)
        self.size = size
        self.mtime_ns = mtime_ns

    def __len__(self) -> int:
        return max(0, len(self.offsets) - 1)

    @property
    def count(self) -> int:
        return len(self)

    def span(self, row: int) -> tuple[int, int]:
        return self.offsets[row], self.offsets[row + 1]

    def start(self, row: int) -> int:
        return self.offsets[row]


def scan_offsets(
    path: str,
    quotechar: str = '"',
    delimiter: str = ",",
    progress: Optional[Callable[[int, int], bool]] = None,
) -> RowIndex:
    """Scan a file and return the byte offset of every record start.

    The scan follows RFC 4180 quoting closely enough for real log data: a quote
    only opens a quoted field when it starts one (right after a delimiter or a
    line break), a doubled quote inside a quoted field is an escape, and stray
    quotes in the middle of an unquoted field -- ``12" pipe`` -- are literal.
    Newlines inside a quoted field therefore do not split a record.

    ``progress(done_bytes, total_bytes)`` may return ``False`` to abort, in
    which case :class:`Aborted` is raised.
    """
    st = os.stat(path)
    total = st.st_size
    quote = quotechar.encode("utf-8") if quotechar else b""
    openers = {b"\n", b"\r", b"", delimiter.encode("utf-8")}
    offsets = array("q")
    offsets.append(0)

    in_quote = False
    escape_pending = False  # a quote closed on the very last byte of a chunk
    prev_last = b""         # last byte of the previous chunk
    pos = 0
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
                nl = chunk.find(b"\n", i)
                q = chunk.find(quote, i) if quote else -1
                if q >= 0 and (nl < 0 or q < nl):
                    prev = chunk[q - 1 : q] if q > 0 else prev_last
                    if prev in openers:
                        in_quote = True
                    i = q + 1
                    continue
                if nl < 0:
                    break
                offsets.append(pos + nl + 1)
                i = nl + 1
            pos += n
            prev_last = chunk[-1:]
            if progress is not None and not progress(pos, total):
                raise Aborted()

    # ``offsets`` now holds a start offset per record; append the EOF sentinel
    # so that every record has a well defined end.  A file ending in a newline
    # already has its last appended offset sitting exactly at EOF.
    if total == 0:
        return RowIndex(array("q", [0]), 0, st.st_mtime_ns)
    if offsets[-1] != total:
        offsets.append(total)
    return RowIndex(offsets, total, st.st_mtime_ns)


class Aborted(Exception):
    """Raised when a long-running scan is cancelled by its caller."""


def index_cache_path(path: str) -> str:
    return path + INDEX_SUFFIX


def load_cached_index(path: str) -> Optional[RowIndex]:
    cache = index_cache_path(path)
    try:
        st = os.stat(path)
        with open(cache, "rb") as fh:
            head = fh.read(len(INDEX_MAGIC) + 24)
            if len(head) < len(INDEX_MAGIC) + 24 or head[: len(INDEX_MAGIC)] != INDEX_MAGIC:
                return None
            size, mtime_ns, count = struct.unpack("<qqq", head[len(INDEX_MAGIC):])
            if size != st.st_size or mtime_ns != st.st_mtime_ns:
                return None
            offsets = array("q")
            offsets.fromfile(fh, count)
    except (OSError, EOFError, ValueError):
        return None
    return RowIndex(offsets, st.st_size, st.st_mtime_ns)


def store_cached_index(path: str, idx: RowIndex) -> bool:
    cache = index_cache_path(path)
    tmp = cache + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(INDEX_MAGIC)
            fh.write(struct.pack("<qqq", idx.size, idx.mtime_ns, len(idx.offsets)))
            idx.offsets.tofile(fh)
        os.replace(tmp, cache)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def build_index(
    path: str,
    quotechar: str = '"',
    delimiter: str = ",",
    use_cache: bool = True,
    progress: Optional[Callable[[int, int], bool]] = None,
) -> RowIndex:
    if use_cache:
        cached = load_cached_index(path)
        if cached is not None:
            return cached
    idx = scan_offsets(path, quotechar=quotechar, delimiter=delimiter, progress=progress)
    if use_cache and idx.size > 8 * 1024 * 1024:
        store_cached_index(path, idx)
    return idx
