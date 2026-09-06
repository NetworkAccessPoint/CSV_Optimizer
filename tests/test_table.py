import os
import tempfile
import unittest

from csvopt.table import Fenwick, RowSequence, Table

SAMPLE = "ts,level,msg\n1,INFO,hello\n2,ERROR,boom\n3,WARN,\"multi\nline\"\n4,INFO,tail\n"


class TempFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sample.csv")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(SAMPLE)

    def tearDown(self):
        self.dir.cleanup()

    def table(self, **kw):
        return Table(self.path, use_cache=False, **kw)

    def rows(self, table):
        return table.read_range(0, 1000)[1]


class FenwickTest(unittest.TestCase):
    def test_prefix_and_find(self):
        fen = Fenwick(10, 1)
        self.assertEqual(fen.total, 10)
        self.assertEqual(fen.prefix(4), 4)
        self.assertEqual(fen.find(0), (0, 0))
        self.assertEqual(fen.find(7), (7, 0))
        fen.add(3, -1)
        self.assertEqual(fen.total, 9)
        self.assertEqual(fen.find(3), (4, 0))
        fen.add(5, 2)  # row 5 now occupies three slots
        self.assertEqual(fen.find(4), (5, 0))
        self.assertEqual(fen.find(6), (5, 2))
        self.assertEqual(fen.find(7), (6, 0))


class RowSequenceTest(unittest.TestCase):
    def test_identity_until_edited(self):
        seq = RowSequence(5)
        self.assertFalse(seq.dirty)
        self.assertEqual(seq.slice(1, 3), [1, 2, 3])
        self.assertEqual(list(seq.iter_ids()), [0, 1, 2, 3, 4])

    def test_delete_and_insert(self):
        seq = RowSequence(5)
        seq.delete(1)
        self.assertEqual(list(seq.iter_ids()), [0, 2, 3, 4])
        self.assertEqual(seq.count, 4)
        seq.insert_after(0, -2)
        self.assertEqual(list(seq.iter_ids()), [0, -2, 2, 3, 4])
        self.assertEqual(seq.at(1), -2)
        self.assertEqual(seq.position_of(-2), 1)
        seq.insert_after(-1, -3)  # head anchor
        self.assertEqual(list(seq.iter_ids()), [-3, 0, -2, 2, 3, 4])
        self.assertEqual(seq.at(0), -3)
        seq.undelete(1)
        self.assertEqual(list(seq.iter_ids()), [-3, 0, -2, 1, 2, 3, 4])

    def test_delete_inserted_row(self):
        seq = RowSequence(3)
        seq.insert_after(1, -2)
        seq.delete(-2)
        self.assertEqual(list(seq.iter_ids()), [0, 1, 2])


