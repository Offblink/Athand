"""Tk canvas probe — for the third candidate tier (spec §35.1), the one that reads pixels.

Two controls are drawn in pixels: a labelled one ("确定") and an icon-shaped one with no
text at all. Tk exposes no a11y for either, so this is exactly the self-drawn-UI case: only
the program's binarisation can find the icon, and the model is supposed to choose it by
number. Every event that actually arrives is appended to its JSONL log — the app's own
account, not the tool's claim.

    python probes/canvas_probe2.py --log %TEMP%\\athand-canvas.jsonl
"""

import json
import os
import sys
import time
import tkinter as tk


def _arg(name: str, default: str) -> str:
    argv = sys.argv[1:]
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    return default


_TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
LOG = _arg("--log", os.path.join(_TEMP, "athand-canvas.jsonl"))
W, H = 900, 600
root = tk.Tk()
root.title("athand canvas probe")
root.geometry(f"{W}x{H}+320+240")
root.attributes("-topmost", True)
canvas = tk.Canvas(root, bg="white", width=W, height=H, highlightthickness=0)
canvas.pack(fill="both", expand=True)

BTN = (60, 60, 260, 120)  # labelled, OCR-readable
ICON = (320, 60, 420, 160)  # a plain drawn icon: no text, no a11y, no label

canvas.create_rectangle(*BTN, fill="#2f6fd0", outline="#1b4b96", width=3)
canvas.create_text(
    (BTN[0] + BTN[2]) // 2,
    (BTN[1] + BTN[3]) // 2,
    text="确定",
    fill="white",
    font=("Microsoft YaHei", 22),
)
# an icon: a filled circle with two bars through it (nothing a text reader can use)
cx, cy = (ICON[0] + ICON[2]) // 2, (ICON[1] + ICON[3]) // 2
canvas.create_oval(ICON[0], ICON[1], ICON[2], ICON[3], fill="#e0f0e0", outline="#2f8f4f", width=4)
canvas.create_rectangle(cx - 6, ICON[1] + 18, cx + 6, ICON[3] - 18, fill="#2f8f4f", outline="")
canvas.create_rectangle(ICON[0] + 18, cy - 6, ICON[2] - 18, cy + 6, fill="#2f8f4f", outline="")
root.update()

t0 = time.time()
log = open(LOG, "w", encoding="utf-8")
state = {"hits": 0}


def write(record: dict) -> None:
    log.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.flush()


def meta() -> None:
    root.update()
    write(
        {
            "kind": "meta",
            "canvas_x": canvas.winfo_rootx(),
            "canvas_y": canvas.winfo_rooty(),
            "w": canvas.winfo_width(),
            "h": canvas.winfo_height(),
        }
    )


def on_down(event) -> None:
    write({"kind": "down", "x": event.x, "y": event.y, "t": round(time.time() - t0, 4)})
    for name, box in (("确定", BTN), ("icon", ICON)):
        if box[0] <= event.x <= box[2] and box[1] <= event.y <= box[3]:
            state["hits"] += 1
            write({"kind": "hit", "target": name, "x": event.x, "y": event.y, "t": round(time.time() - t0, 4)})


canvas.bind("<Button-1>", on_down)
root.bind("<Escape>", lambda _e: root.destroy())
meta()
root.mainloop()
log.close()
print("probe closed")
