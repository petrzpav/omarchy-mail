"""Drive the app headless through the Jev story and render it to an MP4.

Each step saves an SVG screenshot with how long it stays on screen and a caption;
rsvg-convert, ImageMagick and ffmpeg turn those into 1920x1080 video. Waiting on
Jev is cut out: the video shows each change as it lands, not the time in between.
"""

import asyncio
import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path

from textual.widgets import DataTable, TextArea

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
COLS, ROWS = 118, 34
FONT = "iA Writer Mono S"
CAPTION_FONT = "iA-Writer-Duo-S-Bold"
BG = "#11131a"
END = ((88, "white", -70, "Mail for Omarchy"),
       (44, "#b8bcc8", 30, "Jev files your mail and checks your replies"),
       (36, "#7d8290", 110, "Jev by TypeSafe AI"))

PARTIAL = """Hi Jonas,

glad you like it! Sure, October 14 works for us, we'll move the launch.

The form can send to both of you, I'll add Clara."""
REST = """

A Czech version would be around EUR 900, I'll send a proper quote by Friday.

The updated invoice with the extra photo day is attached."""
SIGN = """

Best,
Mia"""


class Film:
    def __init__(self, app):
        self.app = app
        self.frames: list[tuple[str, float, str]] = []
        self.caption = ""

    async def snap(self, seconds: float, caption: str | None = None, settle: float = 0.05):
        await asyncio.sleep(settle)
        if caption is not None:
            self.caption = caption
        self.frames.append((self.app.export_screenshot(title="Mail"), seconds, self.caption))

    async def until(self, cond, timeout=60.0, every=0.1):
        end = time.time() + timeout
        while not cond():
            if time.time() > end:
                raise TimeoutError("demo step never finished")
            await asyncio.sleep(every)


async def story(app, film: Film):
    from mailtui import ops

    main = app.main
    table = main.query_one(DataTable)
    await film.until(lambda: table.row_count >= 11)
    await film.snap(3.0, "A busy inbox. Nothing sorted yet.")

    # -- Jev files the inbox: labels appear as each decision lands
    done = {"v": False}
    real_sort = ops.sort_inbox

    def sort_and_flag(*a, **kw):
        try:
            return real_sort(*a, **kw)
        finally:
            done["v"] = True
    ops.sort_inbox = sort_and_flag
    app.sort_now()
    await film.snap(0.6, "Jev reads each one and files it into a Gmail label")
    shown = None
    while True:
        main.load_messages(keep_cursor=True)
        await asyncio.sleep(0.12)
        labels = [str(table.get_cell_at((r, 4))) for r in range(table.row_count)]
        if labels != shown:
            shown = labels
            await film.snap(0.35)
        if done["v"] and labels == shown and sum(1 for l in labels if l.strip()) >= 10:
            main.load_messages(keep_cursor=True)
            await asyncio.sleep(0.3)
            break
    await film.snap(3.2, "Clients, Dev, Finance, Travel… and Mom stays in the Inbox")

    # -- Ctrl+J: how sure is Jev about this one
    await film.snap(0.5, "Ctrl+J shows how Jev weighed every label")
    row = next(r for r in range(table.row_count) if "Notes from" in str(table.get_cell_at((r, 2))))
    for r in range(1, row + 1):
        table.move_cursor(row=r, animate=False)
        await film.snap(0.12)
    await film.snap(0.5)
    await main.run_action("jev")
    await film.until(lambda: len(app.screen_stack) > 2 or app.screen is not main, timeout=30)
    await film.snap(3.6, "Ctrl+J shows how Jev weighed every label", settle=0.3)
    app.pop_screen()
    table.move_cursor(row=0, animate=False)
    await film.snap(0.5, "")

    # -- the client email with four asks
    main.open_message(main.msgs[0])
    await film.until(lambda: main.thread_msgs and main.query(".body"), timeout=10)
    await film.snap(3.4, "A client email, full of questions", settle=0.4)

    # -- reply: Jev grades it while you type
    await main.run_action("reply")
    await film.until(lambda: app.screen.__class__.__name__ == "Compose", timeout=10)
    compose = app.screen
    await film.snap(1.4, "Hit reply. Jev sits next to what you write", settle=0.4)
    body = compose.query_one("#body", TextArea)
    body.focus()
    await type_into(film, body, PARTIAL + SIGN, caption="Hit reply. Jev sits next to what you write")
    await review(film, compose, body)
    await film.snap(4.2, "Pause typing: Jev grades it and lists what you missed")

    # -- answer the rest
    body.move_cursor((PARTIAL.count("\n"), len(PARTIAL.splitlines()[-1])))
    await film.snap(0.4, "Answer the rest…")
    await type_into(film, body, REST, caption="Answer the rest…")
    await review(film, compose, body)
    await film.snap(4.5, "5/5 answered, all green. Ready to send.")
    film.frames.append(("", 3.5, ""))


