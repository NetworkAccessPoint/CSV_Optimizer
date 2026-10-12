"""사용설명서(마크다운)를 넘겨줄 수 있는 형태로 만든다: HTML 한 장과 PDF.

    python3 make_manual.py              # 같은 폴더의 사용설명서.md 를 찾는다
    python3 make_manual.py --no-pdf     # HTML 만
    python3 make_manual.py --source 다른이름.md

HTML 은 그림을 본문에 박아 넣으므로 그 파일 하나가 설명서 전부다(그림 폴더를
같이 들고 다닐 필요가 없다). PDF 는 그 페이지를 크롬으로 그대로 인쇄한 것이다.

필요한 것: `markdown` 패키지 (pip install markdown).
PDF 는 Playwright 가 있으면 그것을, 없으면 PC 에 깔린 Chrome/Edge/Chromium 을
찾아 쓴다. 둘 다 없으면 HTML 만 만들고, 브라우저에서 인쇄(Ctrl/Cmd+P →
PDF 로 저장)하라고 알려준다.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
TITLE = "CSV Insight 사용설명서"
BASENAME = "사용설명서.md"

# Beside the script (how the handover package is laid out), then the
# repository layout (tools/ next to docs/).
SOURCE_CANDIDATES = (HERE / BASENAME, HERE.parent / "docs" / BASENAME)

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 48px 56px 64px; max-width: 960px;
  font-family: "Pretendard", "Apple SD Gothic Neo", "Malgun Gothic",
               "Noto Sans KR", system-ui, -apple-system, sans-serif;
  font-size: 15px; line-height: 1.75; color: #1a1d21; background: #fff;
  word-break: keep-all;
}
h1 {
  font-size: 30px; margin: 0 0 4px; letter-spacing: -0.02em;
  padding-bottom: 18px; border-bottom: 3px solid #1a1d21;
}
h2 {
  font-size: 21px; margin: 52px 0 14px; padding-top: 18px;
  border-top: 1px solid #e3e6ea; letter-spacing: -0.01em;
  break-after: avoid; page-break-after: avoid;
}
h1 + h2, body > p:first-of-type + h2 { border-top: 0; padding-top: 0; }
h3 {
  font-size: 16px; margin: 28px 0 10px; color: #0b5ed7;
  break-after: avoid; page-break-after: avoid;
}
p, li { margin: 8px 0; }
ul, ol { padding-left: 22px; }
a { color: #0b5ed7; }
hr { display: none; }  /* h2 draws its own rule */
code {
  font-family: "SFMono-Regular", Consolas, "DejaVu Sans Mono", monospace;
  font-size: 0.88em; background: #f1f3f5; border: 1px solid #e3e6ea;
  border-radius: 4px; padding: 1px 5px; white-space: nowrap;
}
pre {
  background: #f7f8fa; border: 1px solid #e3e6ea; border-radius: 6px;
  padding: 14px 16px; overflow-x: auto; line-height: 1.6;
  break-inside: avoid; page-break-inside: avoid;
}
pre code { background: none; border: 0; padding: 0; white-space: pre; font-size: 12.5px; }
blockquote {
  margin: 16px 0; padding: 12px 16px; background: #fff8e6;
  border-left: 3px solid #f0b429; border-radius: 0 6px 6px 0;
}
blockquote p { margin: 4px 0; }
table {
  border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 14px;
  break-inside: avoid; page-break-inside: avoid;
}
th, td {
  border: 1px solid #dfe3e8; padding: 7px 10px; text-align: left;
  vertical-align: top;
}
th { background: #f1f3f5; font-weight: 600; white-space: nowrap; }
td code, th code { white-space: normal; }
img {
  display: block; max-width: 100%; margin: 16px 0;
  border: 1px solid #dfe3e8; border-radius: 6px;
  break-inside: avoid; page-break-inside: avoid;
}
.lead { color: #5b6672; font-size: 14px; margin-top: 10px; }
@page { size: A4; margin: 16mm 14mm 18mm; }
@media print {
  body { padding: 0; max-width: none; font-size: 12.5px; }
  h1 { font-size: 24px; }
  h2 { font-size: 17px; margin-top: 30px; }
  h3 { font-size: 14px; }
  table { font-size: 11px; }
  pre code { font-size: 10.5px; }
  img { max-height: 118mm; object-fit: contain; object-position: left top; }
}
"""


