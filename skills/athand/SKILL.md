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

Set `PYTHONIOENCODING=utf-8` when you read its output through a **pipe**. The script does not
choose an encoding: a Windows child whose stdout is not a console writes the ANSI code page —
`gbk` on this box (measured: a bare child reports `sys.stdout.encoding == 'gbk'`), which a UTF-8
reader sees as mojibake. An agent harness often already exports `PYTHONUTF8=1`, which makes it
UTF-8 anyway (this one does, which is why it usually looks fine); the variable is what makes it
deterministic.

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
   `del` does not use the Recycle Bin — measured: `cmd /c del <file>` left the Recycle Bin
   namespace at 11 entries, while
   `powershell -Command "Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('<path>','OnlyErrorDialogs','SendToRecycleBin')"`
   took it to 12 (exit code 0; the directory call is `DeleteDirectory`). Deleting the user's data
   is the same permission question as moving it: use the second form, or ask.
   *Handing* a file to an application (into a text box, onto a drop area) is not a file
   operation: that is `drag`.

## Commands

| command | what it does |
|---|---|
| `windows [--include visible\|all]` | the window list |
| `shot [--hwnd N]` | PNG of the whole screen, or of one window; prints the path |
| `decider [--start \| --stop] [--hwnd N --intent TEXT]` | the decision service this box may name numbers with: its state, start/stop, or one question asked without clicking |
| `targets --hwnd N` | number this window's controls + draw the numbers on a picture |
| `label --hwnd N --target K --label TEXT` | name a shape that has no text, so `--name TEXT` finds it later |
| `click` / `double-click --hwnd N (--target K \| --name TEXT \| --intent TEXT) [--button left\|right]` | `double-click` is the "open" gesture |
| `drag --hwnd N [--target K \| --name TEXT \| --intent TEXT \| --from titlebar] [--to-hwnd M] [--to-target K \| --to-name TEXT] [--dx PX] [--dy PX] [--route taskbar]` | carry something with the button held down |
| `type --hwnd N [--target K \| --name TEXT \| --intent TEXT] --text S [--char-delay S]` | text, one character at a time; the clipboard is never touched |
| `key --hwnd N --keys ctrl s` | a key or a combination |
| `scroll --hwnd N (--target K \| --name TEXT \| --intent TEXT)` | scroll a control into view |
| `restore --hwnd N [--via auto\|window\|shell]` | bring a minimized / tray-resident window back |
| `release` | lift every button and key this machine still has down (after a kill) |
| `selftest [--keep]` | drive the bundled probes and check every gesture against what the app recorded |

`--hwnd 4653616` and `--hwnd 0x470230` are the same window. Exit codes: `0` ran, `2` refused
(the text starts with `ERROR:`, `UNDECIDED:` or `ESCALATED:` — nothing was sent, or nothing was
verified, or the number could not be named at all).

## What `drag` is for

Carrying something **with the button held down**: a file or picture into a text box (typing
carries text only), a window by its title bar, a selection across, a shape around a canvas. A
drop needs no focus, which is why it also works in an app whose input box exposes no
addressable control at all.

- The grab end is a target in `--hwnd`, or `--from titlebar` for the window's own title bar.
  A target is grabbed at its **centre**, so a selection drag inside a text box needs the box to
  have text under that centre — drag in an empty area of a control and nothing is selected
  (measured 2026-09-17: the tool reported "nothing changed on screen" and it had done exactly
  what it was asked to).
- The drop end is an anchor **in the program**: `--to-target`/`--to-name` in `--to-hwnd`, or that
  window's client-area centre, shifted by `--dx`/`--dy` (a *relative* shift, never a position).
- A window that draws itself usually offers **no candidate for its input box** — nothing cuts a
  blank area into a shape — so the drop end has to be that window's client-area centre plus a
  relative shift. Read the window rectangle out of the listing JSON, work out the blank centre of
  the box, and pass the difference as `--dx`/`--dy`. The tool prints the drop point it actually
  used, so check it against the box; such a box is wide enough that ±20px does not matter, and
  chasing pixels is not what this is for.
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
a label that is out of date refuses itself with its own message. A `label` is a *display* name:
the listing prints it over the candidate ('ping' where the button says 按下) while the stored
record keeps the name the program read — so a number from a labelled listing still resolves, and
`--name ping` reaches it through the label.

