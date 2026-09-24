# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.45"]
# ///
"""Turn a Mermaid flowchart into native Excalidraw shapes, styled to match algorithm.excalidraw.

Uses Excalidraw's own converter, `@excalidraw/mermaid-to-excalidraw`, which needs a real browser
to lay the chart out, so this runs it in headless Chromium. The result is ordinary rectangles,
diamonds, arrows and bound text - editable in excalidraw.com like anything hand-drawn - written as
a scene at the origin, ready to be positioned inside a larger drawing.

    uv run scripts/mermaid-to-excalidraw.py chart.mmd -o chart.excalidraw [--font-size 14]

Two things the converter gets wrong for this repo, and this script fixes before the shapes are
measured: `<br>` in a label arrives as literal text, and every stroke is hand-drawn. Labels become
real line breaks in Helvetica and strokes become crisp, so boxes are sized for the text they hold.

First run needs the browser: `uvx playwright install chromium`. Check the output with
`scripts/check-excalidraw.py`.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Pinned: a converter or editor release can change layout, and a pinned pair reproduces.
CONVERTER = "https://esm.sh/@excalidraw/mermaid-to-excalidraw@1.1.2"
EDITOR = "https://esm.sh/@excalidraw/excalidraw@0.18.1?deps=react@19.1.0,react-dom@19.1.0"

JS = r"""
async ([src, fontSize, converter, editor]) => {
  const { parseMermaidToExcalidraw } = await import(converter);
  const { convertToExcalidrawElements } = await import(editor);
  const { elements, files } = await parseMermaidToExcalidraw(src, { themeVariables: { fontSize: `${fontSize}px` } });
  for (const e of elements) {
    e.roughness = 0;
    e.strokeWidth = 1;
    if (e.type === "rectangle") e.roundness = { type: 3 };
    for (const t of [e.label, e.type === "text" ? e : null]) {
      if (!t) continue;
      t.text = t.text.replace(/<br\s*\/?>/gi, "\n");
      t.fontFamily = 2;
      t.fontSize = fontSize;
    }
  }
  return { elements: convertToExcalidrawElements(elements, { regenerateIds: true }), files: files || {} };
}
"""


def convert(src: str, font_size: int) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: m.type == "error" and print(f"console: {m.text}", file=sys.stderr))
        page.goto("https://esm.sh/")  # any https origin, so the module imports are allowed
        out = page.evaluate(JS, [src, font_size, CONVERTER, EDITOR])
        browser.close()
    return {"type": "excalidraw", "version": 2, "source": "mermaid-to-excalidraw",
            "elements": out["elements"], "appState": {"viewBackgroundColor": "#ffffff"},
            "files": out["files"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mermaid", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--font-size", type=int, default=16)
    args = ap.parse_args()
    scene = convert(args.mermaid.read_text(encoding="utf-8"), args.font_size)
    args.output.write_text(json.dumps(scene, indent=1), encoding="utf-8")
    print(f"{args.output}: {len(scene['elements'])} elements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
