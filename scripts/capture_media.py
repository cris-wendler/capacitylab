"""Capture README screenshots and a short GIF from a real local mock run.

Requires the media extra: pip install -e ".[media]" && playwright install chromium
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "docs" / "media"
FRAMES = MEDIA / "_frames"
PORT = int(os.environ.get("CAPTURE_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"
VIEWPORT = {"width": 1280, "height": 820}


def wait_for_server(timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("server did not start")


def crop(full: Path, box: tuple[float, float, float, float], out: Path, pad: int = 12, max_height: int = 1100) -> None:
    x, y, w, h = box
    with Image.open(full) as image:
        right = min(image.width, int(x + w + pad))
        bottom = min(image.height, int(y + min(h, max_height) + pad))
        image.crop((max(0, int(x - pad)), max(0, int(y - pad)), right, bottom)).save(out, optimize=True)


def span(page, first: str, last: str) -> tuple[float, float, float, float]:
    """Bounding box (page coordinates) from the top of `first` to the bottom of `last`."""
    return page.evaluate(
        """([a, b]) => {
            const r1 = document.querySelector(a).getBoundingClientRect();
            const r2 = document.querySelector(b).getBoundingClientRect();
            const main = document.querySelector('main').getBoundingClientRect();
            return [main.left, r1.top + window.scrollY, main.width, r2.bottom - r1.top];
        }""",
        [first, last],
    )


def main() -> int:
    MEDIA.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    runs_dir = tempfile.mkdtemp(prefix="capacitylab-media-")
    lab_source = ROOT / "runs" / "lab" / "campaign-overlap-lab.yaml"  # produced by `capacitylab lab run`
    percona_source = ROOT / "runs" / "lab" / "campaign-overlap-lab-percona.yaml"  # produced by `lab run --percona`
    for source in (lab_source, percona_source):
        if source.is_file():
            (Path(runs_dir) / "lab").mkdir(parents=True, exist_ok=True)
            shutil.copy(source, Path(runs_dir) / "lab" / source.name)
    env = {**os.environ, "CAPACITYLAB_RUNS_DIR": runs_dir, "CAPACITYLAB_PROVIDER": "mock"}
    server = subprocess.Popen([sys.executable, "-m", "capacitylab", "serve", "--port", str(PORT)], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frames: list[Path] = []
    try:
        wait_for_server()
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, color_scheme="light")

            def frame(name: str) -> None:
                path = FRAMES / f"{len(frames):02d}-{name}.png"
                page.screenshot(path=str(path))
                frames.append(path)

            page.goto(BASE + "/")
            page.screenshot(path=str(MEDIA / "home.png"))
            frame("home")

            if lab_source.is_file():
                page.goto(BASE + f"/lab/{lab_source.name}")
                full = FRAMES / "lab-full.png"
                page.screenshot(path=str(full), full_page=True)
                crop(full, span(page, "h1", ".lab-table"), MEDIA / "lab.png")
                frame("lab")

            if percona_source.is_file():
                page.goto(BASE + f"/lab/{percona_source.name}")
                full = FRAMES / "lab-percona-full.png"
                page.screenshot(path=str(full), full_page=True)
                crop(full, span(page, "#ptqd-EVENT", "#ptqd-EVENT"), MEDIA / "lab-percona.png", pad=2)

            page.goto(BASE + "/scenarios/campaign-overlap")
            frame("scenario")
            full = FRAMES / "scenario-full.png"
            page.screenshot(path=str(full), full_page=True)
            crop(full, span(page, "#options", ".multiples + .table-wrap"), MEDIA / "scenario-options.png")
            if lab_source.is_file():
                page.check("input[name=evidence]")
            page.screenshot(path=str(MEDIA / "scenario.png"))

            page.click("button[type=submit]")
            page.wait_for_url("**/runs/**", timeout=120_000)
            page.wait_for_selector("table.matrix")
            frame("run-top")
            full = FRAMES / "run-full.png"
            page.screenshot(path=str(full), full_page=True)
            crop(full, span(page, ".banner", "#decision"), MEDIA / "run-positions.png")
            if lab_source.is_file():
                crop(full, span(page, "#lab-measurements", ".lab-table"), MEDIA / "run-lab.png")
            crop(full, span(page, "#options", ".multiples + .table-wrap"), MEDIA / "run-options.png")
            crop(full, span(page, "article.proposal", "article.proposal"), MEDIA / "run-proposal.png")
            last_round = page.locator("details.round").last
            crop(full, span(page, "details.round:last-of-type", "details.round:last-of-type"), MEDIA / "run-transcript.png",
                 max_height=900)

            page.locator("#positions").scroll_into_view_if_needed()
            frame("positions")
            page.locator(".multiples").first.scroll_into_view_if_needed()
            panel = page.locator(".multiples svg.viz").nth(4)
            box = panel.bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.5)
            frame("options-hover")
            last_round.scroll_into_view_if_needed()
            frame("transcript")

            run_url = page.url
            page.goto(run_url + "/replay")
            page.screenshot(path=str(MEDIA / "replay.png"))
            frame("replay")

            page.goto(BASE + "/scenarios/campaign-overlap/evaluate", timeout=120_000)
            page.screenshot(path=str(MEDIA / "evaluation.png"))
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    images = []
    for path in frames:
        with Image.open(path) as im:
            scaled = im.convert("RGB").resize((960, int(im.height * 960 / im.width)), Image.LANCZOS)
            images.append(scaled.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    images[0].save(MEDIA / "demo.gif", save_all=True, append_images=images[1:], duration=1800, loop=0, optimize=True)
    print(f"wrote {len(list(MEDIA.glob('*.png')))} screenshots and demo.gif to {MEDIA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