If `targets` says a window **draws itself** (Chromium/Electron like QQ, Qt like WeChat), the
picture *is* the tool face: read the text off the frame and pick the cut-out shapes by number.
OCR names are approximate (`drag_source.txt` reads as `drag_source. txt`), numbers are not. To
type into such a window, click the box first, then `type` with no `--target` — it types into
whatever has the focus. Give the shapes you will need again a `label`.

## `--intent`: when nobody can name the number

A number has to come from somewhere. Normally you read the numbered frame and pick one — that is
the design. Two cases have no number to give: the caller cannot read a picture, or several
controls answer to the same name. `--intent "点击发送按钮"` says what the gesture is *for*, and the
number is then asked of a **decider** — a local model this box may have been pointed at. athand
ships no model and knows none: it knows where one may be configured.

```bash
python skills/athand/athand.py click --hwnd 4653616 --intent "点击发送按钮"
python skills/athand/athand.py type  --hwnd 4653616 --intent "搜索框" --text "发票"
python skills/athand/athand.py decider                                        # what this box is pointed at
python skills/athand/athand.py decider --hwnd 4653616 --intent "点击发送按钮"   # ask; click nothing
```

**The weights are the switch.** A config (`decider.json` beside `athand.py`, or `$ATHAND_DECIDER`
holding the same JSON) names `url` (a resident service on loopback) and/or `ask` (a one-shot
command), plus `weights` (a path). If those weights are not on this disk the seam is **off**:

* every `--intent` is refused, and the refusal names the path it looked for;
* `--target=<n>` and `--name=<text>` keep working exactly as before — nothing else changes.

If the config says how to start the service (`serve`) and it is not answering, athand starts it
(detached, log in `%TEMP%\athand\decider.log`) and waits for the weights — measured once at 28s
(warm page cache) / 44s (cold), then 1-2s per question — and the result says how long it waited.

What crosses the seam is a question, a numbered list, and a picture: no window, no listing, no
paths of athand's (`NOTES.md` has the wire). Three consequences:

* **`UNDECIDED` is a hand-off, not a guess.** Below its own threshold the decider declines, prints
  its ranking, and names the picture it looked at (`…marked.png`, numbered the way *it* numbered
  the options). Read that picture and pass `--target <n>` yourself — the same hand-off
  `unverified`/`ESCALATED` already teach.
* **Which number it named is in the result**: `CLICK #8 [Invoke] '按下' … via decider p=1.00
  conf=1.00`. A wrong pick is then visible as data rather than as a mystery (the model is right
  most of the time and *says when it is not* — the measured 4B never picked wrong at high
  confidence, it dropped to p≈0.2, which is exactly what `UNDECIDED` is for).
* **It never writes to the caller's numbering.** The decider draws its own 1..K over the picture it
  is shown and maps back through the ids athand sent, so a number printed in a listing means what
  it always meant.

## Traps that cost real time (all measured on this box)

1. **A minimized window's geometry is a lie**: client rect `0x0`, window rect the icon slot
   `(-48000,-48000)` — and `WindowFromPoint` at that icon slot *does* answer with the window. So
   "the drop point is reachable" can only be judged after `window_state == "normal"`. `restore`
   first, then measure.
2. **A locked machine answers nothing**: with the session locked the *input desktop* is
   `Screen-saver`, `SendInput` fails with ERROR_ACCESS_DENIED, `GetCursorPos` fails,
   `GetForegroundWindow` returns 0 and the screen capture is empty. The script refuses input
   actions up front with that sentence instead of reporting "INJECTION FAILED". `windows`,
   `targets`, `label` and a window-level `shot` still work while locked — through
   `PrintWindow`, which means a **DPI-aware** window only; an unaware one has no foreground
   window to be raised to, so it is refused (trap 3).
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
11. **`type` and `key` refuse when the window cannot be brought forward**, and that is not
    fussiness: characters and keystrokes go to whatever holds the *focus*, not to the window you
    named, so injecting them from behind another window types your text into that window
    (measured 2026-09-17 — a selftest run reported a `type` as done while the control never
    changed, and the text had gone to the terminal driving the tool). A `click` needs no such
    guard: `target_problem` asks `WindowFromPoint` and refuses when the point belongs to
    something else. `key --keys win d` still works with nothing raised — the desktop and the
    taskbar are exempt, by their own rule.
12. **The door is matched by title *and* process stem, so two windows of the same program share
    it**: `restore` looks for a shell row whose name contains the window's whole title or the
    exe stem, so a second `python.exe` window is matched by the first one's taskbar button —
    `restore --hwnd <the other one>` then clicks *that* button and reports a wake through a door
    its own window never used (measured 2026-09-17). Distinct programs, distinct stems; and start
    a GUI target with `pythonw.exe`, or your own terminal's button answers for it.
