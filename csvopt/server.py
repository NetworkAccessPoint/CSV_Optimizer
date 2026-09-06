"""Local HTTP API + static file server for the csvopt web UI.

Only the standard library is used, and the socket is bound to the loopback
interface with a random per-run token, so the editor behaves like a desktop app
on Windows and macOS alike: run it, a browser tab opens, nothing is exposed to
the network.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import posixpath
import secrets
import string
import threading
import webbrowser
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import ops
from .index import Aborted
from .jobs import Job, JobManager
from .table import SaveError, Table

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 64 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class Session:
    """Everything one running editor instance owns: a table and its jobs."""

    def __init__(self, path: Optional[str] = None, use_cache: bool = True):
        self.lock = threading.RLock()
        self.jobs = JobManager()
        self.table: Optional[Table] = None
        self.error: str = ""
        self.use_cache = use_cache
        self._stamp = 0
        if path:
            self.open(path)

    def open(self, path: str) -> Table:
        with self.lock:
            if self.table is not None:
                self.table.close()
            self.table = Table(path, use_cache=self.use_cache)
            self.error = ""
            return self.table

    def require(self) -> Table:
        if self.table is None:
            raise ValueError("no file is open")
        return self.table

    def state(self) -> dict:
        """A snapshot of everything the UI mirrors.

        Every snapshot carries an increasing ``stamp`` so the client can drop a
        reply that overtook a newer one -- a slow job finishing after the user
        already changed the view, say.
        """
        with self.lock:
            self._stamp += 1
            stamp = self._stamp
        table = self.table
        if table is None:
            return {"open": False, "stamp": stamp, "os": os.name, "sep": os.sep,
                    "error": self.error}
        return {
            "open": True,
            "stamp": stamp,
            "os": os.name,
            "sep": os.sep,
            "path": table.path,
            "name": os.path.basename(table.path),
            "size": table.index.size,
            "base_rows": table.base_count,
            "rows": table.row_count,
            "columns": [c.as_dict() for c in table.columns],
            "dialect": table.dialect.as_dict(),
            "index_mapped": table.index.mapped,
            "dirty": table.dirty,
            "undo": len(table.undo_stack),
            "redo": len(table.redo_stack),
            "view": table.view is not None,
            "view_label": table.view_label,
            "view_rows": len(table.view) if table.view is not None else None,
            "edited_rows": len(table.edits) + len(table.new_rows),
            "marks": table.mark_count,
            "deleted_rows": table.seq.deleted_count,
        }


def _conditions(payload: dict) -> list[ops.Condition]:
    conds = [ops.Condition.from_dict(c) for c in payload.get("conditions", [])]
    quick = (payload.get("quick") or "").strip()
    if quick:
        conds.append(
            ops.Condition(
                col=None,
                op="regex" if payload.get("quick_regex") else "contains",
                value=quick,
                case_sensitive=bool(payload.get("quick_case")),
            )
        )
    return conds


def _scope_ids(table: Table, payload: dict):
    """`scope: "view"` narrows an operation to the rows currently listed."""
    if payload.get("scope") == "view" and table.view is not None:
        return list(table.view)
    return None


class Api:
    """Request handlers, one method per endpoint name."""

    def __init__(self, session: Session):
        self.session = session

    def dispatch(self, name: str, payload: dict) -> dict:
        handler = getattr(self, f"do_{name}", None)
        if handler is None:
            raise KeyError(name)
        return handler(payload)

    # ---------------------------------------------------------------- basics

    def do_state(self, payload: dict) -> dict:
        with self.session.lock:
            return self.session.state()

    def do_open(self, payload: dict) -> dict:
        path = os.path.expanduser(str(payload.get("path", "")).strip().strip('"'))
        if not path:
            raise ValueError("path is required")
        if not os.path.isfile(path):
            raise ValueError(f"not a file: {path}")
        job = self.session.jobs.start(
            "open",
            lambda job: self._open_job(job, path),
            label=os.path.basename(path),
        )
        return {"job": job.as_dict()}

    def _open_job(self, job: Job, path: str) -> dict:
        table = Table(path, use_cache=self.session.use_cache, progress=job.progress)
        with self.session.lock:
            if self.session.table is not None:
                self.session.table.close()  # release the previous mapping
            self.session.table = table
        return {"rows": table.row_count}

    def do_rows(self, payload: dict) -> dict:
        start = int(payload.get("start", 0))
        count = max(0, min(int(payload.get("count", 100)), 5000))
        with self.session.lock:
            table = self.session.require()
            ids, rows = table.read_range(start, count)
            return {
                "start": start,
                "ids": ids,
                "rows": rows,
                "total": table.row_count,
                "edited": {
                    str(rid): sorted(
                        (table.new_rows if rid < 0 else table.edits).get(rid, {}).keys()
                    )
                    for rid in ids
                    if (rid < 0 or rid in table.edits)
                },
                "marked": [rid for rid in ids if table.is_marked(rid)],
            }

    def do_row(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            rid = int(payload["rid"])
            return {"rid": rid, "row": table.read_row(rid)}

    def do_position(self, payload: dict) -> dict:
        """Display position of a row id, or -1 when the filter hides it."""
        rid = int(payload["rid"])
        with self.session.lock:
            table = self.session.require()
            if table.view is not None:
                return {"rid": rid, "pos": _index_of(table.view, rid)}
            return {"rid": rid, "pos": table.seq.position_of(rid)}

    # ----------------------------------------------------------------- edits

    def do_set_cells(self, payload: dict) -> dict:
        cells = [
            (int(c["rid"]), int(c["cid"]), str(c["value"]))
            for c in payload.get("cells", [])
        ]
        with self.session.lock:
            table = self.session.require()
            changed = table.set_cells(cells, label=payload.get("label", "edit"))
            return {"changed": changed, "state": self.session.state()}

    def do_insert_row(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            count = max(1, min(int(payload.get("count", 1)), 1000))
            pos = int(payload.get("pos", table.row_count))
            values = payload.get("values")
            ids = [table.insert_row(pos + i, values) for i in range(count)]
            return {"ids": ids, "state": self.session.state()}

    def do_duplicate_rows(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            ids = [int(i) for i in payload.get("ids", [])]
            created = []
            for rid in ids:
                if table.view is not None:
                    pos = _index_of(table.view, rid)
                else:
                    pos = table.seq.position_of(rid)
                if pos < 0:
                    continue
                created.append(table.insert_row(pos + 1, table.read_row(rid)))
            return {"ids": created, "state": self.session.state()}

    def do_delete_rows(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            removed = table.delete_rows([int(i) for i in payload.get("ids", [])])
            return {"removed": removed, "state": self.session.state()}

    def do_column(self, payload: dict) -> dict:
        action = payload.get("action")
        with self.session.lock:
            table = self.session.require()
            if action == "rename":
                table.rename_column(int(payload["cid"]), str(payload["name"]))
            elif action == "add":
                table.add_column(str(payload.get("name", "new_column")), payload.get("at"))
            elif action == "delete":
                table.delete_column(int(payload["cid"]))
            elif action == "move":
                table.move_column(int(payload["cid"]), int(payload["to"]))
            else:
                raise ValueError(f"unknown column action: {action}")
            return {"state": self.session.state()}

    def do_undo(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            label = table.undo()
            return {"label": label, "state": self.session.state()}

    def do_redo(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            label = table.redo()
            return {"label": label, "state": self.session.state()}

    # ------------------------------------------------------------- bookmarks

    def do_mark(self, payload: dict) -> dict:
        """Toggle (or set) the bookmark on one row."""
        with self.session.lock:
            table = self.session.require()
            rid = int(payload["rid"])
            on = payload.get("on")
            marked = table.set_mark(rid, None if on is None else bool(on))
            return {"rid": rid, "marked": marked, "count": table.mark_count}

    def do_mark_ids(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            changed = table.mark_ids(
                [int(i) for i in payload.get("ids", [])], on=bool(payload.get("on", True))
            )
            return {"changed": changed, "count": table.mark_count}

    def do_mark_search(self, payload: dict) -> dict:
        """Bookmark every row matching a search -- Notepad++'s "Mark All"."""
        conditions = _conditions(payload)
        match_all = payload.get("match_all", True)
        replace = bool(payload.get("replace"))
        scope_view = payload.get("scope") == "view"

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                if not conditions:
                    raise ValueError("검색어나 조건이 필요합니다")
                base = list(table.view) if (scope_view and table.view is not None) else None
                ids = ops.run_filter(
                    table, conditions, match_all=match_all, base_ids=base, progress=job.progress
                )
                if replace:
                    table.clear_marks()
                added = table.mark_ids(ids)
                return {"matched": len(ids), "added": added, "count": table.mark_count,
                        "state": self.session.state()}

        return {"job": self.session.jobs.start("mark_search", run, "책갈피 표시").as_dict()}

    def do_marks_invert(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            table.invert_marks()
            return {"count": table.mark_count, "state": self.session.state()}

    def do_marks_clear(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            table.clear_marks()
            return {"count": 0, "state": self.session.state()}

    def do_marks_filter(self, payload: dict) -> dict:
        """Show only bookmarked rows."""
        with self.session.lock:
            table = self.session.require()
            ids = table.marked_ids()
            if not len(ids):
                raise ValueError("책갈피가 없습니다")
            table.view = ids
            table.view_label = "책갈피"
            return {"rows": len(ids), "state": self.session.state()}

    def do_marks_delete(self, payload: dict) -> dict:
        """Delete bookmarked rows, or with ``keep`` everything except them."""
        keep = bool(payload.get("keep"))

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                removed = table.delete_marked(keep=keep)
                return {"removed": removed, "state": self.session.state()}

        label = "책갈피 외 행 삭제" if keep else "책갈피 행 삭제"
        return {"job": self.session.jobs.start("marks_delete", run, label).as_dict()}

    def do_marks_next(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            pos = int(payload.get("pos", 0))
            forward = bool(payload.get("forward", True))
            return {"pos": table.find_mark(pos, forward=forward)}

    # --------------------------------------------------------------- queries

    def do_filter(self, payload: dict) -> dict:
        conditions = _conditions(payload)
        match_all = payload.get("match_all", True)
        label = payload.get("label", "")
        scope_view = payload.get("scope") == "view"

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                if scope_view and table.view is not None:
                    base = list(table.view)  # narrow down inside the current result
                else:
                    table.view = None        # a fresh filter always scans the file
                    table.view_label = ""
                    base = None
                if not conditions:
                    return {"rows": table.row_count, "filtered": False}
                ids = ops.run_filter(
                    table, conditions, match_all=match_all, base_ids=base,
                    progress=job.progress,
                )
                table.view = ids
                table.view_label = label
                return {"rows": len(ids), "filtered": True}

        return {"job": self.session.jobs.start("filter", run, label or "filter").as_dict()}

    def do_clear_view(self, payload: dict) -> dict:
        with self.session.lock:
            table = self.session.require()
            table.view = None
            table.view_label = ""
            return {"state": self.session.state()}

    def do_sort(self, payload: dict) -> dict:
        cid = int(payload["cid"])
        desc = bool(payload.get("desc"))

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                ids = ops.sort_ids(
                    table, cid, descending=desc,
                    ids=list(table.view) if table.view is not None else None,
                    progress=job.progress,
                )
                table.view = ids
                if not table.view_label:
                    table.view_label = "sorted"
                return {"rows": len(ids)}

        return {"job": self.session.jobs.start("sort", run, "sort").as_dict()}

    def do_stats(self, payload: dict) -> dict:
        cid = int(payload["cid"])
        top_k = int(payload.get("top", 25))

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                stats = ops.column_stats(
                    table, cid, top_k=top_k, ids=_scope_ids(table, payload),
                    progress=job.progress,
                )
                return stats.as_dict()

        return {"job": self.session.jobs.start("stats", run, "stats").as_dict()}

    def do_find(self, payload: dict) -> dict:
        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                hits = ops.find_matches(
                    table,
                    str(payload.get("needle", "")),
                    col_id=payload.get("cid"),
                    use_regex=bool(payload.get("regex")),
                    case_sensitive=bool(payload.get("case")),
                    whole_cell=bool(payload.get("whole")),
                    ids=_scope_ids(table, payload),
                    limit=int(payload.get("limit", 5000)),
                    progress=job.progress,
                )
                return {"hits": [{"rid": r, "col": c} for r, c in hits]}

        return {"job": self.session.jobs.start("find", run, "find").as_dict()}

    def do_replace_all(self, payload: dict) -> dict:
        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                changed = ops.replace_all(
                    table,
                    str(payload.get("needle", "")),
                    str(payload.get("replacement", "")),
                    col_id=payload.get("cid"),
                    use_regex=bool(payload.get("regex")),
                    case_sensitive=bool(payload.get("case")),
                    whole_cell=bool(payload.get("whole")),
                    ids=_scope_ids(table, payload),
                    progress=job.progress,
                )
                return {"changed": changed, "state": self.session.state()}

        return {"job": self.session.jobs.start("replace", run, "replace").as_dict()}

    def do_dedupe(self, payload: dict) -> dict:
        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                dupes = ops.dedupe(
                    table, col_ids=payload.get("cids"), ids=_scope_ids(table, payload),
                    progress=job.progress,
                )
                removed = table.delete_rows(dupes) if payload.get("apply", True) else 0
                return {"duplicates": len(dupes), "removed": removed,
                        "state": self.session.state()}

        return {"job": self.session.jobs.start("dedupe", run, "dedupe").as_dict()}

    def do_trim(self, payload: dict) -> dict:
        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                changed = ops.trim_whitespace(
                    table, col_id=payload.get("cid"), ids=_scope_ids(table, payload),
                    progress=job.progress,
                )
                return {"changed": changed, "state": self.session.state()}

        return {"job": self.session.jobs.start("trim", run, "trim").as_dict()}

    # ------------------------------------------------------------------ save

    def do_save(self, payload: dict) -> dict:
        backup = bool(payload.get("backup", True))

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                written = table.save(backup=backup, progress=job.progress)
                return {"written": written, "state": self.session.state()}

        return {"job": self.session.jobs.start("save", run, "save").as_dict()}

    def do_save_as(self, payload: dict) -> dict:
        path = os.path.expanduser(str(payload.get("path", "")).strip().strip('"'))
        if not path:
            raise ValueError("path is required")
        scope = payload.get("scope") or ("view" if payload.get("view_only") else "all")
        encoding = payload.get("encoding") or None
        delimiter = payload.get("delimiter") or None
        newline = {"crlf": "\r\n", "lf": "\n"}.get(payload.get("newline") or "", None)

        def run(job: Job) -> dict:
            with self.session.lock:
                table = self.session.require()
                if scope == "marks":
                    ids = list(table.iter_marked())
                    if not ids:
                        raise ValueError("책갈피가 없습니다")
                    written = table.write_to(
                        path, ids=ids, delimiter=delimiter, encoding=encoding,
                        newline=newline, progress=job.progress,
                    )
                else:
                    written = table.save_as(
                        path, view_only=(scope == "view"), delimiter=delimiter,
                        encoding=encoding, newline=newline, progress=job.progress,
                    )
                return {"written": written, "path": path, "scope": scope}

        return {"job": self.session.jobs.start("save_as", run, os.path.basename(path)).as_dict()}

    # ------------------------------------------------------------------ jobs

    def do_job(self, payload: dict) -> dict:
        job = self.session.jobs.get(str(payload.get("id")))
        if job is None:
            raise ValueError("unknown job")
        return {"job": job.as_dict()}

    def do_cancel_job(self, payload: dict) -> dict:
        job = self.session.jobs.get(str(payload.get("id")))
        if job is not None:
            job.cancel()
        return {"ok": True}

    # ------------------------------------------------------------ file browse

    def do_browse(self, payload: dict) -> dict:
        """Directory listing, so the UI can pick files by real path."""
        path = os.path.expanduser(str(payload.get("path", "")) or os.getcwd())
        if os.path.isfile(path):
            path = os.path.dirname(path)
        path = os.path.abspath(path)
        entries = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir()
                        size = 0 if is_dir else entry.stat().st_size
                    except OSError:
                        continue
                    if not is_dir and os.path.splitext(entry.name)[1].lower() not in (
                        ".csv", ".tsv", ".txt", ".log", ".psv", ".dat",
                    ):
                        continue
                    entries.append({"name": entry.name, "dir": is_dir, "size": size,
                                    "path": os.path.join(path, entry.name)})
        except OSError as exc:
            raise ValueError(str(exc))
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {
            "path": path,
            "parent": os.path.dirname(path) if os.path.dirname(path) != path else None,
            "entries": entries,
            "roots": _roots(),
        }


def _index_of(view: array, rid: int) -> int:
    try:
        return view.index(rid)
    except ValueError:
        return -1


def _roots() -> list[str]:
    """Home, cwd, and on Windows every drive letter that exists."""
    roots = [os.path.expanduser("~"), os.getcwd()]
    if os.name == "nt":
        roots += [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    else:
        roots += ["/"]
        for extra in ("/Volumes", os.path.expanduser("~/Desktop"), os.path.expanduser("~/Downloads")):
            if os.path.isdir(extra):
                roots.append(extra)
    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "csvopt"
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _authorised(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host and host not in LOCAL_HOSTS:
            return False  # blocks DNS-rebinding from a page in another tab
        token = self.headers.get("X-Csvopt-Token") or ""
        if not token:
            token = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return hmac.compare_digest(token, self.server.token)

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # --------------------------------------------------------------- routing

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route in ("/", "/index.html"):
            if not self._authorised():
                self._send(403, b"invalid or missing token", "text/plain; charset=utf-8")
                return
            self._serve_static("index.html")
            return
        if route.startswith("/static/"):
            self._serve_static(posixpath.normpath(route[len("/static/"):]).lstrip("/."))
            return
        if route == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if not route.startswith("/api/"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if not self._authorised():
            self._json(403, {"error": "invalid or missing token"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": "request too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return
        name = route[len("/api/"):]
        try:
            self._json(200, self.server.api.dispatch(name, payload))
        except KeyError:
            self._json(404, {"error": f"unknown endpoint: {name}"})
        except Aborted:
            self._json(409, {"error": "cancelled"})
        except (SaveError, ValueError, OSError, TypeError) as exc:
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

    def _serve_static(self, rel: str) -> None:
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, array):
        return list(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


class CsvOptServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, session: Session, token: str, verbose: bool = False):
        super().__init__(addr, Handler)
        self.session = session
        self.api = Api(session)
        self.token = token
        self.verbose = verbose


def serve(
    path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    verbose: bool = False,
    use_cache: bool = True,
) -> CsvOptServer:
    session = Session(path, use_cache=use_cache)
    token = secrets.token_urlsafe(24)
    server = CsvOptServer((host, port), session, token, verbose=verbose)
    url = f"http://{host}:{server.server_address[1]}/?t={token}"
    server.url = url
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    return server
