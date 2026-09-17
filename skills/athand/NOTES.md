# Notes for whoever changes this tool

[`SKILL.md`](SKILL.md) is the agent's contract and [`../../README.md`](../../README.md) is the front
page. This is the third thing: what a change here has to survive, and which of these details already
cost someone an afternoon.

## A change is only as good as the probes

There is no CI, no unit test suite and no lint gate in this repo. `selftest` plus the desktop-icon
check *are* the regression account, and they are the only oracle that can falsify "I broke click".

```bash
python skills/athand/athand.py selftest [--keep]        # ~45s, real input, takes the foreground
python skills/athand/probes/desktop_door_check.py       # ~25s, builds and removes a desktop shortcut
```

Baseline, 2026-09-17 on Windows 11 26200: `selftest` **18/18, exit 0, no SKIP** on two consecutive
runs; `desktop_door_check` **6/6, exit 0** twice, no leftovers. With the session locked, the
read-only rows still pass and every injecting row reports `SKIP` with its reason.

- **A check that cannot run is `SKIP`, never a pass.** Two concessions produce that: the input
  desktop is not this session's (`OpenInputDesktop() != "Default"`), or the probe cannot hold the
  foreground because a person is using the machine.
- **Wait for an unlocked, idle desktop** (poll `OpenInputDesktop` + `GetLastInputInfo`, look for an
  idle window of ≥45s) and write the output to a file before reading it. A run started seconds after
  its user came back had half its rows red for reasons that had nothing to do with the tool.
- **Environment refusals are not failures.** `_ENVIRONMENT_REFUSALS` in `athand.py` — `could not be
  brought to the foreground`, `could not raise the source window`, `belongs to`, `has no rectangle
  any more`, `DPI-unaware process` — are the machine's answer; a check they interrupt is skipped.

## What a call leaves on disk

One process per call, so both products of a call are written under `%TEMP%\athand` (`ATHAND_DIR`
overrides it, and it names the whole directory):

| file | contents |
|---|---|
| `<hwnd>.json` | the numbered listing, the window rectangle it was cut from, the timestamp, the numbered-frame path, and `records` — the names the program itself read |
| `<hwnd>.labels.json` | what `label` named: a semantic name → the stored record (with its rectangle) |
| `targets-0x<..>-*.png`, `shot-0x<..>-*.png`, `frame-*.png` | the pictures, path printed on stdout for the caller to read |
| `strikes.json` | strike counts, **across processes**, with a ten-minute window |

The listing's `records` must be taken **before** any label is applied. A label rewrites the stored
`name` (`'按下'` → `'ping'`), and a later `--target` resolution then matches `'ping'` against the live
tree, fails, and reports `target #8 ('ping') is not on screen any more` for a button that is right
there. `records = [_target_record(c) for c in targets]` is the fix; the label only affects display
and `--name`.

Strikes have to be cross-process, or `FAILURE_LIMIT = 3` can never be reached — one call is one
gesture, and a per-process counter resets every time.

## The two guards

1. **The lock guard.** `desktop_problem()` asks `OpenInputDesktop()` for the *name* of the input
   desktop. Anything but `Default` (locked or the screensaver is `Screen-saver`) refuses every
   action in `_INJECTING_ACTIONS`. What the caller sees is the machine's state, not a broken tool:
   `SendInput` returns 0 with `GetLastError() == 5 (ERROR_ACCESS_DENIED)`, `GetForegroundWindow()`
   is 0, and the cursor cannot be read. The name comes from
   `GetUserObjectInformationW(handle, UOI_NAME, ...)`. `windows`, `targets`, `label` and a
   window-level `shot` keep working — through `PrintWindow`, which means a DPI-aware window only.
2. **The foreground guard.** `_raise_for_input()` is used by `type` and `key` and by nothing else:
   characters and keystrokes follow the *focus*, not the window you named, so injecting them from
   behind another window types your text into whatever is in front (measured — a run reported a
   `type` as done while the text had gone to the terminal driving the tool). Clicks need no such
   guard: `target_point` asks `WindowFromPoint` and refuses when the point belongs to something
   else. The desktop and the taskbar are exempt from the guard by their own rule.

