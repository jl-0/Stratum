# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.45"]
# ///
"""Open .excalidraw scenes in the real excalidraw.com editor and report whether they load.

A JSON-schema or `restore()` check in Node is not the same thing as the website accepting the
file, so this drives a headless Chromium: it drops each file onto the canvas exactly as a user
would, then reads back what the editor actually holds.

    uv run scripts/check-excalidraw.py examples/emit-us-west-2025/algorithm.excalidraw

First run needs the browser: `uvx playwright install chromium`.
Add `--screenshots DIR` before the files to save what the editor rendered.
Exit status is non-zero if any file fails to load or loses elements.
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://excalidraw.com/"

# Build a File from the text and fire the same dragenter/dragover/drop sequence a real drag does.
DROP_JS = """
async ([name, text]) => {
  const file = new File([text], name, { type: "application/vnd.excalidraw+json" });
  const dt = new DataTransfer();
  dt.items.add(file);
  const target = document.querySelector(".excalidraw-container") || document.querySelector(".excalidraw");
  const r = target.getBoundingClientRect();
  const at = { clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
  for (const type of ["dragenter", "dragover", "drop"]) {
    target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt, ...at }));
  }
}
"""

# excalidraw.com persists the live scene to localStorage; that is the editor's own view of it.
READ_JS = """
() => {
  const raw = localStorage.getItem("excalidraw");
  return raw ? JSON.parse(raw).filter(e => !e.isDeleted).length : null;
}
"""


def check(page, path: Path, shots: Path | None) -> bool:
    text = path.read_text(encoding="utf-8")
    try:
        expected = sum(1 for e in json.loads(text)["elements"] if not e.get("isDeleted"))
    except (ValueError, KeyError, TypeError) as e:
        expected = None  # still drop it: the point is what the editor does with a bad file
        print(f"     not a readable scene: {e}")

    page.goto(URL)
    page.wait_for_selector(".excalidraw", timeout=30_000)
    page.evaluate("() => localStorage.clear()")
    page.reload()
    page.wait_for_selector(".excalidraw", timeout=30_000)

    page.evaluate(DROP_JS, [path.name, text])
    # A dropped scene that replaces a non-empty canvas asks first; ours is empty, but accept anyway.
    confirm = page.locator("button:has-text('Replace')")
    if confirm.count():
        confirm.first.click()

    loaded = None
    for _ in range(40):  # up to ~10s for the scene to settle and be saved
        page.wait_for_timeout(250)
        loaded = page.evaluate(READ_JS)
        if loaded:
            break

    error = page.locator(".ErrorDialog, [role=dialog]:has-text('rror'), .Toast")
    messages = [m.strip() for m in error.all_inner_texts() if m.strip()]

    ok = expected is not None and loaded == expected and not messages
    if shots:
        shot = shots / f"{path.stem}.png"
        page.screenshot(path=shot)
        print(f"     screenshot: {shot}")
    status = "OK  " if ok else "FAIL"
    print(f"{status} {path}: editor holds {loaded} of {expected} elements")
    for m in messages:
        print(f"     editor said: {m}")
    return ok


def main(argv: list[str]) -> int:
    shots = None
    if argv[:1] == ["--screenshots"]:
        shots = Path(argv[1])
        shots.mkdir(parents=True, exist_ok=True)
        argv = argv[2:]
    if not argv:
        print(__doc__)
        return 2
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: print(f"     page error: {e}"))
        results = [check(page, Path(a), shots) for a in argv]
        browser.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
