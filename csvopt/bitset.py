"""A compact bitset over row numbers.

One bit per row keeps bookmarks and deletions affordable on files with tens of
millions of rows: 60M rows cost 7.5 MB here, where a Python ``set`` of the same
row numbers would cost gigabytes.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional

POPCOUNT = bytes(bin(i).count("1") for i in range(256))


class Bitset:
    def __init__(self, size: int, value: bool = False):
        self.size = max(0, size)
        self.bits = bytearray(b"\xff" if value else b"\x00") * ((self.size + 7) // 8)
        self._count = self.size if value else 0
        if value:
            self._clear_tail()

    # ------------------------------------------------------------- internals

    def _clear_tail(self) -> None:
        """Zero the padding bits after the last row so counts stay exact."""
        spare = len(self.bits) * 8 - self.size
        if spare and self.bits:
            self.bits[-1] &= (0xFF >> spare) & 0xFF

    def __len__(self) -> int:
        return self.size

    @property
    def count(self) -> int:
        return self._count

    def __contains__(self, row: int) -> bool:
        return self.get(row)

    def get(self, row: int) -> bool:
        if row < 0 or row >= self.size:
            return False
        return bool(self.bits[row >> 3] >> (row & 7) & 1)

    def add(self, row: int) -> bool:
        """Set a bit; returns True when it changed."""
        if row < 0 or row >= self.size or self.get(row):
            return False
        self.bits[row >> 3] |= 1 << (row & 7)
        self._count += 1
        return True

    def discard(self, row: int) -> bool:
        if row < 0 or row >= self.size or not self.get(row):
            return False
        self.bits[row >> 3] &= ~(1 << (row & 7)) & 0xFF
        self._count -= 1
        return True

    def toggle(self, row: int) -> bool:
        """Flip a bit; returns its new value."""
        if self.get(row):
            self.discard(row)
            return False
        self.add(row)
        return True

    def update(self, rows: Iterable[int]) -> int:
        added = 0
        for row in rows:
            added += self.add(row)
        return added

    def clear(self) -> None:
        self.bits = bytearray(len(self.bits))
        self._count = 0

    def invert(self) -> None:
        """Flip every bit (rows beyond ``size`` stay unset)."""
        if self.bits:
            mask = (1 << (len(self.bits) * 8)) - 1
            flipped = int.from_bytes(bytes(self.bits), "little") ^ mask
            self.bits = bytearray(flipped.to_bytes(len(self.bits), "little"))
            self._clear_tail()
        self._count = self.size - self._count

    def copy(self) -> "Bitset":
        clone = Bitset(0)
        clone.size = self.size
        clone.bits = bytearray(self.bits)
        clone._count = self._count
        return clone

    def snapshot(self) -> bytes:
        return bytes(self.bits)

    def restore(self, data: bytes) -> None:
        self.bits = bytearray(data)
        self._clear_tail()
        self._count = self.recount()

    def recount(self) -> int:
        return sum(POPCOUNT[b] for b in self.bits)

    # -------------------------------------------------------------- scanning

    def iter_set(self, start: int = 0, stop: Optional[int] = None) -> Iterator[int]:
        """Row numbers whose bit is set, in ascending order.

        Whole empty bytes are skipped, so sparse bookmarks over a huge file are
        cheap to walk.
        """
        stop = self.size if stop is None else min(stop, self.size)
        row = max(0, start)
        bits = self.bits
        while row < stop:
            byte_index = row >> 3
            byte = bits[byte_index]
            if byte == 0:
                row = (byte_index + 1) << 3
                continue
            bit = row & 7
            while bit < 8 and row < stop:
                if byte >> bit & 1:
                    yield row
                bit += 1
                row += 1
        return

    def next_set(self, row: int, wrap: bool = True) -> int:
        """First set row at or after ``row`` (-1 when the set is empty)."""
        for found in self.iter_set(row):
            return found
        if wrap:
            for found in self.iter_set(0):
                return found
        return -1

    def previous_set(self, row: int, wrap: bool = True) -> int:
        """Last set row at or before ``row`` (-1 when the set is empty)."""
        found = -1
        for candidate in self.iter_set(0, row + 1):
            found = candidate
        if found < 0 and wrap:
            for candidate in self.iter_set(0):
                found = candidate
        return found

    def count_between(self, start: int, stop: int) -> int:
        """Set bits in ``[start, stop)``; ``start`` must be byte-aligned."""
        start = max(0, min(start, self.size))
        stop = max(start, min(stop, self.size))
        lo, hi = start >> 3, stop >> 3
        total = sum(POPCOUNT[b] for b in self.bits[lo:hi])
        rest = stop & 7
        if rest and hi < len(self.bits):
            total += POPCOUNT[self.bits[hi] & ((1 << rest) - 1)]
        return total

    def count_before(self, row: int) -> int:
        """How many set bits are strictly before ``row``."""
        row = max(0, min(row, self.size))
        whole = row >> 3
        total = sum(POPCOUNT[b] for b in self.bits[:whole])
        rest = row & 7
        if rest:
            total += POPCOUNT[self.bits[whole] & ((1 << rest) - 1)]
        return total

    # ------------------------------------------------------------ set algebra

    def _as_int(self) -> int:
        return int.from_bytes(bytes(self.bits), "little")

    def _set_int(self, value: int) -> None:
        self.bits = bytearray(value.to_bytes(len(self.bits), "little"))
        self._clear_tail()
        self._count = self.recount()

    def intersect_update(self, other: "Bitset") -> None:
        """Keep only bits set in both sets."""
        self._set_int(self._as_int() & other._as_int())

    def difference_update(self, other: "Bitset") -> None:
        """Clear every bit that is set in ``other``."""
        self._set_int(self._as_int() & ~other._as_int())

    def union_update(self, other: "Bitset") -> None:
        self._set_int(self._as_int() | other._as_int())
