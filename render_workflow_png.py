"""Render WORKFLOW.md mermaid blocks to WORKFLOW.png (and per-diagram PNGs)."""
from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
WORKFLOW_MD = ROOT / "WORKFLOW.md"
OUT_DIR = ROOT / "docs" / "workflow"
COMBINED_PNG = ROOT / "WORKFLOW.png"

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>
    body {{ margin: 24px; background: #fff; font-family: Segoe UI, Arial, sans-serif; }}
    h2 {{ color: #1a1a2e; margin: 32px 0 12px; font-size: 18px; }}
    .diagram {{ margin-bottom: 24px; }}
    .mermaid {{ display: inline-block; }}
  </style>
</head>
<body>
{body}
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: "default",
    flowchart: {{ useMaxWidth: false, htmlLabels: true }},
    sequence: {{ useMaxWidth: false }}
  }});
</script>
</body>
</html>
"""


def extract_diagrams(md_text: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"^##\s+(.+?)\s*\n+```mermaid\n(.*?)\n```",
        re.MULTILINE | re.DOTALL,
    )
    return [(title.strip(), code.strip()) for title, code in pattern.findall(md_text)]


def build_html(diagrams: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for title, code in diagrams:
        parts.append(f'<h2>{title}</h2>')
        parts.append(f'<div class="diagram"><pre class="mermaid">\n{code}\n</pre></div>')
    return HTML_TEMPLATE.format(body="\n".join(parts))


def render_png(html: str, out_path: Path, width: int = 1400) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 800})
        page.set_content(html, wait_until="networkidle")
        page.wait_for_selector(".mermaid svg", timeout=60000)
        page.wait_for_timeout(1500)
        height = page.evaluate("() => document.body.scrollHeight")
        page.set_viewport_size({"width": width, "height": max(height + 40, 800)})
        page.locator("body").screenshot(path=str(out_path), type="png")
        browser.close()


def render_single(title: str, code: str, out_path: Path) -> None:
    html = HTML_TEMPLATE.format(
        body=f'<h2>{title}</h2><div class="diagram"><pre class="mermaid">\n{code}\n</pre></div>'
    )
    render_png(html, out_path)


def main() -> None:
    md = WORKFLOW_MD.read_text(encoding="utf-8")
    diagrams = extract_diagrams(md)
    if not diagrams:
        raise SystemExit("No mermaid diagrams found in WORKFLOW.md")

    combined_html = build_html(diagrams)
    render_png(combined_html, COMBINED_PNG, width=1600)
    print(f"Wrote {COMBINED_PNG}")

    for i, (title, code) in enumerate(diagrams, start=1):
        slug = re.sub(r"[^\w]+", "_", title.lower()).strip("_")[:50]
        out = OUT_DIR / f"{i:02d}_{slug}.png"
        render_single(title, code, out)
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
