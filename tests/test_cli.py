import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from csvopt.cli import main

DATA = "ts,level,msg\n1,INFO,hello\n2,ERROR,boom\n3,ERROR,again\n"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "log.csv")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DATA)

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_info(self):
        code, out, _err = self.run_cli("info", self.path)
        self.assertEqual(code, 0)
        self.assertIn("행        : 3", out)
        self.assertIn("ts, level, msg", out)

    def test_grep_to_stdout(self):
        code, out, err = self.run_cli("grep", self.path, "-w", "level=ERROR")
        self.assertEqual(code, 0)
        self.assertIn("2행이 일치", err)
        self.assertEqual(out.strip().splitlines()[1], "2,ERROR,boom")

    def test_grep_to_file(self):
        out_path = os.path.join(self.dir.name, "hits.csv")
        self.run_cli("grep", self.path, "-c", "again", "-o", out_path)
        with open(out_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines()[1], "3,ERROR,again")

    def test_grep_unknown_column(self):
        with self.assertRaises(SystemExit):
            self.run_cli("grep", self.path, "-w", "nope=1")

    def test_convert(self):
        out_path = os.path.join(self.dir.name, "out.tsv")
        code, _out, _err = self.run_cli(
            "convert", self.path, "-o", out_path, "--delimiter", "\\t", "--newline", "crlf"
        )
        self.assertEqual(code, 0)
        with open(out_path, "rb") as fh:
            data = fh.read()
        self.assertIn(b"1\tINFO\thello\r\n", data)

    def test_bare_path_is_treated_as_open(self):
        # `csvopt file.csv` must route to the server command, not fail parsing.
        from csvopt import cli

        called = {}
        original = cli.cmd_serve

        def fake_serve(args):
            called["path"] = args.path
            called["port"] = args.port
            return 0

        cli.cmd_serve = fake_serve
        try:
            self.assertEqual(main([self.path]), 0)
            self.assertEqual(main(["--port", "9123", self.path]), 0)
        finally:
            cli.cmd_serve = original
        self.assertEqual(called["path"], self.path)
        self.assertEqual(called["port"], 9123)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("--version")
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()


class StdioTest(unittest.TestCase):
    """Windows pipes default to a legacy code page; Korean output must survive."""

    def test_redirected_output_is_switched_to_utf8(self):
        import io

        from csvopt.cli import _configure_stdio

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", newline="")
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream
        try:
            _configure_stdio()
            print("경로      : C:\\logs\\app.csv")
            stream.flush()
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr
        self.assertEqual(stream.encoding, "utf-8")
        self.assertIn("경로", raw.getvalue().decode("utf-8"))

    def test_info_output_survives_a_cp1252_stream(self):
        import io

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "log.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DATA)

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", newline="")
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream
        try:
            code = main(["info", path])
            stream.flush()
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr
        self.assertEqual(code, 0)
        self.assertIn("인코딩", raw.getvalue().decode("utf-8"))
