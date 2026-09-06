"""Command line entry point.

    csvopt app.csv                 # open the editor in a browser
    csvopt info app.csv            # print what was detected about the file
    csvopt grep app.csv -w level=ERROR -o errors.csv
    csvopt convert app.csv -o out.csv --encoding utf-8-sig --newline crlf
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .index import sniff
from .ops import Condition, run_filter
from .table import Table


def _progress_printer(label: str):
    last = [0.0]

    def report(done: int, total: int) -> bool:
        now = time.time()
        if now - last[0] > 0.25 and total:
            last[0] = now
            pct = done * 100 // max(1, total)
            sys.stderr.write(f"\r{label} {pct:3d}%")
            sys.stderr.flush()
        return True

    return report


def _finish(label: str) -> None:
    sys.stderr.write(f"\r{label} 100%\n")
    sys.stderr.flush()


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    path = os.path.abspath(args.path) if args.path else None
    if path and not os.path.isfile(path):
        print(f"파일을 찾을 수 없습니다: {path}", file=sys.stderr)
        return 2
    server = serve(
        path=path,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        verbose=args.verbose,
        use_cache=not args.no_cache,
    )
    print(f"csvopt {__version__} — {server.url}")
    print("브라우저가 열리지 않으면 위 주소를 직접 붙여넣으세요. 종료하려면 Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    dialect = sniff(args.path)
    table = Table(args.path, progress=_progress_printer("인덱싱"))
    _finish("인덱싱")
    size = os.path.getsize(args.path)
    print(f"경로      : {os.path.abspath(args.path)}")
    print(f"크기      : {size:,} bytes")
    print(f"행        : {table.row_count:,} (헤더 {'있음' if dialect.has_header else '없음'})")
    print(f"인코딩    : {dialect.encoding}")
    print(f"구분자    : {dialect.delimiter!r}")
    print(f"줄바꿈    : {'CRLF' if dialect.newline == chr(13) + chr(10) else 'LF'}")
    print(f"열 ({len(table.columns)}): " + ", ".join(c.name for c in table.columns))
    for _rid, row in zip(range(args.rows), table.iter_all()):
        print("  " + " | ".join(row[1])[:200])
    return 0


def _conditions_from(args: argparse.Namespace, table: Table) -> list[Condition]:
    conds: list[Condition] = []
    by_name = {c.name: c.id for c in table.columns}
    for spec in args.where or []:
        if "=" not in spec:
            raise SystemExit(f"--where 형식은 열=값 입니다: {spec}")
        name, value = spec.split("=", 1)
        name = name.strip()
        if name not in by_name:
            raise SystemExit(f"열을 찾을 수 없습니다: {name} (가능: {', '.join(by_name)})")
        conds.append(Condition(col=by_name[name], op=args.op, value=value))
    if args.contains:
        conds.append(Condition(col=None, op="contains", value=args.contains))
    if args.regex:
        conds.append(Condition(col=None, op="regex", value=args.regex))
    return conds


def cmd_grep(args: argparse.Namespace) -> int:
    table = Table(args.path, progress=_progress_printer("인덱싱"))
    _finish("인덱싱")
    conds = _conditions_from(args, table)
    ids = run_filter(table, conds, match_all=not args.any, progress=_progress_printer("검색"))
    _finish("검색")
    print(f"{len(ids):,}행이 일치했습니다.", file=sys.stderr)
    if args.output:
        table.view = ids
        written = table.write_to(args.output, ids=list(ids))
        print(f"{written:,}행을 {args.output} 에 저장했습니다.", file=sys.stderr)
    else:
        import csv as _csv

        writer = _csv.writer(sys.stdout, delimiter=table.dialect.delimiter, lineterminator="\n")
        writer.writerow([c.name for c in table.columns])
        for _rid, row in table.iter_all(ids=list(ids)):
            writer.writerow(row)
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    table = Table(args.path, progress=_progress_printer("인덱싱"))
    _finish("인덱싱")
    newline = {"crlf": "\r\n", "lf": "\n"}.get(args.newline or "")
    written = table.write_to(
        args.output,
        encoding=args.encoding,
        delimiter=args.delimiter,
        newline=newline,
        progress=_progress_printer("변환"),
    )
    _finish("변환")
    print(f"{written:,}행을 {args.output} 에 저장했습니다.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="csvopt",
        description="대용량 CSV/로그 뷰어·편집기 (Windows / macOS / Linux)",
    )
    ap.add_argument("--version", action="version", version=f"csvopt {__version__}")
    sub = ap.add_subparsers(dest="command")

    p_open = sub.add_parser("open", help="편집기를 브라우저에서 열기 (기본 동작)")
    p_open.add_argument("path", nargs="?", help="열 CSV/로그 파일")
    p_open.add_argument("--port", type=int, default=0, help="포트 (기본: 임의)")
    p_open.add_argument("--host", default="127.0.0.1", help="바인드 주소 (기본: 127.0.0.1)")
    p_open.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    p_open.add_argument("-v", "--verbose", action="store_true", help="요청 로그 출력")
    p_open.add_argument(
        "--no-cache", action="store_true",
        help="인덱스 캐시(.csvidx)를 만들지 않음 (읽기 전용/네트워크 드라이브)",
    )
    p_open.set_defaults(func=cmd_serve)

    p_info = sub.add_parser("info", help="파일 정보와 미리보기 출력")
    p_info.add_argument("path")
    p_info.add_argument("--rows", type=int, default=5, help="미리 볼 행 수")
    p_info.set_defaults(func=cmd_info)

    p_grep = sub.add_parser("grep", help="조건에 맞는 행만 추출")
    p_grep.add_argument("path")
    p_grep.add_argument("-w", "--where", action="append", help="열=값 (여러 번 사용 가능)")
    p_grep.add_argument("--op", default="equals", help="--where 비교 방식 (equals/contains/gt/...)")
    p_grep.add_argument("-c", "--contains", help="모든 열에서 부분 문자열 검색")
    p_grep.add_argument("-e", "--regex", help="모든 열에서 정규식 검색")
    p_grep.add_argument("--any", action="store_true", help="조건을 OR로 결합")
    p_grep.add_argument("-o", "--output", help="결과를 저장할 파일 (없으면 표준 출력)")
    p_grep.set_defaults(func=cmd_grep)

    p_conv = sub.add_parser("convert", help="인코딩/구분자/줄바꿈 변환")
    p_conv.add_argument("path")
    p_conv.add_argument("-o", "--output", required=True)
    p_conv.add_argument("--encoding", help="예: utf-8, utf-8-sig, cp949")
    p_conv.add_argument("--delimiter", help="예: , 또는 ; 또는 \\t")
    p_conv.add_argument("--newline", choices=["crlf", "lf"])
    p_conv.set_defaults(func=cmd_convert)
    return ap


COMMANDS = ("open", "info", "grep", "convert")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `csvopt app.csv` and `csvopt --port 9000 app.csv` both mean `csvopt open ...`.
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv = ["open"] + argv
    args = build_parser().parse_args(argv)
    if getattr(args, "delimiter", None) == "\\t":
        args.delimiter = "\t"
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
