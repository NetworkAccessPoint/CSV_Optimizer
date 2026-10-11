"""Turn docs/사용설명서.md into something you can hand over: one HTML file and a PDF.

    python3 tools/make_manual.py            # HTML (+ PDF when Playwright is around)
    python3 tools/make_manual.py --no-pdf

The HTML carries its images inline, so the single file is the whole manual --
no folder of screenshots travelling beside it.  The PDF is that same page
printed by Chromium, which is the only renderer we can count on having the
fonts the screenshots were taken with.

Needs the `markdown` package (pip install markdown); the PDF step additionally
needs Playwright with its Chromium.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "사용설명서.md"
TITLE = "CSV Insight 사용설명서"

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
    import markdown

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


def write_pdf(html_path: pathlib.Path, pdf_path: pathlib.Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  건너뜀: Playwright 가 없어 PDF 는 만들지 않습니다.", file=sys.stderr)
        return False
    chrome = os.environ.get("CSVINSIGHT_CHROME")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome) if chrome else pw.chromium.launch()
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
                            'padding:0 14mm;">CSV Insight 사용설명서</div>',
            footer_template='<div style="font-size:7pt;color:#9aa3ad;width:100%;'
                            'text-align:center;"><span class="pageNumber"></span>'
                            ' / <span class="totalPages"></span></div>',
        )
        browser.close()
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="사용설명서 HTML/PDF 생성")
    ap.add_argument("--source", default=str(SOURCE))
    ap.add_argument("--out-dir", default=str(ROOT / "docs"))
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    source = pathlib.Path(args.source).resolve()
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

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
