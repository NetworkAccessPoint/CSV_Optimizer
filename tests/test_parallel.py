"""Worker-pool filtering.

The end-to-end case really does start processes, so it is written to stay small:
a few thousand rows split over two workers is enough to prove the pieces line up
(pickling, the shared index, segment ordering).
"""

import os
import tempfile
import unittest

from csvopt import ops, parallel
from csvopt.table import Table


class SegmentTest(unittest.TestCase):
    def test_segments_cover_every_row_once(self):
        segments = parallel._segments(1_000_000, 4)
        self.assertEqual(segments[0][0], 0)
        self.assertEqual(segments[-1][1], 1_000_000)
        for (_, previous_stop), (start, _) in zip(segments, segments[1:]):
            self.assertEqual(previous_stop, start)

    def test_small_tables_get_one_segment(self):
        self.assertEqual(parallel._segments(10, 4), [(0, 10)])

    def test_minimum_segment_size_is_configurable(self):
        self.assertEqual(len(parallel._segments(20000, 2, min_rows=5000)), 4)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "small.csv")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write("id,level\n")
            for i in range(2000):
                fh.write(f"{i},{'ERROR' if i % 3 == 0 else 'INFO'}\n")
        self.table = Table(self.path, use_cache=False)

    def tearDown(self):
        self.table.close()
        self.dir.cleanup()

    def test_small_file_stays_single_process(self):
        self.assertEqual(parallel.plan_workers(self.table, None), 1)
        self.assertEqual(parallel.plan_workers(self.table, 4), 1)

    def test_explicit_single_worker_is_respected(self):
        self.assertEqual(parallel.plan_workers(self.table, 1), 1)

    def test_edited_table_stays_single_process(self):
        self.table.set_cells([(0, self.table.columns[1].id, "WARN")])
        self.assertFalse(self.table.can_stream_raw())
        self.assertEqual(parallel.plan_workers(self.table, 4), 1)


class WorkerPoolTest(unittest.TestCase):
    """Actually spawns processes; kept to one small file to stay quick."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.dir.name, "pool.csv")
        with open(cls.path, "w", encoding="utf-8", newline="") as fh:
            fh.write("id,level,msg\n")
            for i in range(20000):
                level = ("INFO", "ERROR", "WARN", "DEBUG")[i % 4]
                fh.write(f'{i},{level},"line {i}"\n')
        cls.table = Table(cls.path)   # cached index: the workers map the same file

    @classmethod
    def tearDownClass(cls):
        cls.table.close()
        from csvopt.index import drop_cached_index

        drop_cached_index(cls.path)
        cls.dir.cleanup()

    def conditions(self, **kw):
        by_name = {c.name: c.id for c in self.table.columns}
        return [ops.Condition(col=by_name[kw.pop("column")], **kw)]

    def test_pool_result_matches_single_process(self):
        conditions = self.conditions(column="level", op="equals", value="ERROR")
        expected = list(ops.run_filter(self.table, conditions, workers=1))
        found = parallel.filter_parallel(
            self.table, conditions, workers=2, min_segment_rows=5000)
        if found is None:
            self.skipTest("this environment cannot start worker processes")
        self.assertEqual(list(found), expected)
        self.assertEqual(len(expected), 5000)

    def test_pool_result_is_ordered_across_segments(self):
        conditions = self.conditions(column="msg", op="regex", value=r"line \d*7$")
        expected = list(ops.run_filter(self.table, conditions, workers=1))
        found = parallel.filter_parallel(
            self.table, conditions, workers=3, min_segment_rows=3000)
        if found is None:
            self.skipTest("this environment cannot start worker processes")
        self.assertEqual(list(found), expected)
        self.assertEqual(list(found), sorted(found))

    def test_pool_respects_a_limit(self):
        conditions = self.conditions(column="level", op="equals", value="INFO")
        found = parallel.filter_parallel(
            self.table, conditions, workers=2, limit=10, min_segment_rows=5000)
        if found is None:
            self.skipTest("this environment cannot start worker processes")
        self.assertEqual(len(found), 10)

    def test_cancelling_raises_aborted(self):
        from csvopt.index import Aborted

        conditions = self.conditions(column="level", op="equals", value="INFO")
        with self.assertRaises(Aborted):
            parallel.filter_parallel(
                self.table, conditions, workers=2, min_segment_rows=5000,
                progress=lambda done, total: False,
            )


if __name__ == "__main__":
    unittest.main()