## The selftest, row by row

Each row is judged against what the **probe wrote down about itself**, never against the tool's own
`verify:` line — a button's `WM_COMMAND`, the `EM_GETSEL` an edit reports, the window rectangle the
OS gives back, the `trayclick` an application logs when its own icon is clicked. Read that `verify:`
line first when a row is red: it says *why* the gesture did not reach the control.

The named rows (`athand.py`, near `LABEL_CHECK` / `CLICK_CHECK` / …):

- a label outlives the process that gave it
- a number from a stale listing is refused, not guessed
- click reaches the button (the app logged `WM_COMMAND`)
- a click by label reaches the same button (label read by a new process)
- type puts the characters in the control (read back from the control itself)
- key presses land, and X replaces the Ctrl+A selection
- scroll uses `ScrollItemPattern`
- double-click presses twice (the app logged two clicks)
- restore brings a minimized window back
- restore wakes a tray-only window through its own tray icon
- drag `--from titlebar` moves the window by the shift it was given
- drag carries a selection the app reports (`EM_GETSEL`)
- targets reads a canvas UI off the picture

The remaining rows bring each probe up and check `windows`, `shot`, `targets`, and that the listing's
numbered frame exists — 18 rows in all on an idle, unlocked desktop.

- **`--dpi-aware` is a switch on the probe, not a nicety.** It is off by default, because a
  DPI-unaware window is what the real world mostly offers. `selftest` passes it: an unaware probe
  that loses the foreground cannot be captured at all, which reddens half the rows for purely
  environmental reasons. Keep the unaware coverage by hand-testing the refusal wording instead.
- **The canvas row races Tk.** `python` + `tkinter` + the first `update` takes about a second, so
  asking `windows` immediately after the spawn sometimes does not see it — the first run of this
  check reported 16/17 with the canvas row as the only red, and earlier runs had merely been lucky.
  Poll for the window; if the probe itself died, report its exit code rather than calling the tool
  broken.
- **The tray probe is started with `pythonw.exe`** (`_probe_python()`) — a console program renames
  its console to its command line, which gives the terminal running the test a taskbar button that
  matches the same tokens as the window the door was meant to be found for. Probes take `--log` and
  `--hwnd-file` (default `%TEMP%`), and `ATHAND_DIR` points the whole run at its own temp directory.

## The three doors, in the code

The constants live together (`TRAY_OVERFLOW_HINTS`, `TRAY_FLYOUT_CLASSES`, `DESKTOP_CLASSES`,
`TRAY_BUTTON_PREFIX`, `PINNED_HINTS`), then the matchers (`_norm`, `_mentions`, `_shell_row`,
`_entry_sized`, `_desktop_surface`), then the overflow flyout (`_tray_overflow_entry`) and finally
`at_hand` / `shell_wake` / `wake_window`.

- **Rows are matched by whole word, both directions**, after stripping zero-width characters and
  casefolding — `re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay)`, and `\w` covers CJK. Pinned
  rows and container rows are skipped (a row must be smaller than a quarter of the surface's area:
  `area * 4 < whole`, which is what keeps the full-screen `'桌面'` container row out).
- **A taskbar row has no identity beyond its name.** Reading `AutomationId` / `HelpText` through
  comtypes raises `AttributeError` every single time, so there is no AUMID or exe-level key to match
  on — a second window of the same program shares the door with the first one. `_app_tokens` is what
  the shell side matches against: the window's whole title, and the exe's stem.
