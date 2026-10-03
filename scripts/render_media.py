from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def run_demo(folder: Path) -> Path:
    report = folder / "browser-report"
    run = subprocess.run([str(PYTHON), "-m", "toolcall_check", "--demo", "--output", str(report)], cwd=folder, capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise RuntimeError(run.stdout + run.stderr)
    return report


def render_browser_assets(report: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 375, "height": 812})
            page.goto((report / "report.html").as_uri(), wait_until="load")
            if page.evaluate("document.documentElement.scrollWidth") > 375:
                raise RuntimeError("report overflows mobile viewport")
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.tagName") != "SUMMARY":
                raise RuntimeError("evidence is not reachable with the keyboard")
            if page.evaluate("getComputedStyle(document.activeElement).outlineWidth") == "0px":
                raise RuntimeError("focused evidence has no visible outline")
            page.set_viewport_size({"width": 1200, "height": 920})
            if page.evaluate("document.documentElement.scrollWidth") > 1200:
                raise RuntimeError("report overflows desktop viewport")
            page.screenshot(path=str(ROOT / "assets" / "report.png"))
            page.set_viewport_size({"width": 1200, "height": 675})
            page.goto((ROOT / "assets" / "social-card.html").as_uri(), wait_until="load")
            page.screenshot(path=str(ROOT / "assets" / "social-card.png"))
        finally:
            browser.close()


def render_terminal_gif(folder: Path) -> float:
    vhs = shutil.which("vhs")
    ffprobe = shutil.which("ffprobe")
    if not vhs or not ffprobe:
        raise RuntimeError("vhs and ffprobe are required for terminal media")
    destination = ROOT / "assets" / "demo.gif"
    source = (ROOT / "assets" / "demo.tape").read_text()
    tape = folder / "demo.tape"
    tape.write_text(source.replace("Output assets/demo.gif", "Output " + json.dumps(str(destination))))
    env = dict(os.environ)
    env["PATH"] = str(PYTHON.parent) + os.pathsep + env.get("PATH", "")
    run = subprocess.run([vhs, str(tape)], cwd=folder, env=env, capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        raise RuntimeError(run.stdout + run.stderr)
    info = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
    duration = float(json.loads(info.stdout)["format"]["duration"])
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to encode the recording")
    original = folder / "vhs-recording.gif"
    destination.rename(original)
    first = folder / "first.png"
    last = folder / "last.png"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(original), "-frames:v", "1", str(first)], check=True, capture_output=True)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(max(0, duration - 0.1)), "-i", str(original), "-frames:v", "1", str(last)], check=True, capture_output=True)
    concat = folder / "states.txt"
    concat.write_text(f"file '{first.as_posix()}'\nduration 7\nfile '{last.as_posix()}'\nduration 7\nfile '{last.as_posix()}'\n")
    palette = folder / "palette.png"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-vf", "fps=1/2,palettegen=stats_mode=full", str(palette)], check=True, capture_output=True)
    graph = "fps=1/2[x]" + chr(59) + "[x][1:v]paletteuse=dither=none"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(palette), "-lavfi", graph, "-t", "14", "-loop", "0", str(destination)], check=True, capture_output=True)
    info = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(destination)], check=True, capture_output=True, text=True)
    duration = float(json.loads(info.stdout)["format"]["duration"])
    if destination.stat().st_size > 10 * 1024 * 1024:
        raise RuntimeError("GIF exceeds the publication size limit")
    if not 10 <= duration <= 20:
        raise RuntimeError(f"GIF duration is outside 10 to 20 seconds: {duration}")
    return duration


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="toolcall-check-media-") as temporary:
        folder = Path(temporary)
        report = run_demo(folder)
        render_browser_assets(report)
        duration = render_terminal_gif(folder)
    print(f"Rendered terminal GIF ({duration:g}s), browser report, and 1200x675 social card")


if __name__ == "__main__":
    main()
