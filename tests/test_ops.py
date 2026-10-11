import os
import random
import tempfile
import unittest
from array import array

from csvopt import ops
from csvopt.table import Table

DATA = (
    "ts,level,service,latency,msg\n"
    "2026-01-01 00:00:01,INFO,auth,12.5,ok\n"
    "2026-01-01 00:00:02,ERROR,billing,340,connection reset\n"
    "2026-01-01 01:00:03,warn,auth,,slow\n"
    "2026-01-01 02:00:04,ERROR,search,1200.75,\"multi\nline\"\n"
    "2026-01-01 03:00:05,INFO,auth,12.5,ok\n"
)


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "log.csv")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DATA)
        self.table = Table(self.path, use_cache=False)
        self.cid = {c.name: c.id for c in self.table.columns}

    def tearDown(self):
        self.dir.cleanup()

    def ids(self, *conditions, **kw):
        return list(ops.run_filter(self.table, list(conditions), **kw))

    def all_ids(self):
        return list(self.table.all_ids())

    def cond(self, col, op, value="", value2="", **kw):
        return ops.Condition(col=self.cid.get(col), op=op, value=value, value2=value2, **kw)

    def test_equals_is_case_insensitive_by_default(self):
        self.assertEqual(self.ids(self.cond("level", "equals", "error")), [1, 3])
        self.assertEqual(
            self.ids(self.cond("level", "equals", "error", case_sensitive=True)), []
        )
        self.assertEqual(
            self.ids(self.cond("level", "equals", "ERROR", case_sensitive=True)), [1, 3]
        )
        self.assertEqual(self.ids(self.cond("level", "equals", "WARN")), [2])

    def test_contains_across_all_columns(self):
        self.assertEqual(self.ids(ops.Condition(col=None, op="contains", value="auth")), [0, 2, 4])

    def test_regex_and_negate(self):
        self.assertEqual(self.ids(self.cond("msg", "regex", "^ok$")), [0, 4])
        self.assertEqual(self.ids(self.cond("level", "equals", "INFO", negate=True)), [1, 2, 3])

    def test_numeric_comparisons(self):
        self.assertEqual(self.ids(self.cond("latency", "gt", "100")), [1, 3])
        self.assertEqual(self.ids(self.cond("latency", "between", "10", "500")), [0, 1, 4])
        self.assertEqual(self.ids(self.cond("latency", "empty")), [2])
        self.assertEqual(self.ids(self.cond("latency", "not_empty")), [0, 1, 3, 4])

    def test_time_range(self):
        cond = ops.Condition(
            col=self.cid["ts"], op="time_between",
            value="2026-01-01 00:30:00", value2="2026-01-01 02:30:00",
        )
        self.assertEqual(self.ids(cond), [2, 3])

    def test_and_or_semantics(self):
        conds = [self.cond("level", "equals", "ERROR"), self.cond("service", "equals", "search")]
        self.assertEqual(self.ids(*conds), [3])
        self.assertEqual(self.ids(*conds, match_all=False), [1, 3])

    def test_prefilter_matches_slow_path(self):
        random.seed(3)
        path = os.path.join(self.dir.name, "big.csv")
        levels = ["INFO", "ERROR", "WARN", "DEBUG"]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("id,level,msg\n")
            for i in range(5000):
                fh.write(f"{i},{random.choice(levels)},\"line {i} ,x\"\n")
        table = Table(path, use_cache=False)
        cid = {c.name: c.id for c in table.columns}
        conds = [
            ops.Condition(col=cid["level"], op="equals", value="ERROR"),
            ops.Condition(col=cid["id"], op="gt", value="2500"),
        ]
        self.assertTrue(table.can_stream_raw())
        fast = list(ops.run_filter(table, conds))
        slow = list(ops.run_filter(table, conds, base_ids=list(table.all_ids())))
        self.assertEqual(fast, slow)
        self.assertTrue(fast)

    def test_sort_numeric_and_text(self):
        by_latency = list(ops.sort_ids(self.table, self.cid["latency"]))
        self.assertEqual(by_latency[:3], [0, 4, 1])   # 12.5, 12.5, 340 then 1200.75
        self.assertEqual(by_latency[-1], 2)           # empty sorts last
        by_service = list(ops.sort_ids(self.table, self.cid["service"], descending=True))
        self.assertEqual([self.table.read_row(r)[2] for r in by_service][0], "search")

    def test_sort_row_limit(self):
        with self.assertRaises(ValueError):
            ops.sort_ids(self.table, self.cid["level"], max_rows=2)

    def test_column_stats(self):
        stats = ops.column_stats(self.table, self.cid["level"])
        self.assertEqual(stats.total, 5)
        self.assertEqual(dict(stats.top)["INFO"], 2)
        numeric = ops.column_stats(self.table, self.cid["latency"])
        self.assertEqual(numeric.empty, 1)
        self.assertEqual(numeric.numeric_count, 4)
        self.assertAlmostEqual(numeric.minimum, 12.5)
        self.assertAlmostEqual(numeric.maximum, 1200.75)

    def test_stats_respects_active_view(self):
        self.table.view = array("q", [0, 4])
        stats = ops.column_stats(self.table, self.cid["level"])
        self.assertEqual(stats.total, 2)
        self.assertEqual(stats.distinct, 1)

    def test_find_and_replace(self):
        hits = ops.find_matches(self.table, "auth")
        self.assertEqual(hits, [(0, 2), (2, 2), (4, 2)])
        changed = ops.replace_all(self.table, "auth", "authn", col_id=self.cid["service"])
        self.assertEqual(changed, 3)
        self.assertEqual(self.table.read_row(0)[2], "authn")
        self.table.undo()
        self.assertEqual(self.table.read_row(0)[2], "auth")

    def test_replace_with_regex_groups(self):
        changed = ops.replace_all(
            self.table, r"(\d+)\.(\d+)", r"\1_\2", col_id=self.cid["latency"], use_regex=True
        )
        self.assertEqual(changed, 3)
        self.assertEqual(self.table.read_row(0)[3], "12_5")

    def test_replace_literal_backslash_is_not_a_group(self):
        ops.replace_all(self.table, "ok", "a\\1b", col_id=self.cid["msg"])
        self.assertEqual(self.table.read_row(0)[4], "a\\1b")

    def test_dedupe_and_trim(self):
        dupes = ops.dedupe(self.table, col_ids=[self.cid["service"]])
        self.assertEqual(dupes, [2, 4])
        self.table.set_cells([(0, self.cid["msg"], "  padded  ")])
        self.assertEqual(ops.trim_whitespace(self.table, col_id=self.cid["msg"]), 1)
        self.assertEqual(self.table.read_row(0)[4], "padded")

    def test_number_and_timestamp_parsing(self):
        self.assertEqual(ops.parse_number("1,234.5"), 1234.5)
        self.assertEqual(ops.parse_number("12ms"), 12.0)
        self.assertIsNone(ops.parse_number("n/a"))
        self.assertIsNotNone(ops.parse_timestamp("2026-01-01T10:00:00Z"))
        self.assertIsNotNone(ops.parse_timestamp("1767225600"))
        self.assertIsNotNone(ops.parse_timestamp("10/Oct/2026:13:55:36"))
        self.assertIsNone(ops.parse_timestamp("not a time"))

    def test_regex_literal_extraction(self):
        self.assertEqual(ops.regex_literals("ERROR.*timeout"), ["ERROR", "timeout"])
        self.assertEqual(ops.regex_literals("user session (expired|reset)"), ["user session "])
        self.assertEqual(ops.regex_literals(r"user:\d{5}"), ["user:"])
        self.assertEqual(ops.regex_literals("(abc)+def"), ["abcdef"])
        self.assertEqual(ops.regex_literals("(?:xy)?zz"), ["zz"])
        self.assertEqual(ops.regex_literals("^ok$"), ["ok"])
        self.assertEqual(ops.regex_literals("reset|expired"), [])   # nothing is mandatory
        self.assertEqual(ops.regex_literals("(?i)abc"), [])          # inline flags: stay safe
        self.assertEqual(ops.regex_literals("[a-z]+"), [])
        self.assertEqual(ops.regex_literals("bad["), [])             # invalid pattern

    def test_regex_filters_match_the_unaccelerated_path(self):
        for pattern in ("connection reset", "connection.*peer", r"user:\d+",
                        "reset|expired", "^ok$", "slow query"):
            cond = self.cond("msg", "regex", pattern)
            fast = self.ids(cond)
            slow = self.ids(cond, base_ids=list(self.table.all_ids()))
            self.assertEqual(fast, slow, pattern)

    def test_case_insensitive_non_ascii_is_not_prefiltered(self):
        path = os.path.join(self.dir.name, "accents.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("id,city\n1,Ärger\n2,plain\n")
        table = Table(path, use_cache=False)
        city = table.columns[1].id
        # 'Ä'.lower() is not reachable through bytes.lower(), so the literal must
        # be rejected and the filter still has to find the row.
        cond = ops.Condition(col=city, op="contains", value="ÄRGER")
        self.assertEqual(list(ops.run_filter(table, [cond])), [0])
        self.assertEqual(ops._prefilter_literals(table, ops.compile_conditions(table, [cond])), [])

    def test_unselective_literal_falls_back_to_streaming(self):
        # 'INFO' style values that appear in most records should not be used for
        # the block scan; results must be identical either way.
        cond = self.cond("service", "contains", "a")
        fast = self.ids(cond)
        slow = self.ids(cond, base_ids=list(self.table.all_ids()))
        self.assertEqual(fast, slow)

    def test_dedupe_refuses_to_track_too_many_keys(self):
        with self.assertRaises(ValueError) as ctx:
            ops.dedupe(self.table, max_keys=2)
        self.assertIn("중복 검사", str(ctx.exception))

    def test_filter_over_edited_rows_uses_overlay(self):
        self.table.set_cells([(0, self.cid["level"], "ERROR")])
        self.assertFalse(self.table.can_stream_raw())
        self.assertEqual(self.ids(self.cond("level", "equals", "ERROR")), [0, 1, 3])


if __name__ == "__main__":
    unittest.main()