- **The overflow flyout is not created on demand; it exists hidden, and its state is the
  information.** After the arrow is clicked it goes `hidden → normal` in about 0.12s, and roughly
  0.30s later the rows are in place; the arrow is a toggle. So never press `Escape` merely because
  the window exists, and wait for the flyout rectangle *and* the row rectangle to stop moving before
  clicking (a row read the moment the flyout appears points a slot off — the first click landed on
  the neighbouring icon and opened that application's panel).
- **Never click the taskbar button of the window that is already in front.** It toggles: the
  window you meant to wake gets minimized instead.
- **After a successful tray click the flyout is deliberately left open** — closing it measured
  taking the application's own panel down with it. The result says the flyout is still open instead
  of tidying up.
- **The desktop door is a double-click** (a single click only selects), and the point is checked
  with `GetAncestor(WindowFromPoint(centre), GA_ROOT) == desktop_surface` first.
- **All of these handles must be declared `restype = wintypes.HWND`**, and every return wrapped as
  `int(fn(...) or 0)`: a handle left on `c_int` turns negative above `0x7FFFFFFF` and comparisons
  fail silently; `WindowFromPoint` answering 0 means "unknown", not "desktop".
- **Known gap**: when the entry's name shares no word with any window title, no door is found. The
  example that produced this: a console's taskbar row reads `智能终端 - 2 个运行窗口` while the window
  title is a prompt of its own (`π : …`). `WindowFromPoint` answering 0 at some taskbar coordinates
  is the same shape of hole.

## Traps that cost time while changing this

1. **`Win+D` minimizes the probes too** — twice, with the user on the machine. `shot --hwnd` and
   `targets` then go red. `selftest` now restores first: `if window_state(hwnd) != "normal": restore`.
2. **Never inject from the terminal that is driving the tool.** Minimizing Windows Terminal takes the
   console away from the bash backend behind it, and the tool call that did it hangs until its full
   timeout. Keep probe windows out of the way of the terminal that started them.
3. **Reading another process's `Edit` with `GetWindowText` lies.** It returns the text from creation
   time, so a successful `type` gets judged a failure. The fix is to let the application under test
   write its own log (the probe does it in `EN_CHANGE`).
4. **`ImageGrab.grab` on a locked or pixel-less desktop raises `OSError("screen grab failed")`** —
   wrapped as `ScreenUnavailable` with the reason, so PIL's bare exception never reads as "the tool is
   broken".
5. **`athand.py windows | head` raises `OSError: [Errno 22]`** (on Windows a broken pipe is EINVAL):
   the final `print` in `main` is individually guarded so a normal ending cannot become a traceback.
6. **Clean probe windows up by window class** (`AthandTargetProbe`, `AthandDragProbe`, `TkTopLevel`),
   **never by a title substring.** The terminal driving the run carries the project name in its title
   (`Build athand.py from screen.py`), so a title filter closes the terminal you are working in — done
   once, the command died mid-run. A leftover window also keeps answering for a door: its title has
   become `<name> clicks=0`, which an exact-title cleanup misses. `selftest`'s own `finally` collects
   everything anyway; hand-written cleanup is how you hurt yourself.
7. **The probe's `log()` must tolerate a directory that has gone away**, and teardown is
   `wait` (with a timeout) → `terminate` → remove the directory; an exception inside a window
   procedure prints a traceback nobody reads.
8. **The desktop-door check asserts the tool's own sentence**, `… 桌面图标 … and it opened … instead`,
   not "the target window became normal" — the `ShowWindow` fallback satisfies the latter, so it
   proves nothing about the door.

## One fact from the wider notes that bears on this repo

`Win+D` with exactly two windows up: the taskbar button opens a *thumbnail flyout*, the click lands
on the thumbnail **image**, not on the title text, and `Escape` does not close it. That is the same
toggle in trap 14 of `SKILL.md`, seen from the shell's side.

## The public/private line

This repo is public. Measured numbers are its culture and belong here — a person's desktop does not:
no e-mail address or display name, no absolute home-directory paths, no screen or terminal geometry
from someone's own machine, no chat-client window rectangles, and no timestamps finer than a day.
Write "the probe window must not overlap the terminal driving the run", not the terminal's rectangle.

## Not done

No CI (the selftest needs a real desktop — real input, ~45s, unlocked and idle — so it cannot run on
a runner), no lint gate, no tag, no release. The desktop-icon door has no automation, by design: it
costs a desktop shortcut and a `Win+D`.
