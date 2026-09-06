import os
import tempfile
import unittest

from csvopt.index import (
    build_index,
    index_cache_path,
    load_cached_index,
    scan_offsets,
    sniff,
    store_cached_index,
)


def write(data, suffix=".csv"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def records(path, idx):
    out = []
    with open(path, "rb") as fh:
        for i in range(len(idx)):
            start, end = idx.span(i)
            fh.seek(start)
            out.append(fh.read(end - start))
    return out


class ScanTest(unittest.TestCase):
    def tearDown(self):
        for path in getattr(self, "_paths", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    def scan(self, data, **kw):
        path = write(data)
        self._paths = getattr(self, "_paths", []) + [path]
        return path, scan_offsets(path, **kw)

    def test_simple_lines(self):
        path, idx = self.scan(b"a,b\n1,2\n3,4\n")
        self.assertEqual(len(idx), 3)
        self.assertEqual(records(path, idx)[-1], b"3,4\n")

    def test_missing_trailing_newline(self):
        path, idx = self.scan(b"a,b\n1,2")
        self.assertEqual(len(idx), 2)
        self.assertEqual(records(path, idx)[-1], b"1,2")

    def test_quoted_newline_stays_one_record(self):
        path, idx = self.scan(b'a,b\n"x\ny",2\n3,4\n')
        self.assertEqual(len(idx), 3)
        self.assertEqual(records(path, idx)[1], b'"x\ny",2\n')

    def test_escaped_quotes(self):
        path, idx = self.scan(b'a,b\n"say ""hi""\nagain",2\n3,4\n')
        self.assertEqual(len(idx), 3)

    def test_stray_quote_is_literal(self):
        path, idx = self.scan(b'a,b\n12" pipe,2\n3,4\n')
        self.assertEqual(len(idx), 3)

    def test_empty_file(self):
        _path, idx = self.scan(b"")
        self.assertEqual(len(idx), 0)

    def test_crlf(self):
        path, idx = self.scan(b"a,b\r\n1,2\r\n")
        self.assertEqual(len(idx), 2)
        self.assertEqual(records(path, idx)[1], b"1,2\r\n")

    def test_chunk_boundaries(self):
        # A record that straddles many chunk boundaries, with quoting in play.
        body = b'"' + b"x" * (5 << 20) + b'\n",1\n'
        path, idx = self.scan(b"a,b\n" + body + b"2,3\n")
        self.assertEqual(len(idx), 3)
        self.assertEqual(records(path, idx)[2], b"2,3\n")

    def test_progress_abort(self):
        path = write(b"a,b\n" + b"1,2\n" * 500000)
        self._paths = getattr(self, "_paths", []) + [path]
        from csvopt.index import Aborted

        with self.assertRaises(Aborted):
            scan_offsets(path, progress=lambda done, total: False)


class SniffTest(unittest.TestCase):
    def check(self, data, **expected):
        path = write(data)
        try:
            dialect = sniff(path)
            for key, value in expected.items():
                self.assertEqual(getattr(dialect, key), value, f"{key} of {data[:40]!r}")
        finally:
            os.unlink(path)

    def test_comma_utf8(self):
        self.check(b"name,age\nfoo,3\nbar,4\n", delimiter=",", encoding="utf-8", has_header=True)

    def test_tab(self):
        self.check(b"name\tage\nfoo\t3\nbar\t4\n", delimiter="\t")

    def test_semicolon(self):
        self.check(b"name;age\nfoo;3\nbar;4\n", delimiter=";")

    def test_headerless(self):
        self.check(b"1,2,3\n4,5,6\n7,8,9\n", has_header=False)

    def test_cp949(self):
        self.check("이름,나이\n홍길동,30\n".encode("cp949"), encoding="cp949")

    def test_bom_is_preserved(self):
        self.check("a,b\n1,2\n".encode("utf-8-sig"), encoding="utf-8-sig")

    def test_newline_detection(self):
        self.check(b"a,b\r\n1,2\r\n", newline="\r\n")
        self.check(b"a,b\n1,2\n", newline="\n")


class CacheTest(unittest.TestCase):
    def test_cache_round_trip_and_invalidation(self):
        path = write(b"a,b\n" + b"1,2\n" * 1000)
        try:
            idx = build_index(path, use_cache=False)
            self.assertTrue(store_cached_index(path, idx))
            cached = load_cached_index(path)
            self.assertIsNotNone(cached)
            self.assertEqual(len(cached), len(idx))

            with open(path, "ab") as fh:  # file changed -> cache must be ignored
                fh.write(b"9,9\n")
            self.assertIsNone(load_cached_index(path))
        finally:
            for p in (path, index_cache_path(path)):
                try:
                    os.unlink(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
