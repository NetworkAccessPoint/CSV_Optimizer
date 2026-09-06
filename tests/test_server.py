import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from csvopt.server import serve

DATA = "ts,level,msg\n1,INFO,hello\n2,ERROR,boom\n3,WARN,tail\n"


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.dir.name, "log.csv")
        with open(cls.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DATA)
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
