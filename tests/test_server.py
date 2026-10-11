import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from csvopt.server import serve

DATA = "ts,level,msg\n1,INFO,hello\n2,ERROR,boom\n3,WARN,tail\n"


class ServerCase(unittest.TestCase):
    """Shared fixture: one server instance for every API test.

    A subclass that needs a differently shaped log overrides ``DATA``.
    """

    DATA = DATA

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.dir.name, "log.csv")
        with open(cls.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(cls.DATA)
        cls.server = serve(cls.path, port=0, open_browser=False)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def call(self, name, token=None, **payload):
        req = urllib.request.Request(
            f"{self.base}/api/{name}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Csvopt-Token": self.server.token if token is None else token,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read())

    def run_job(self, name, **payload):
        job = self.call(name, **payload)["job"]
        for _ in range(200):
            job = self.call("job", id=job["id"])["job"]
            if job["status"] != "running":
                return job
            threading.Event().wait(0.05)
        self.fail("job did not finish")


class ServerTest(ServerCase):
    # ------------------------------------------------------------- security

    def test_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("state", token="wrong")
        self.assertEqual(ctx.exception.code, 403)

    def test_rejects_foreign_host_header(self):
        req = urllib.request.Request(
            f"{self.base}/api/state", data=b"{}",
            headers={"Host": "evil.example.com", "X-Csvopt-Token": self.server.token},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)

    def test_unknown_endpoint(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_index_page_and_static_assets(self):
        with urllib.request.urlopen(f"{self.base}/?t={self.server.token}", timeout=10) as res:
            self.assertIn(b"csvopt", res.read())
        for asset in ("app.js", "grid.js", "api.js", "style.css"):
            with urllib.request.urlopen(f"{self.base}/static/{asset}", timeout=10) as res:
                self.assertEqual(res.status, 200)

    def test_static_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/static/../server.py", timeout=10)
        self.assertIn(ctx.exception.code, (403, 404))

    # ----------------------------------------------------------------- api

    def test_state_and_rows(self):
        state = self.call("state")
        self.assertTrue(state["open"])
        self.assertEqual(state["rows"], 3)
        self.assertEqual([c["name"] for c in state["columns"]], ["ts", "level", "msg"])
        rows = self.call("rows", start=0, count=10)
        self.assertEqual(rows["rows"][1], ["2", "ERROR", "boom"])
        self.assertEqual(rows["ids"], [0, 1, 2])

    def test_edit_undo_and_position(self):
        cid = self.call("state")["columns"][1]["id"]
        res = self.call("set_cells", cells=[{"rid": 0, "cid": cid, "value": "TRACE"}])
        self.assertEqual(res["changed"], 1)
        self.assertTrue(res["state"]["dirty"])
        self.assertEqual(self.call("rows", start=0, count=1)["rows"][0][1], "TRACE")
        self.assertEqual(self.call("position", rid=2)["pos"], 2)
        self.assertEqual(self.call("undo")["label"], "edit")
        self.assertFalse(self.call("state")["dirty"])

    def test_filter_job_then_clear(self):
        cid = self.call("state")["columns"][1]["id"]
        job = self.run_job("filter", conditions=[{"col": cid, "op": "equals", "value": "ERROR"}])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["rows"], 1)
        state = self.call("state")
        self.assertTrue(state["view"])
        self.assertEqual(state["rows"], 1)
        self.assertEqual(self.call("rows", start=0, count=5)["rows"][0][2], "boom")
        self.call("clear_view")
        self.assertEqual(self.call("state")["rows"], 3)

    def test_stats_job(self):
        cid = self.call("state")["columns"][1]["id"]
        job = self.run_job("stats", cid=cid)
        self.assertEqual(job["result"]["total"], 3)
        self.assertEqual(job["result"]["distinct"], 3)

    def test_save_as_export(self):
        out = os.path.join(self.dir.name, "export.csv")
        job = self.run_job("save_as", path=out, encoding="utf-8-sig", newline="crlf")
        self.assertEqual(job["result"]["written"], 3)
        with open(out, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", data)

    def test_browse_lists_the_sample_directory(self):
        listing = self.call("browse", path=self.dir.name)
        names = [e["name"] for e in listing["entries"]]
        self.assertIn("log.csv", names)
        self.assertTrue(listing["roots"])

    def test_open_missing_file_is_reported(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("open", path=os.path.join(self.dir.name, "nope.csv"))
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()


class BookmarkApiTest(ServerCase):
    def setUp(self):
        self.call("marks_clear")
        self.call("clear_view")

    def test_mark_toggle_and_rows_report_marks(self):
        res = self.call("mark", rid=1)
        self.assertTrue(res["marked"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(self.call("rows", start=0, count=5)["marked"], [1])
        self.assertFalse(self.call("mark", rid=1)["marked"])

    def test_mark_search_marks_every_match(self):
        cid = self.call("state")["columns"][1]["id"]
        job = self.run_job("mark_search", conditions=[{"col": cid, "op": "equals", "value": "ERROR"}])
        self.assertEqual(job["result"]["matched"], 1)
        self.assertEqual(self.call("state")["marks"], 1)

    def test_invert_and_clear(self):
        self.call("mark", rid=0)
        self.assertEqual(self.call("marks_invert")["count"], 2)
        self.assertEqual(self.call("marks_clear")["count"], 0)

    def test_marks_filter_shows_only_bookmarks(self):
        self.call("mark", rid=2)
        self.assertEqual(self.call("marks_filter")["rows"], 1)
        self.assertEqual(self.call("rows", start=0, count=5)["rows"], [["3", "WARN", "tail"]])
        self.call("clear_view")

    def test_marks_navigation(self):
        self.call("mark", rid=2)
        self.assertEqual(self.call("marks_next", pos=0, forward=True)["pos"], 2)
        self.assertEqual(self.call("marks_next", pos=2, forward=True)["pos"], 2)  # wraps to itself
        self.call("marks_clear")
        self.assertEqual(self.call("marks_next", pos=0, forward=True)["pos"], -1)

    def test_delete_marked_and_undo(self):
        self.call("mark", rid=0)
        job = self.run_job("marks_delete", keep=False)
        self.assertEqual(job["result"]["removed"], 1)
        self.assertEqual(self.call("state")["rows"], 2)
        self.call("undo")
        self.assertEqual(self.call("state")["rows"], 3)

    def test_delete_unmarked_keeps_bookmarks(self):
        self.call("mark", rid=1)
        job = self.run_job("marks_delete", keep=True)
        self.assertEqual(job["result"]["removed"], 2)
        self.assertEqual(self.call("rows", start=0, count=5)["rows"], [["2", "ERROR", "boom"]])
        self.call("undo")
        self.assertEqual(self.call("state")["rows"], 3)

    def test_export_bookmarked_rows_only(self):
        self.call("mark", rid=2)
        out = os.path.join(self.dir.name, "marks.csv")
        job = self.run_job("save_as", path=out, scope="marks")
        self.assertEqual(job["result"]["written"], 1)
        with open(out, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), "ts,level,msg\n3,WARN,tail\n")

    def test_export_without_bookmarks_is_reported(self):
        out = os.path.join(self.dir.name, "empty.csv")
        job = self.run_job("save_as", path=out, scope="marks")
        self.assertEqual(job["status"], "error")
        self.assertIn("책갈피", job["error"])


class StateStampTest(ServerCase):
    def test_every_state_snapshot_has_an_increasing_stamp(self):
        first = self.call("state")["stamp"]
        second = self.call("state")["stamp"]
        self.assertGreater(second, first)
        embedded = self.call("mark", rid=0)
        self.call("marks_clear")
        self.assertIn("count", embedded)
        self.assertGreater(self.call("state")["stamp"], second)


class WorkerSettingTest(ServerCase):
    def test_state_reports_the_planned_worker_count(self):
        state = self.call("state")
        self.assertIn("workers", state)
        # The fixture file is tiny, so filtering stays in this process.
        self.assertEqual(state["workers"], 1)


class ColumnApiTest(ServerCase):
    def column_names(self):
        return [c["name"] for c in self.call("state")["columns"]]

    def test_delete_many_columns_in_one_request(self):
        cols = self.call("state")["columns"]
        res = self.call("column", action="delete", cids=[cols[0]["id"], cols[2]["id"]])
        self.assertEqual(res["removed"], 2)
        self.assertEqual(self.column_names(), ["level"])
        self.assertEqual(self.call("rows", start=0, count=1)["rows"], [["INFO"]])
        self.call("undo")
        self.assertEqual(self.column_names(), ["ts", "level", "msg"])

    def test_single_cid_is_still_accepted(self):
        cols = self.call("state")["columns"]
        res = self.call("column", action="delete", cid=cols[1]["id"])
        self.assertEqual(res["removed"], 1)
        self.assertEqual(self.column_names(), ["ts", "msg"])
        self.call("undo")
        self.assertEqual(self.column_names(), ["ts", "level", "msg"])

    def test_deleting_all_columns_is_rejected(self):
        cols = self.call("state")["columns"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("column", action="delete", cids=[c["id"] for c in cols])
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(len(self.column_names()), 3)


FIREWALL = (
    "timestamp,src_ip,dst_ip,protocol,src_port,dst_port,action\n"
    "2026-03-01 00:00:00,70.222.12.5,105.181.150.6,UDP,41000,161,allow\n"
    "2026-03-01 00:00:01,70.222.12.64,105.181.150.6,UDP,41001,161,allow\n"
    "2026-03-01 00:00:02,105.181.150.6,70.222.12.62,UDP,41002,514,allow\n"
    "2026-03-01 00:00:03,70.222.12.9,105.181.150.6,TCP,41003,23,allow\n"
    "2026-03-01 00:00:04,10.0.0.1,10.0.0.2,TCP,41004,80,deny\n"
)

POLICY = (
    "Source\tDestination\tProtocol\tPort\tPurpose\n"
    "70.222.12.0/26\t105.181.150.6\tUDP\t161\tSNMP\n"
    "105.181.150.6\t70.222.12.62\tUDP\t514\tSyslog\n"
    "70.222.12.0/26\t105.181.150.6\tTCP\t22, 23\tConfig Backup\n"
    "203.0.113.0/24\t105.181.150.6\tUDP\t161\t미사용 정책\n"
)


class RuleApiTest(ServerCase):
    """The multi searcher: paste a policy table, scan once, pick a rule."""

    DATA = FIREWALL

    def parsed(self):
        return self.call("rules_parse", text=POLICY)

    def run_rules(self, **extra):
        spec = self.parsed()
        payload = {"rows": spec["rows"], "mapping": spec["mapping"],
                   "header": spec["header"]}
        payload.update(extra)
        return self.run_job("rules_run", **payload)

    def test_parse_suggests_a_mapping_from_the_log_columns(self):
        spec = self.parsed()
        names = {c["id"]: c["name"] for c in spec["columns"]}
        self.assertEqual(spec["header"][0], "Source")
        self.assertEqual([names.get(m["column"]) for m in spec["mapping"]],
                         ["src_ip", "dst_ip", "protocol", "dst_port", None])
        self.assertEqual(spec["kinds"], ["ip", "ip", "equals", "ports", "ignore"])
        self.assertEqual(len(spec["rows"]), 4)

    def test_parse_rejects_text_that_is_not_a_table(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("rules_parse", text="   \n\n")
        self.assertEqual(ctx.exception.code, 400)

    def test_remap_guesses_from_a_sheet_being_edited(self):
        # The sheet sends its own header and rows, with no pasted text.
        res = self.call(
            "rules_map",
            header=["Source", "Destination", "Protocol", "Port"],
            rows=[["70.222.12.0/26", "105.181.150.6", "UDP", "161"]],
        )
        names = {c["id"]: c["name"] for c in res["columns"]}
        self.assertEqual([names.get(m["column"]) for m in res["mapping"]],
                         ["src_ip", "dst_ip", "protocol", "dst_port"])
        self.assertEqual(res["kinds"], ["ip", "ip", "equals", "ports"])

    def test_remap_honours_kinds_the_user_set(self):
        res = self.call(
            "rules_map",
            header=["Source", "Port"],
            rows=[["70.222.12.5", "161"]],
            kinds=["ip", "ignore"],
        )
        self.assertIsNone(res["mapping"][1]["column"])
        self.assertEqual(res["mapping"][1]["kind"], "ignore")

    def test_remap_without_rows_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("rules_map", header=["Source"], rows=[])
        self.assertEqual(ctx.exception.code, 400)

    def test_run_reports_a_count_per_rule_and_filters_the_view(self):
        job = self.run_rules()
        self.assertEqual(job["status"], "done")
        result = job["result"]
        self.assertEqual([r["count"] for r in result["rules"]], [1, 1, 1, 0])
        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["scanned"], 5)
        state = self.call("state")
        self.assertTrue(state["view"])
        self.assertEqual(state["view_rows"], 3)
        self.assertEqual([r["count"] for r in state["rules"]["rules"]], [1, 1, 1, 0])
        self.call("rules_clear")

    def test_rule_labels_name_the_purpose_column(self):
        job = self.run_rules()
        self.assertTrue(job["result"]["rules"][0]["label"].startswith("SNMP"))
        self.call("rules_clear")

    def test_rows_carry_the_rule_that_caught_them(self):
        self.run_rules()
        rows = self.call("rows", start=0, count=10)
        self.assertEqual(set(rows["rules"].values()), {0, 1, 2})
        self.call("rules_clear")

    def test_selecting_one_rule_narrows_the_view(self):
        self.run_rules()
        res = self.call("rules_select", rule=2)
        self.assertEqual(res["rows"], 1)
        self.assertIn("Config Backup", res["state"]["view_label"])
        rows = self.call("rows", start=0, count=10)
        self.assertEqual(rows["rows"][0][5], "23")
        self.assertEqual(self.call("rules_select", rule=None)["rows"], 3)
        self.call("rules_clear")

    def test_selecting_a_missing_rule_is_rejected(self):
        self.run_rules()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("rules_select", rule=99)
        self.assertEqual(ctx.exception.code, 400)
        self.call("rules_clear")

    def test_select_before_a_run_is_rejected(self):
        self.call("rules_clear")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("rules_select", rule=0)
        self.assertEqual(ctx.exception.code, 400)

    def test_marking_a_rule_bookmarks_its_rows(self):
        self.run_rules()
        res = self.call("rules_mark", rule=0)
        self.assertEqual(res["marked"], 1)
        self.assertEqual(res["state"]["marks"], 1)
        every = self.call("rules_mark", rule=None)
        self.assertEqual(every["marked"], 3)
        self.assertEqual(every["state"]["marks"], 3)
        self.call("marks_clear")
        self.call("rules_clear")

    def test_clear_drops_both_the_result_and_the_view(self):
        self.run_rules()
        state = self.call("rules_clear")["state"]
        self.assertFalse(state["view"])
        self.assertIsNone(state["rules"])
        self.assertEqual(self.call("rows", start=0, count=10)["rules"], {})

    def test_scope_view_searches_inside_the_current_result(self):
        self.run_job("filter", conditions=[], quick="UDP", scope="all")
        job = self.run_rules(scope="view")
        self.assertEqual(job["result"]["scanned"], 3)   # only the UDP rows
        self.assertEqual([r["count"] for r in job["result"]["rules"]], [1, 1, 0, 0])
        self.call("rules_clear")

    def test_run_without_rows_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("rules_run", rows=[], mapping=[])
        self.assertEqual(ctx.exception.code, 400)

    def test_run_with_nothing_mapped_fails_the_job(self):
        spec = self.parsed()
        blank = [{"column": None, "kind": "ignore"} for _ in spec["mapping"]]
        job = self.run_job("rules_run", rows=spec["rows"], mapping=blank)
        self.assertEqual(job["status"], "error")
        self.assertIn("조건", job["error"])
