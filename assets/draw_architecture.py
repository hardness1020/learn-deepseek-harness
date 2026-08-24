#!/usr/bin/env python3
"""Draw assets/architecture.svg: the Mini-dsh architecture, one group per Section.

Stdlib only, like the tutorial itself. Run it from anywhere:

    python assets/draw_architecture.py           # writes assets/architecture.svg
    python assets/draw_architecture.py --png     # also renders architecture.png

The sketchy stroke is a seeded jitter over every outline, so the output is
byte-identical run to run: the SVG can be committed and diffed.
"""

import argparse
import base64
import pathlib
import random
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
W, H = 2200, 1420

BG = "#f6f8fa"
INK = "#1e1e1e"
GRAY = "#6f7681"
ORANGE = "#dd9a3c"
RED = "#e03131"
WHITE = "#ffffff"

FONT = "'Architects Daughter','Comic Sans MS',cursive"

parts = []
rnd = random.Random(20260822)


# -- sketchy primitives -----------------------------------------------------
def jit(a=1.7):
    return rnd.uniform(-a, a)


def rough_line(x1, y1, x2, y2, amp=1.7):
    """One cubic whose control points wander: a straight line, drawn by hand."""
    dx, dy = x2 - x1, y2 - y1
    c1 = (x1 + dx * 0.35 + jit(amp), y1 + dy * 0.35 + jit(amp))
    c2 = (x1 + dx * 0.68 + jit(amp), y1 + dy * 0.68 + jit(amp))
    return f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {x2:.1f},{y2:.1f}"


def rrect_path(x, y, w, h, r, amp=1.7):
    p = [f"M{x + r + jit(amp):.1f},{y + jit(amp):.1f}"]
    p.append(rough_line(x + r, y, x + w - r, y, amp))
    p.append(f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}")
    p.append(rough_line(x + w, y + r, x + w, y + h - r, amp))
    p.append(f"Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f}")
    p.append(rough_line(x + w - r, y + h, x + r, y + h, amp))
    p.append(f"Q{x:.1f},{y + h:.1f} {x:.1f},{y + h - r:.1f}")
    p.append(rough_line(x, y + h - r, x, y + r, amp))
    p.append(f"Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}")
    return " ".join(p)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, *, size=21, anchor="middle", fill=INK):
    parts.append(
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>'
    )


def box(x, y, w, h, lines=(), *, stroke=INK, fill=WHITE, dashed=False,
        size=21, r=14, sw=2.1, sub=None, sub_size=16):
    """A rounded rect drawn twice, with its label centred inside."""
    dash = ' stroke-dasharray="9 7"' if dashed else ""
    parts.append(f'<path d="{rrect_path(x, y, w, h, r)}" fill="{fill}" stroke="none"/>')
    for amp in (1.6, 2.2):
        parts.append(
            f'<path d="{rrect_path(x, y, w, h, r, amp)}" fill="none" '
            f'stroke="{stroke}" stroke-width="{sw}" stroke-linecap="round"{dash}/>'
        )
    lines = list(lines)
    subs = [sub] if isinstance(sub, str) else list(sub or ())
    if not lines and not subs:
        return
    step, sub_step = size * 1.22, sub_size * 1.3
    total = (len(lines) - 1) * step + len(subs) * sub_step
    cursor = y + h / 2 - total / 2 + size * 0.34
    for line in lines:
        text(x + w / 2, cursor, line, size=size, fill=stroke)
        cursor += step
    cursor += sub_step * 0.15
    for line in subs:
        text(x + w / 2, cursor, line, size=sub_size, fill=GRAY)
        cursor += sub_step


def group(x, y, w, h, title, *, dashed=False, r=16):
    """A titled container: the title sits inside the top-left corner."""
    box(x, y, w, h, (), fill="none", dashed=dashed, r=r)
    text(x + 20, y + 34, title, size=23, anchor="start")


def arrow(points, *, color=GRAY, dashed=False, sw=2.0, head=True, amp=1.3):
    """A hand-drawn polyline through waypoints, with an arrowhead at the end."""
    dash = ' stroke-dasharray="10 8"' if dashed else ""
    d = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        d.append(rough_line(x1, y1, x2, y2, amp))
    parts.append(
        f'<path d="{" ".join(d)}" fill="none" stroke="{color}" stroke-width="{sw}" '
        f'stroke-linecap="round" stroke-linejoin="round"{dash}/>'
    )
    if head:
        (px, py), (qx, qy) = points[-2], points[-1]
        dx, dy = qx - px, qy - py
        n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / n, dy / n
        for sign in (1, -1):
            hx, hy = qx - ux * 15 - uy * 8 * sign, qy - uy * 15 + ux * 8 * sign
            parts.append(
                f'<path d="M{hx:.1f},{hy:.1f} {rough_line(hx, hy, qx, qy, 0.8)}" '
                f'fill="none" stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>'
            )


def note(x, y, s, *, color=GRAY, size=18, anchor="middle"):
    text(x, y, s, size=size, anchor=anchor, fill=color)


