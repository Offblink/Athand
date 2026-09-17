---
name: athand
description: Drive this Windows machine's desktop from the command line — list windows, number a window's controls, then click / double-click / drag / type / press keys / scroll by target number. Use when the work needs a real window on screen (a GUI app, a tray app, a desktop icon, a file being dragged into another program) and there is no programmatic path to it.
---

# athand — the desk at hand

One script, `athand.py`, that looks at this machine's screen and acts on it. A fresh process
per call: what survives between calls is written to `%TEMP%\athand` (the listing of a window,
and the pictures). Windows only.

```bash
python skills/athand/athand.py windows
python skills/athand/athand.py targets --hwnd 4653616
python skills/athand/athand.py click   --hwnd 4653616 --target 3
python skills/athand/athand.py type    --hwnd 4653616 --target 7 --text "你好"
python skills/athand/athand.py drag    --hwnd 4653616 --target 7 --dx 300
```

Run it with `PYTHONIOENCODING=utf-8` when you read its output through a pipe, or Chinese text
comes back as mojibake (measured: a `stdout=PIPE` child on this box speaks cp936).

## The flow: windows → targets → act

1. **`windows`** — the numbered list of windows: `hwnd` (decimal *and* hex), size, process,
   title. `hwnd` is the identity; titles are not (they change under you). `--include all`
   adds minimized/hidden windows plus the notification area (`Shell_TrayWnd`) and the desktop.
2. **`targets --hwnd N`** — the controls inside one window, numbered, printed as a listing and
   *drawn* on a picture: `[numbered frame: <path>]`. Read that PNG (your own file-read tool) and
   pick the number you can see. The listing is also written to `%TEMP%\athand\<hwnd>.json`.
3. **Act by number or name**: `click --target 3`, `type --name "发送"`, `drag --target 7 --dx 300`.
   Coordinates are never accepted — that is the whole point (see *Why numbers* below).

Every input action prints what it verified and a fresh `[frame: <path>]` of the window
afterwards. **Look at that path** before deciding the action worked.

## The two rules (they outrank what you know)

1. **触手可及 — anything at hand on screen comes first.** Opening a file or an app: if its icon
   is on the desktop or its row is in a window that is showing, that is a `double-click`, not a
   shell command. An application that is not running anywhere visible is opened through its
   *own* door — `restore` finds it (taskbar button → tray icon → desktop icon: that is the order
   the app itself listens to). `ShowWindow` is the fallback for an app with no door at all, and
   what it yields is a window that *looks* normal and ignores input — the result says so.
2. **Creating, deleting, renaming or moving a file is not a desktop gesture.** That goes through
   the shell (`bash`: `mkdir`/`mv`/`rm`, or the file tools). A pixel path has to be resolved into
   a rectangle first, and one resolved onto the wrong row is a data accident, not a stray click.
   (`del` does not use the Recycle Bin — measured.) *Handing* a file to an application (into a
   text box, onto a drop area) is not a file operation: that is `drag`.

## Commands

| command | what it does |
|---|---|
| `windows [--include visible\|all]` | the window list |
| `shot [--hwnd N]` | PNG of the whole screen, or of one window; prints the path |
| `targets --hwnd N` | number this window's controls + draw the numbers on a picture |
| `label --hwnd N --target K --label TEXT` | name a shape that has no text, so `--name TEXT` finds it later |
| `click` / `double-click --hwnd N (--target K \| --name TEXT) [--button left\|right]` | `double-click` is the "open" gesture |
| `drag --hwnd N [--target K \| --name TEXT \| --from titlebar] [--to-hwnd M] [--to-target K \| --to-name TEXT] [--dx PX] [--dy PX] [--route taskbar]` | carry something with the button held down |
| `type --hwnd N [--target K \| --name TEXT] --text S [--char-delay S]` | text, one character at a time; the clipboard is never touched |
| `key --hwnd N --keys ctrl s` | a key or a combination |
| `scroll --hwnd N (--target K \| --name TEXT)` | scroll a control into view |
| `restore --hwnd N [--via auto\|window\|shell]` | bring a minimized / tray-resident window back |
| `release` | lift every button and key this machine still has down (after a kill) |
| `selftest [--keep]` | drive the bundled probes and check every gesture against what the app recorded |

`--hwnd 4653616` and `--hwnd 0x470230` are the same window. Exit codes: `0` ran, `2` refused
(the text starts with `ERROR:` or `ESCALATED:` — nothing was sent or nothing was verified).

## What `drag` is for

Carrying something **with the button held down**: a file or picture into a text box (typing
carries text only), a window by its title bar, a selection across, a shape around a canvas. A
drop needs no focus, which is why it also works in an app whose input box exposes no
addressable control at all.

- The grab end is a target in `--hwnd`, or `--from titlebar` for the window's own title bar.
- The drop end is an anchor **in the program**: `--to-target`/`--to-name` in `--to-hwnd`, or that
  window's client-area centre, shifted by `--dx`/`--dy` (a *relative* shift, never a position).
- No `--to-hwnd` = a carry inside one window.
- `--route taskbar` is the way past a covering window: the thing is carried onto the
  destination's taskbar button and held until the shell brings that window forward (that is how
  a *minimized* destination comes back). Without it a covered drop point is refused.