class ReadTest(TempFileTest):
    def test_header_and_rows(self):
        table = self.table()
        self.assertEqual([c.name for c in table.columns], ["ts", "level", "msg"])
        self.assertEqual(table.row_count, 4)
        self.assertEqual(self.rows(table)[2], ["3", "WARN", "multi\nline"])

    def test_read_range_matches_iter_all(self):
        table = self.table()
        self.assertEqual([r for _rid, r in table.iter_all()], self.rows(table))

    def test_headerless_file(self):
        path = os.path.join(self.dir.name, "nohead.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("1,2,3\n4,5,6\n")
        table = Table(path, use_cache=False)
        self.assertEqual([c.name for c in table.columns], ["col1", "col2", "col3"])
        self.assertEqual(table.row_count, 2)


class EditTest(TempFileTest):
    def test_set_cell_and_undo(self):
        table = self.table()
        cid = table.columns[1].id
        self.assertEqual(table.set_cells([(1, cid, "FATAL")]), 1)
        self.assertTrue(table.dirty)
        self.assertEqual(self.rows(table)[1][1], "FATAL")
        table.undo()
        self.assertFalse(table.dirty)
        self.assertEqual(self.rows(table)[1][1], "ERROR")
        table.redo()
        self.assertEqual(self.rows(table)[1][1], "FATAL")

    def test_setting_same_value_is_not_an_edit(self):
        table = self.table()
        self.assertEqual(table.set_cells([(0, table.columns[1].id, "INFO")]), 0)
        self.assertFalse(table.dirty)

    def test_insert_delete_rows(self):
        table = self.table()
        rid = table.insert_row(1, ["9", "DEBUG", "new"])
        self.assertEqual(table.row_count, 5)
        self.assertEqual(self.rows(table)[1], ["9", "DEBUG", "new"])
        table.delete_rows([0])
        self.assertEqual(self.rows(table)[0], ["9", "DEBUG", "new"])
        table.undo()
        table.undo()
        self.assertEqual(table.row_count, 4)
        self.assertEqual(self.rows(table)[0], ["1", "INFO", "hello"])
        self.assertFalse(table.dirty)
        void = table.read_row(rid)
        self.assertEqual(void, ["", "", ""])

    def test_column_operations(self):
        table = self.table()
        table.rename_column(table.columns[0].id, "time")
        table.add_column("note", at=1)
        table.set_cells([(0, table.columns[1].id, "first")])
        self.assertEqual([c.name for c in table.columns], ["time", "note", "level", "msg"])
        self.assertEqual(self.rows(table)[0], ["1", "first", "INFO", "hello"])
        table.move_column(table.columns[1].id, 3)
        self.assertEqual([c.name for c in table.columns], ["time", "level", "msg", "note"])
        self.assertEqual(self.rows(table)[0][3], "first")
        table.delete_column(table.columns[3].id)
        self.assertEqual(self.rows(table)[0], ["1", "INFO", "hello"])

    def test_undo_stack_depth(self):
        table = self.table()
        cid = table.columns[2].id
        for i in range(table.MAX_UNDO + 20):
            table.set_cells([(0, cid, f"v{i}")])
        self.assertEqual(len(table.undo_stack), table.MAX_UNDO)


class SaveTest(TempFileTest):
    def test_untouched_file_round_trips_byte_for_byte(self):
        for encoding, newline, text in (
            ("utf-8", "\n", "a,b\n1,2\n"),
            ("utf-8-sig", "\r\n", "a,b\r\n1,2\r\n"),
            ("cp949", "\r\n", "이름,값\r\n가,1\r\n"),
        ):
            path = os.path.join(self.dir.name, f"rt_{encoding}_{len(newline)}.csv")
            data = text.encode(encoding)
            with open(path, "wb") as fh:
                fh.write(data)
            out = path + ".out"
            Table(path, use_cache=False).write_to(out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), data, f"{encoding} {newline!r}")

    def test_save_applies_overlay_and_reindexes(self):
        table = self.table()
        table.set_cells([(0, table.columns[1].id, "TRACE")])
        table.insert_row(4, ["5", "INFO", "added"])
        table.delete_rows([2])
        written = table.save(backup=True)
        self.assertEqual(written, 4)
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertFalse(table.dirty)
        reopened = Table(self.path, use_cache=False)
        self.assertEqual(
            self.rows(reopened),
            [["1", "TRACE", "hello"], ["2", "ERROR", "boom"], ["4", "INFO", "tail"],
             ["5", "INFO", "added"]],
        )

    def test_save_as_view_only(self):
        from array import array

        table = self.table()
        table.view = array("q", [1, 3])
        out = os.path.join(self.dir.name, "view.csv")
        self.assertEqual(table.save_as(out, view_only=True), 2)
        with open(out, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), "ts,level,msg\n2,ERROR,boom\n4,INFO,tail\n")

    def test_save_as_converts_encoding_and_newline(self):
        table = self.table()
        out = os.path.join(self.dir.name, "win.csv")
        table.save_as(out, encoding="utf-8-sig", newline="\r\n")
        with open(out, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", data)


if __name__ == "__main__":
    unittest.main()


class RawStreamTest(TempFileTest):
    def _wide_table(self, rows=300):
        path = os.path.join(self.dir.name, "wide.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("a,b\n")
            for i in range(rows):
                fh.write(f'{i},"x{"y" * (i % 29)}\nz"\n')  # embedded newline, varying length
        return Table(path, use_cache=False)

    def test_blocks_cover_every_record_exactly_once(self):
        table = self._wide_table()
        expected = [row for _rid, row in table.iter_all()]
        offsets = table.index.offsets
        header = table.header_rows
        for chunk in (16, 64, 4096, 1 << 20):
            ids, parsed = [], []
            for first_rid, base, blob in table.iter_blocks(chunk=chunk):
                rid = first_rid
                while rid + header + 1 < len(offsets) and offsets[rid + header + 1] - base <= len(blob):
                    start = offsets[rid + header] - base
                    parsed.append(table.parse_bytes(blob[start : offsets[rid + header + 1] - base]))
                    ids.append(rid)
                    rid += 1
            self.assertEqual(ids, list(range(300)), f"chunk={chunk}")
            self.assertEqual(parsed, expected, f"chunk={chunk}")

    def test_row_at_offset_maps_bytes_back_to_rows(self):
        table = self._wide_table()
        offsets = table.index.offsets
        for rid in (0, 1, 17, 299):
            start, end = offsets[rid + 1], offsets[rid + 2]  # header excluded
            self.assertEqual(table.row_at_offset(start), rid)
            self.assertEqual(table.row_at_offset(end - 1), rid)

    def test_can_stream_raw_turns_off_after_an_edit(self):
        table = self.table()
        self.assertTrue(table.can_stream_raw())
        table.set_cells([(0, table.columns[1].id, "TRACE")])
        self.assertFalse(table.can_stream_raw())
        table.undo()
        self.assertTrue(table.can_stream_raw())


class BookmarkTest(TempFileTest):
    def marked(self, table):
        return list(table.iter_marked())

    def test_mark_toggle_and_count(self):
        table = self.table()
        self.assertTrue(table.set_mark(1))
        self.assertTrue(table.is_marked(1))
        self.assertEqual(table.mark_count, 1)
        self.assertFalse(table.set_mark(1))
        self.assertEqual(table.mark_count, 0)
        table.set_mark(2, True)
        table.set_mark(2, True)
        self.assertEqual(table.mark_count, 1)

    def test_mark_ids_from_a_search(self):
        from csvopt import ops

        table = self.table()
        level = table.columns[1].id
        ids = ops.run_filter(table, [ops.Condition(col=level, op="equals", value="INFO")])
        self.assertEqual(table.mark_ids(ids), 2)
        self.assertEqual(self.marked(table), [0, 3])

    def test_invert_marks(self):
        table = self.table()
        table.mark_ids([0, 2])
        table.invert_marks()
        self.assertEqual(self.marked(table), [1, 3])

    def test_navigation_wraps(self):
        table = self.table()
        table.mark_ids([1, 3])
        self.assertEqual(table.find_mark(0), 1)
        self.assertEqual(table.find_mark(1), 3)
        self.assertEqual(table.find_mark(3), 1)          # wraps forward
        self.assertEqual(table.find_mark(0, forward=False), 3)  # wraps backward
        table.clear_marks()
        self.assertEqual(table.find_mark(0), -1)

    def test_delete_marked_rows_and_undo(self):
        table = self.table()
        table.mark_ids([1, 2])
        self.assertEqual(table.delete_marked(), 2)
        self.assertEqual(table.row_count, 2)
        self.assertEqual(self.rows(table), [["1", "INFO", "hello"], ["4", "INFO", "tail"]])
        self.assertEqual(table.mark_count, 0)
        table.undo()
        self.assertEqual(table.row_count, 4)
        self.assertEqual(table.mark_count, 2)
        table.redo()
        self.assertEqual(table.row_count, 2)

    def test_delete_unmarked_rows_keeps_only_bookmarks(self):
        table = self.table()
        table.mark_ids([0, 3])
        self.assertEqual(table.delete_marked(keep=True), 2)
        self.assertEqual(self.rows(table), [["1", "INFO", "hello"], ["4", "INFO", "tail"]])
        self.assertEqual(table.mark_count, 2)
        table.undo()
        self.assertEqual(table.row_count, 4)

    def test_marks_survive_edits_and_follow_inserted_rows(self):
        table = self.table()
        rid = table.insert_row(1, ["9", "DEBUG", "new"])
        table.set_mark(rid)
        self.assertEqual(table.mark_count, 1)
        self.assertEqual(self.marked(table), [rid])
        table.invert_marks()
        self.assertNotIn(rid, self.marked(table))
        self.assertEqual(len(self.marked(table)), 4)

    def test_deleting_a_row_clears_its_bookmark(self):
        table = self.table()
        table.mark_ids([2])
        table.delete_rows([2])
        self.assertEqual(table.mark_count, 0)

    def test_saving_writes_only_kept_rows_after_bulk_delete(self):
        table = self.table()
        table.mark_ids([1])
        table.delete_marked(keep=True)
        out = os.path.join(self.dir.name, "kept.csv")
        self.assertEqual(table.write_to(out), 1)
        with open(out, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), "ts,level,msg\n2,ERROR,boom\n")


class BulkSequenceTest(unittest.TestCase):
    def test_bitset_backed_sequence_handles_mass_deletion(self):
        from csvopt.bitset import Bitset

        seq = RowSequence(100000)
        keep = Bitset(100000)
        keep.update(range(0, 100000, 1000))
        removed = seq.keep_where(keep)
        self.assertEqual(removed, 100000 - 100)
        self.assertEqual(seq.count, 100)
        self.assertEqual(seq.at(0), 0)
        self.assertEqual(seq.at(99), 99000)
        self.assertEqual(seq.slice(50, 3), [50000, 51000, 52000])
        self.assertEqual(seq.position_of(52000), 52)
        self.assertEqual(list(seq.iter_ids())[:3], [0, 1000, 2000])

    def test_snapshot_restores_previous_shape(self):
        from csvopt.bitset import Bitset

        seq = RowSequence(5000)
        seq.delete(10)
        seq.insert_after(20, -2)
        before = seq.snapshot()
        marks = Bitset(5000)
        marks.update(range(0, 5000, 2))
        seq.delete_where(marks)
        # 2499 further rows deleted (row 10 was already gone) plus the inserted
        # row, which survives its anchor being deleted.
        self.assertEqual(seq.count, 2501)
        seq.restore(before)
        self.assertEqual(seq.count, 5000)          # 5000 - 1 deleted + 1 inserted
        self.assertEqual(seq.at(19), 20)   # row 10 is gone, so rows shift up by one
        self.assertEqual(seq.at(20), -2)   # the inserted row follows its anchor
        self.assertTrue(seq.is_deleted(10))


class DiskGuardTest(TempFileTest):
    def test_save_refuses_when_the_disk_is_full(self):
        import shutil as _shutil
        from collections import namedtuple

        from csvopt.table import SaveError

        usage = namedtuple("usage", "total used free")
        table = self.table()
        original = _shutil.disk_usage
        _shutil.disk_usage = lambda _path: usage(total=1, used=1, free=0)
        try:
            with self.assertRaises(SaveError) as ctx:
                table.write_to(os.path.join(self.dir.name, "out.csv"))
        finally:
            _shutil.disk_usage = original
        self.assertIn("공간", str(ctx.exception))