# ===========================================================================
# The drawing. Every group carries the Section that builds it.
# ===========================================================================
parts.append(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

# --- left column: what the model is told, and what it is derived from ------
group(50, 60, 590, 300, "System prompt (08):")
box(78, 120, 172, 52, ["identity"], size=20)
box(78, 184, 172, 52, ["skills catalog"], size=20)
box(78, 248, 172, 52, ["tool schemas"], size=20)
box(330, 140, 285, 140, ["system text", "+ tool list"],
    sub=["ordered providers,", "byte-identical every step"])
for yy in (146, 210, 274):
    arrow([(250, yy), (292, yy), (292, 210), (326, 210)])
note(291, 326, "assemble()", size=17)

box(50, 400, 300, 78, ["runtime context"], dashed=True, size=20,
    sub=["cwd, clock: the part that moves"])
arrow([(200, 478), (200, 510), (62, 510), (62, 700), (74, 700)], color=ORANGE)
note(215, 500, "re-emitted as a user/message", color=ORANGE, anchor="start")

group(50, 545, 590, 380, "Session log (02):")
box(78, 596, 175, 300, ["Events[]"],
    sub=["append-only,", "frozen; seq is", "its index, forever"])
box(315, 596, 300, 100, ["surface"], sub=["which seqs are messages"])
box(315, 750, 300, 110, ["Compaction (03)"],
    sub=["one appended event,", "a surface replace"])
arrow([(253, 636), (311, 636)])
note(282, 618, "append", size=17)
arrow([(465, 750), (465, 700)], color=ORANGE)
note(542, 730, "replace", color=ORANGE, size=17)

group(50, 1010, 590, 158, "Inbox (07):")
box(80, 1065, 245, 78, ["next-turn"], size=20, sub=["starts its own turn"])
box(370, 1065, 245, 78, ["next-step"], size=20, sub=["joins the work underway"])
note(200, 1196, "claimed only at a step boundary")

# --- centre column: the loop that drives all of it -------------------------
group(680, 60, 420, 1140, "Agent loop (04):")
box(712, 108, 190, 54, ["turn/start"], size=20)
group(698, 190, 390, 862, "step:", dashed=True)
box(716, 246, 336, 56, ["claim(target)"])
box(716, 320, 336, 56, ["assemble()"])
box(716, 394, 336, 62, ["derive_messages()"], sub=["from the surface, never stored"])
box(716, 474, 336, 56, ["request/header"])
box(716, 554, 336, 100, ["Model seam (00)"], stroke=RED,
    sub=["scripted stand-in | anthropic adapter"])
box(716, 678, 336, 76, ["assistant/chunk .. /message"], size=20,
    sub=["streamed, then one final Message"])
box(716, 778, 336, 56, ["tool_calls?"])
box(716, 852, 336, 76, ["step/end"], sub=["completed | aborted | go around"])
box(712, 1096, 190, 54, ["turn/end"], size=20)
for a, b in ((302, 316), (376, 390), (456, 470), (530, 550), (654, 674),
             (754, 774), (834, 848)):
    arrow([(884, a), (884, b)], sw=1.9)
arrow([(807, 162), (807, 242)])
arrow([(807, 928), (807, 1092)])
arrow([(1052, 890), (1070, 890), (1070, 222), (860, 222), (860, 242)], color=ORANGE)
note(962, 208, "consume loop", color=ORANGE)

# the log and the inbox feed the step; the step feeds the log
arrow([(615, 210), (646, 210), (646, 348), (712, 348)])
arrow([(615, 646), (660, 646), (660, 425), (712, 425)])
arrow([(698, 962), (680, 962), (680, 988), (200, 988), (200, 900)])
note(432, 966, "every event of the turn appends here")
arrow([(615, 1104), (672, 1104), (672, 276), (712, 276)])

# --- the tool spine: one ring per gate a call passes through ---------------
group(1140, 175, 340, 895, "Scheduler (06):", dashed=True)
note(1310, 244, "prepare . dispatch . finalize . finish", size=16)
note(1310, 266, "one result per call, in model order", size=16)
group(1158, 286, 304, 766, "pre / ask (05):")
group(1176, 350, 268, 686, "guard (05):")
group(1193, 412, 234, 608, "Tools:")
TOOLS = ["skill/load", "fs/read", "fs/write", "shell/run", "job/start",
         "job/output", "subagent/start", "subagent/wait"]
for i, name in enumerate(TOOLS):
    box(1213, 452 + i * 62, 194, 48, [name], size=19)
box(1213, 452 + len(TOOLS) * 62, 194, 48)
note(1310, 1010, "any plugin's tool", size=15)
arrow([(1052, 806), (1136, 806)])
arrow([(1136, 884), (1056, 884)])

# --- right column: what a tool call reaches --------------------------------
group(1580, 60, 570, 220, "Skills (09):")
box(1608, 118, 250, 132, ["layered registry"], sub=["providers shadow by name"])
box(1885, 118, 240, 60, ["catalog_text()"], size=19)
box(1885, 190, 240, 60, ["get(name)"], size=19)
arrow([(1407, 476), (1508, 476), (1508, 220), (1881, 220)])
note(1516, 300, "load a body on use", size=17, anchor="start")
arrow([(1885, 148), (1544, 148), (1544, 36), (30, 36), (30, 208), (74, 208)])
note(900, 24, "the catalog is context: cheap, and always visible")

group(1580, 320, 570, 320, "Capability seams (10):")
box(1608, 380, 250, 60, ["Definition (ABC)"], size=19)
box(1888, 380, 237, 60, ["Consumer"], size=19)
box(1608, 464, 250, 58, ["MemoryFileSystem"], size=18)
box(1888, 464, 237, 58, ["ArgvRewriteSandbox"], size=18)
box(1608, 544, 250, 58, ["sandboxed shell"], size=18)
box(1888, 544, 237, 58, ["LlmRuntime"], size=18)
note(1866, 626, "seam(key): one Definition, many Providers", size=17)
arrow([(1858, 410), (1884, 410)])
arrow([(1407, 538), (1536, 538), (1536, 410), (1604, 410)])

group(1580, 680, 570, 240, "Jobs (11):")
box(1608, 740, 280, 88, ["JobRegistry"], sub=["start / output / kill"])
box(1918, 740, 207, 88, ["worker thread"], size=20, sub=["outlives the turn"])
box(1608, 848, 517, 54, ["owner fence: only caller_id may wait or kill"], size=18)
arrow([(1888, 784), (1914, 784)])
arrow([(1407, 724), (1500, 724), (1500, 784), (1604, 784)])
arrow([(1604, 875), (1552, 875), (1552, 1188), (350, 1188), (350, 1172)],
      color=ORANGE)
note(1310, 1178, "a wakeup lands in the inbox, never in the log", color=ORANGE)

group(1580, 960, 570, 250, "Subagents (12):")
box(1608, 1018, 280, 88, ["SubagentRuntime"], size=20, sub=["named providers"])
box(1918, 1018, 207, 88, ["child session"], size=20, sub=["+ its own tool scope"])
box(1608, 1120, 517, 74, ["SubagentRun"],
    sub=["the parent takes back one result, not a transcript"])
arrow([(1407, 848), (1500, 848), (1500, 1062), (1604, 1062)],
      color=ORANGE, dashed=True)
note(1510, 992, "spawn", color=ORANGE, size=17, anchor="start")
arrow([(1888, 1062), (1914, 1062)])
arrow([(2021, 1106), (2021, 1116)])

# --- bottom band: what mounts every box above ------------------------------
group(50, 1245, 1000, 150, "Composition (13):")
box(78, 1300, 290, 72, ["MINI_BASE + layers"], size=20,
    sub=["insert / replace / disable"])
box(400, 1300, 300, 72, ["entry list"], size=20, sub=["id . name . config"])
box(732, 1300, 290, 72, ["mount_entries()"], size=20)
arrow([(368, 1336), (396, 1336)])
arrow([(700, 1336), (728, 1336)])

group(1090, 1245, 1060, 150, "Kernel (01):")
box(1118, 1300, 310, 72, ["ctx.plugin() / effect()"], size=19)
box(1460, 1300, 300, 72, ["fiber collects disposers"], size=19)
box(1792, 1300, 330, 72, ["dispose(): reverse order"], size=19)
arrow([(1022, 1336), (1114, 1336)])
arrow([(1428, 1336), (1456, 1336)])
arrow([(1760, 1336), (1788, 1336)])
for x in (200, 480, 1500, 1950):
    arrow([(x, 1241), (x, 1214)], dashed=True, sw=1.8)
note(900, 1232, "every box above is a plugin, and every registration is reversible")


# ===========================================================================
def build_svg():
    # Architects Daughter by Kimberly Geswein, SIL OFL 1.1; embedded so the
    # SVG stands alone. Licence text: assets/ArchitectsDaughter-OFL.txt
    font = base64.b64encode((HERE / "ArchitectsDaughter.woff2").read_bytes()).decode()
    style = (
        "<style>@font-face{font-family:'Architects Daughter';font-style:normal;"
        "font-weight:400;src:url(data:font/woff2;base64," + font + ")"
        " format('woff2');}</style>"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}">{style}' + "".join(parts) + "</svg>"
    )


def render_png(svg_path, png_path):
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not pathlib.Path(chrome).exists():
        sys.exit("no headless Chrome found: open the SVG and export it by hand")
    subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--screenshot={png_path}", f"--window-size={W},{H}",
         f"file://{svg_path}"],
        check=True, capture_output=True,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", action="store_true", help="also render architecture.png")
    args = ap.parse_args()

    svg = HERE / "architecture.svg"
    svg.write_text(build_svg(), encoding="utf-8")
    print(f"wrote {svg}")
    if args.png:
        png = HERE / "architecture.png"
        render_png(svg, png)
        print(f"wrote {png}")