- A drag that would release somewhere else is cancelled with Escape and says so. Escape-first,
  release-second: that is the order a shell drag loop reads as "give it back".

## Why numbers, not coordinates

Measured 2026-09-13: coordinates produced by a model miss by 15–68px (a coin flip on an 18px
target); picking a candidate number hit 3/3. So the program resolves every target itself out of
the window's accessibility tree (exact), OCR, or the shapes it cuts out of the picture — and
hands you numbers.

The numbers belong to **one listing**: `%TEMP%\athand\<hwnd>.json` is written with the window
rectangle and the timestamp, and a later call that finds the window somewhere else **refuses**
`--target` with "run `targets --hwnd N` again". Re-list; do not retry the old number.
`--name` keeps working across a moved window (it is re-matched against the live a11y tree), and
a label that is out of date refuses itself with its own message.

If `targets` says a window **draws itself** (Chromium/Electron like QQ, Qt like WeChat), the
picture *is* the tool face: read the text off the frame and pick the cut-out shapes by number.
OCR names are approximate (`drag_source.txt` reads as `drag_source. txt`), numbers are not. To
type into such a window, click the box first, then `type` with no `--target` — it types into
whatever has the focus. Give the shapes you will need again a `label`.

## Traps that cost real time (all measured on this box)

1. **A minimized window's geometry is a lie**: client rect `0x0`, window rect the icon slot
   `(-48000,-48000)` — and `WindowFromPoint` at that icon slot *does* answer with the window. So
   "the drop point is reachable" can only be judged after `window_state == "normal"`. `restore`
   first, then measure.
2. **A locked machine answers nothing**: with the session locked the *input desktop* is
   `Screen-saver`, `SendInput` fails with ERROR_ACCESS_DENIED, `GetCursorPos` fails,
   `GetForegroundWindow` returns 0 and the screen capture is empty. The script refuses input
   actions up front with that sentence instead of reporting "INJECTION FAILED". `windows`,
   `targets` and window-level `shot` still work while locked (they use `PrintWindow`).
3. **DPI**: a DPI-unaware window that is not in the foreground cannot be captured at all
   (`PrintWindow` returns a scaled stub — half black, every rect off by the scale factor). The
   tool refuses instead of showing you that stub. Bring the window forward (`restore`) and read
   again.
4. **`set_foreground` can be refused** for another process's window; the result says
   `could not raise the window to the foreground!` That is not a bug — the click may still have
   landed, or not. Re-`targets`, or wake the window through its own door (`restore`).
5. **A stuck key or button is a human's problem**, so the script never leaves one: presses and
   releases are booked, the release is checked against `GetAsyncKeyState`, and
   `athand.py release` asks the OS what is still down (it cannot tell your keys from its own —
   the safe direction to be wrong in).
6. **Win11 Explorer's file rows are not in the a11y tree this tool walks**: they do not appear
   in `targets` and are not judged self-drawn. Reach one by a *fragment* of its visible text —
   `--name "报告"` finds `报告.pdf` where the full name with its extension may not.
7. **A synthesized drag really does start OLE drag-and-drop**: Explorer file row → another app
   delivers `WM_DROPFILES` with the right path (measured). The alternative route is the
   keyboard: click the row, `key --keys ctrl c`, click the box, `key --keys ctrl v` — a plain
   copy through the clipboard, so it spends whatever the user had on it.
8. **Thresholds and timeouts are module constants read inside the functions**, never default
   arguments: a default is bound at import and cannot be tuned (or patched in a test).
9. **An elevated window (UAC secure desktop) is out of reach**: UIPI. That is the hard boundary,
   not a gap — report it rather than trying harder.
10. **`--char-delay` is a real pace, not a knob to minimize**: 0.15s ≈ seven characters a
    second, the band the user asked for. 0.03 (33/s) makes text appear as if pasted, which is
    exactly what this tool exists not to do.

## `unverified` and `ESCALATED`

Input actions verify themselves against program-side truth (a11y value, focus, window rect,
frame diff). Three outcomes:

- **`verified: …`** — you are done; the reason is printed.
- **`unverified: …`** — the action ran, the machine did not confirm it. Look at the frame path
  it printed. Ordinary causes: the control exposes no readable value (then the picture moving
  *is* the effect, and it counted), the window was mid-repaint, or the click landed on the wrong
  thing. Re-`targets` once; do not click the same number a second time blindly.
- **`ESCALATED: 3 attempts at … with no visible effect`** (exit 2) — the same target failed
  `FAILURE_LIMIT` times and the run stops there. In Fungi this asked the user; a script has
  nobody to ask, so it is on you: re-read the frame, re-list the window, `restore` it, or report.
  Clicking again is what this line exists to prevent.

## Selftest

`python athand.py selftest` starts the probes in `probes/` and checks every gesture against what
the probe *itself* recorded (the `WM_COMMAND` a button sent, the `EM_GETSEL` an edit reported,
the window rectangle the OS gives back). `--keep` keeps the work directory with the JSONL logs,
the PNGs and the listings. Checks that cannot run are reported `SKIP` with the reason, never as
a pass — including "the machine is locked".