def inline_images(html: str, base: pathlib.Path) -> str:
    """Replace every <img src="relative/path"> with a data: URI."""

    def swap(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith(("data:", "http://", "https://")):
            return match.group(0)
        path = (base / src).resolve()
        if not path.is_file():
            print(f"  경고: 그림을 찾지 못했습니다 - {src}", file=sys.stderr)
            return match.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'src="data:{mime};base64,{data}"'

    return re.sub(r'src="([^"]+)"', swap, html)


def build_html(source: pathlib.Path) -> str:
    try:
        import markdown
    except ImportError:
        raise SystemExit(
            "markdown 패키지가 필요합니다.\n"
            f"  {sys.executable} -m pip install markdown"
        )

    text = source.read_text(encoding="utf-8")
    body = markdown.markdown(
        text, extensions=["tables", "fenced_code", "sane_lists", "attr_list"]
    )
    body = inline_images(body, source.parent)
    return (
        "<!doctype html>\n<html lang=\"ko\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{TITLE}</title>\n<style>{CSS}</style>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )


def _pdf_with_playwright(html_path: pathlib.Path, pdf_path: pathlib.Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    chrome = os.environ.get("CSVINSIGHT_CHROME")
    try:
        with sync_playwright() as pw:
            browser = (
                pw.chromium.launch(executable_path=chrome) if chrome else pw.chromium.launch()
            )
            page = browser.new_page()
            page.goto(html_path.as_uri())
            page.emulate_media(media="print")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
                display_header_footer=True,
                header_template='<div style="font-size:7pt;color:#9aa3ad;width:100%;'
                                'padding:0 14mm;">' + TITLE + '</div>',
                footer_template='<div style="font-size:7pt;color:#9aa3ad;width:100%;'
                                'text-align:center;"><span class="pageNumber"></span>'
                                ' / <span class="totalPages"></span></div>',
            )
            browser.close()
    except Exception as exc:  # no browser downloaded, sandbox, ...
        print(f"  Playwright 로는 실패했습니다 ({exc.__class__.__name__}). 설치된 크롬을 찾아봅니다.",
              file=sys.stderr)
        return False
    return True


# Where Chrome-family browsers normally live, per platform.
CHROME_PATHS = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ),
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
}
CHROME_COMMANDS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
)


def find_chrome() -> str:
    """A Chrome-family executable to print with, or '' when there is none."""
    given = os.environ.get("CSVINSIGHT_CHROME")
    if given and pathlib.Path(given).exists():
        return given
    for path in CHROME_PATHS.get(sys.platform, ()):
        if pathlib.Path(path).exists():
            return path
    for name in CHROME_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _pdf_with_chrome(html_path: pathlib.Path, pdf_path: pathlib.Path) -> bool:
    html_path, pdf_path = html_path.resolve(), pdf_path.resolve()
    chrome = find_chrome()
    if not chrome:
        return False
    print(f"  크롬으로 인쇄합니다: {chrome}")
    # --headless=new is the current spelling; older builds only know --headless.
    for headless in ("--headless=new", "--headless"):
        result = subprocess.run(
            [
                chrome, headless, "--disable-gpu", "--no-sandbox",
                "--no-first-run", "--disable-background-networking",
                "--disable-component-update", "--no-default-browser-check",
                "--no-pdf-header-footer",
                "--virtual-time-budget=20000",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True
        last = result.stderr.strip().splitlines()[-1:] or [""]
    print(f"  크롬 인쇄가 실패했습니다: {last[0]}", file=sys.stderr)
    return False


def write_pdf(html_path: pathlib.Path, pdf_path: pathlib.Path) -> bool:
    # as_uri() below needs absolute paths, whoever called us.
    html_path, pdf_path = html_path.resolve(), pdf_path.resolve()
    if _pdf_with_playwright(html_path, pdf_path):
        return True
    if _pdf_with_chrome(html_path, pdf_path):
        return True
    print(
        "  PDF 는 건너뜁니다 - Playwright 도, 설치된 Chrome/Edge 도 찾지 못했습니다.\n"
        "  만들어진 HTML 을 브라우저로 열고 인쇄(Ctrl/Cmd+P) → 'PDF로 저장' 하시면 같은 결과입니다.\n"
        "  (크롬 경로를 직접 지정하려면 환경변수 CSVINSIGHT_CHROME)",
        file=sys.stderr,
    )
    return False


def resolve_source(given: str) -> pathlib.Path:
    if given:
        path = pathlib.Path(given).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"원본을 찾지 못했습니다: {path}")
        return path
    for candidate in SOURCE_CANDIDATES:
        if candidate.is_file():
            return candidate.resolve()
    looked = "\n".join(f"  {c}" for c in SOURCE_CANDIDATES)
    raise SystemExit(f"{BASENAME} 을 찾지 못했습니다. 찾아본 곳:\n{looked}\n--source 로 지정해 주세요.")


def main() -> int:
    ap = argparse.ArgumentParser(description="사용설명서 HTML/PDF 생성")
    ap.add_argument("--source", default="", help=f"마크다운 원본 (기본: 스크립트 옆의 {BASENAME})")
    ap.add_argument("--out-dir", default="", help="결과를 둘 폴더 (기본: 원본과 같은 폴더)")
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    source = resolve_source(args.source)
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve() if args.out_dir else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"원본 : {source}")

    html_path = out_dir / "CSV_Insight_사용설명서.html"
    html_path.write_text(build_html(source), encoding="utf-8")
    print(f"HTML : {html_path} ({html_path.stat().st_size / 1024 / 1024:.1f} MB)")

    if not args.no_pdf:
        pdf_path = out_dir / "CSV_Insight_사용설명서.pdf"
        if write_pdf(html_path, pdf_path):
            print(f"PDF  : {pdf_path} ({pdf_path.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