async def type_into(film: Film, body: TextArea, s: str, caption: str, chunk=3):
    for i in range(0, len(s), chunk):
        body.insert(s[i:i + chunk])
        await film.snap(0.045, caption, settle=0.01)


async def review(film: Film, compose, body: TextArea):
    await film.snap(0.5)
    compose.action_review(quiet=True)
    await film.snap(0.7, settle=0.1)          # "reading…"
    await film.until(lambda: compose._reviewed == body.text, timeout=60)
    await asyncio.sleep(0.2)


# ------------------------------------------------------------ rendering

def render(frames, out: Path, work: Path, fps=30):
    """SVG screenshots -> captioned 1920x1080 PNGs -> one image per video frame -> MP4."""
    work.mkdir(parents=True, exist_ok=True)
    pngs: dict[str, Path] = {}
    seq = work / "seq"
    seq.mkdir()
    n, t = 0, 0.0
    for svg, seconds, caption in frames:
        svg = svg.replace('font-family: "Fira Code", monospace', f'font-family: "{FONT}", monospace')
        svg = svg.replace("font-family: arial", f'font-family: "{FONT}"')
        key = hashlib.sha1((svg + caption).encode()).hexdigest()[:16]
        if key not in pngs:
            pngs[key] = card(work / key, caption, svg) if svg else card(work / key, "", lines=END)
        t += seconds
        while n < round(t * fps):                    # cumulative, so short frames never drift
            (seq / f"{n:05d}.png").hardlink_to(pngs[key])
            n += 1
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", seq / "%05d.png",
                    "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-movflags", "+faststart", out], check=True)


def card(base: Path, caption: str, svg: str = "", lines: tuple = ()) -> Path:
    """One 1920x1080 frame: the screenshot on top with the caption under it, or a title card."""
    frame = base.with_suffix(".png")
    cmd = ["magick", "-size", "1920x1080", f"xc:{BG}"]
    if svg:
        src, shot = base.with_suffix(".svg"), base.with_name(base.name + "-shot.png")
        src.write_text(svg)
        subprocess.run(["rsvg-convert", "-w", "1560", "-o", shot, src], check=True)
        cmd += [shot, "-gravity", "north", "-geometry", "+0+18", "-composite"]
    if caption:
        cmd += ["-font", CAPTION_FONT, "-pointsize", "48", "-fill", "white", "-gravity", "south",
                "-annotate", "+0+34", caption]
    for size, colour, dy, text in lines:
        cmd += ["-font", CAPTION_FONT, "-pointsize", str(size), "-fill", colour, "-gravity", "center",
                "-annotate", f"+0{dy:+d}", text]
    subprocess.run(cmd + [frame], check=True)
    return frame


def main(cfg, argv):
    from mailtui.app import MailApp

    app = MailApp(cfg)
    film = Film(app)

    async def run():
        async with app.run_test(size=(COLS, ROWS)) as pilot:
            await pilot.pause()
            await story(app, film)

    asyncio.run(run())
    OUT.mkdir(exist_ok=True)
    work = OUT / "frames"
    shutil.rmtree(work, ignore_errors=True)
    out = OUT / "jev-demo.mp4"
    render(film.frames, out, work)
    shutil.rmtree(work)
    total = sum(s for _, s, _ in film.frames)
    print(f"{out}  {len(film.frames)} frames, {total:.1f}s")