13. **A tray icon is inside the overflow flyout, and the flyout grows as it fills**: a row
    rectangle read the moment the flyout appears can point a slot off — the first click landed
    on the neighbouring icon and opened *that* application's panel. The tool now waits for the
    flyout's rectangle and the row to stop moving before clicking. After a successful tray
    click the flyout is deliberately **left open**: closing it measured taking the app's own
    panel down with it (2026-09-13), so the result says "the notification flyout is still open"
    instead of tidying up.
14. **`win d` is a toggle, not "show the desktop"**: pressed while the user's windows are already
    minimized it brings them all back — which covers the desktop icon, and the tool then rightly
    refuses to click it (`the point … belongs to …`). Ask what is on top at the icon
    (`WindowFromPoint`, made DPI-aware first) and press `win d` only when the desktop is not the
    answer; that is also how you put the user's windows back afterwards.
15. **A console program renames its console — i.e. your terminal — to its command line**, and the
    title stays. Put the target's `--title` in such a command line and the terminal's own taskbar
    button starts matching the same tokens as the window, so the wake clicks the terminal instead
    of the door (measured 2026-09-17). Cheap rules: drive GUI targets with `pythonw.exe`, keep the
    target's name out of console command lines, and clean up leftovers **by window class**, never
    by a title — a leftover titled `<name> clicks=0` survived a cleanup that looked for `<name>`
    and kept answering for it.
16. **`restore --via auto` decides for you**: for a plain Win32 window it correctly prefers
    `ShowWindow`, so the shell door is never tried. When the door is what you mean to test, say so
    — `restore --via shell`.
17. **A taskbar button is a toggle for the window it belongs to**, so the entry of the window that
    is already in front minimizes it instead of waking it — the door code refuses that one
    (`it is already on screen and in front — nothing to open`). Same gesture, and worth knowing
    before you blame the door: with exactly two windows up the button opens a **thumbnail flyout**,
    the click lands on the thumbnail *image* rather than the title text, and `Escape` does not
    close it.
18. **A door can be missing entirely.** The row and the window are matched by whole words, so when
    the shell's name for an entry shares no word with any window title there is nothing to click.
    Measured: a console's row reads `智能终端 - 2 个运行窗口` while the window calls itself `π : …`.
    `WindowFromPoint` answering 0 at some taskbar coordinates is the same shape of hole.

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

The rows, the recorded baseline, the probe switches and the traps that only bite when you *change*
this tool live in [`NOTES.md`](NOTES.md) — read it before touching `athand.py`.

`python athand.py selftest` starts the probes in `probes/` and checks every gesture against what
the probe *itself* recorded (the `WM_COMMAND` a button sent, the `EM_GETSEL` an edit reported,
the window rectangle the OS gives back, the `trayclick` an app logged when its own icon was
clicked). 18 checks; `--keep` keeps the work directory with the JSONL logs, the PNGs and the
listings. A check that cannot run is reported `SKIP` with its reason, never as a pass — "the
machine is locked" and "the probe cannot hold the foreground: the machine is in use" are the two
that matter.

Two of the three doors are covered here: the taskbar button, and a tray-only window reached
through the overflow flyout (`probes/target.py --tray` is a tool window with a notification icon
and no taskbar button). The **desktop-icon door is covered by its own check**, because it costs
your screen — one shortcut on the desktop and a `win d`:

```bash
python probes/desktop_door_check.py     # ~25s, writes and removes one .lnk, puts your windows back
```

The **decider seam has its own check too**, and it needs no model at all: a stub decider answers
the same way every time, so what is checked is the seam — the question that reaches the decider,
the number athand then acts on, and that every kind of "no" (nothing configured, weights missing,
nothing listening, a malformed config, `UNDECIDED`, an id that is not in the list) arrives
**before** anything is injected. Real clicks land on its own probe window and nowhere else:

```bash
python probes/decider_check.py          # needs no model; ~5min on this box
```

It is not in `selftest` for exactly that reason; it was run twice on 2026-09-17 (all checks
passed, no leftovers).

It **injects real input and takes the foreground**, so run it when the desktop is yours. The
foreground guard exists because of a measured run: started seconds after the user came back, half
its checks failed (their windows kept coming forward over the probe) and it stole focus from the
terminal it was being driven from. If the probe cannot be raised the checks skip instead of
fighting the person for the keyboard.
