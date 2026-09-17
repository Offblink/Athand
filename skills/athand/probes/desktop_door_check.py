"""Verify the third shell door — a desktop icon — and clean up after itself.

The 触手可及 rule has three doors: a taskbar button, a tray icon, and a desktop icon. The first
two are in `selftest`; this one is not, because it costs the **user's screen**: it writes one
shortcut to the desktop and presses `win d` (which minimises every window they have open). Run
it when the desktop is yours:

    python probes/desktop_door_check.py

What it does, and what it expects at each step:

1. an application is started **hidden** (`target.py --hidden --tool-window`): running, with no
   window on screen, no taskbar button and no tray icon — so the only door left is a shortcut;
2. `win d` shows the desktop (the tool refuses a desktop icon that something covers);
3. `restore --hwnd <hidden>` must find that shortcut as the application's own entry point and
   double-click it. A desktop icon *launches*, so the evidence is a **new visible window** with
   the application's title, and the tool's own words say "opened … instead";
4. `win d` again gives the user their windows back, the shortcut is deleted, both processes are
   closed, and the work directory goes away.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes as wt
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATHAND = HERE.parent / "athand.py"
TITLE = "athand desktop door"
SHORTCUT = f"{TITLE}.lnk"
WM_CLOSE = 0x0010

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]


def _windows() -> list[tuple[int, str, str]]:
    """(hwnd, title, state) for every top-level window, hidden ones included."""
    found: list[tuple[int, str, str]] = []
    enum = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if user32.IsWindowVisible(hwnd):
            state = "minimized" if user32.IsIconic(hwnd) else "normal"
        else:
            state = "hidden"
        found.append((hwnd, title.value, state))
        return True

    user32.EnumWindows(enum(visit), 0)
    return found


user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
PROBE_CLASS = "AthandTargetProbe"


def _state_of(hwnd: int) -> str:
    return next((state for h, _t, state in _windows() if h == hwnd), "<gone>")


def _titled(state: str) -> list[int]:
    """This check's own probe windows, found by **class name** and a title prefix.

    By class, never by a title substring alone: a leftover from an earlier run was titled
    '… clicks=0' and survived a cleanup that looked for the exact title, and its taskbar button
    then matched the same tokens as the window the door was meant to be found for — so the wake
    kept clicking that instead of the desktop icon (measured 2026-09-17).
    """
    out: list[int] = []
    for hwnd, title, status in _windows():
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if status == state and cls.value == PROBE_CLASS and title.startswith(TITLE):
            out.append(hwnd)
    return out


def _close(hwnd: int) -> None:
    with contextlib.suppress(Exception):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _wait_or_kill(proc: subprocess.Popen) -> None:
    """Let it leave on its own, then make sure it is gone — the work directory goes next, and a
    probe still writing into it would only shout tracebacks nobody reads."""
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


GA_ROOT = 2
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wt.HWND


def _top_window_at(point: tuple[int, int]) -> int:
    """The top-level window at a point — the same question the tool asks before a desktop icon
    click (`covering_window`), asked here with the same API.

    The process has to be DPI-aware first: an unaware one asks about *virtualised* coordinates,
    so a point that the tool calls (175, 1194) becomes (262, 1791) here — off the screen, and
    `WindowFromPoint` answers nothing. That cost this check three pointless `win d` presses
    before it was measured (2026-09-17).
    """
    with contextlib.suppress(Exception):
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    at = user32.WindowFromPoint(wt.POINT(point[0], point[1]))
    return int(user32.GetAncestor(at, GA_ROOT) or 0) if at else 0


def _icon_center(desktop_hwnd: str, env: dict) -> tuple[int, int] | None:
    """Where the shortcut is on the desktop, read out of the tool's own listing."""
    _code, out = _athand("targets", "--hwnd", desktop_hwnd, env=env)
    for line in out.splitlines():
        if f"'{TITLE}'" in line:
            found = re.search(r"centre=\((\d+), (\d+)\)", line)
            if found:
                return int(found.group(1)), int(found.group(2))
    return None


def _athand(*argv: str, env: dict) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(ATHAND), *map(str, argv)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetConsoleTitleW.argtypes = [wt.LPWSTR, wt.DWORD]
kernel32.GetConsoleTitleW.restype = wt.DWORD
kernel32.SetConsoleTitleW.argtypes = [wt.LPCWSTR]
kernel32.SetConsoleTitleW.restype = wt.BOOL


def _console_title() -> str:
    """The title of the console this check was started from — the user's terminal window.

    It has to be put back: every `python.exe` this check starts sets that title to its own
    command line, and those command lines carry the probe's `--title`. The title then *stays*
    that way (nothing resets it), and a shell row whose name contains the application's title
    matches the same tokens as the window — so the wake clicked the terminal's taskbar button
    instead of the desktop icon (measured 2026-09-17, and it outlived the run that did it).
    """
    buf = ctypes.create_unicode_buffer(1024)
    kernel32.GetConsoleTitleW(buf, 1024)
    return buf.value


def _set_console_title(text: str) -> None:
    with contextlib.suppress(Exception):
        kernel32.SetConsoleTitleW(text)


def _powershell(script: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _desktop() -> Path:
    return Path(_powershell("[Environment]::GetFolderPath('Desktop')"))


def _make_shortcut(desktop: Path, work: Path) -> Path:
    """One shortcut that launches the probe with a visible window."""
    python = Path(sys.executable).with_name("pythonw.exe")
    path = desktop / SHORTCUT
    arguments = (
        f'"{HERE / "target.py"}" --log "{work / "launched.jsonl"}" '
        f'--hwnd-file "{work / "launched.hwnd"}" --title "{TITLE}" --tool-window'
    )
    _powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; $s.Arguments = '%s'; $s.WorkingDirectory = '%s'; $s.Save()"
        % (path, python if python.exists() else sys.executable, arguments, HERE)
    )
    return path


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="athand-desktop-door-"))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "ATHAND_DIR": str(work / "data")}
    failures = 0
    shortcut: Path | None = None
    hidden: subprocess.Popen | None = None
    centre: tuple[int, int] | None = None
    original_title = _console_title()

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True)

    def desktop_hwnd() -> str:
        _code, out = _athand("windows", "--include", "all", env=env)
        row = re.search(r"hwnd=(\d+).*Program Manager", out)
        return row.group(1) if row else ""

    try:
        desktop = _desktop()
        print(f"desktop: {desktop}")
        print(f"console title at start: {original_title!r}")
        shortcut = _make_shortcut(desktop, work)
        check("the shortcut is on the desktop", shortcut.exists(), shortcut.name)

        hwnd_file = work / "hidden.hwnd"
        # `pythonw.exe`, not `python.exe`: CPython renames its console to the command line, and
        # this command line carries `--title "athand desktop door"` — which makes the *terminal*
        # that runs this check look like a door for that window, and the wake clicks the
        # terminal's taskbar button instead of the desktop icon (measured 2026-09-17). No
        # console, no renamed window, and the desktop icon is the only door left.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        hidden = subprocess.Popen(
            [
                str(pythonw if pythonw.exists() else sys.executable),
                str(HERE / "target.py"),
                "--log",
                str(work / "hidden.jsonl"),
                "--hwnd-file",
                str(hwnd_file),
                "--title",
                TITLE,
                "--tool-window",
                "--hidden",
                "--dpi-aware",
            ]
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not hwnd_file.exists():
            time.sleep(0.1)
        hidden_hwnd = int(hwnd_file.read_text(encoding="utf-8").strip())
        state = _state_of(hidden_hwnd)
        check(
            "the application runs with nothing on screen and no other door",
            state == "hidden",
            f"hwnd=0x{hidden_hwnd:X} is {state}, WS_EX_TOOLWINDOW, no tray icon",
        )

        # win d: the desktop has to be what is on screen, or the icon is covered and refused
        # (that refusal is the tool being right — it is what the check in `SKIP` says).
        row = desktop_hwnd()
        check("the desktop window is listed", bool(row), f"hwnd={row}")
        centre = _icon_center(row, env)
        check("the shortcut shows up as a desktop icon", centre is not None, f"centre={centre}")
        # `win d` is a *toggle*: pressed when the user's windows are already down it brings them
        # all back — which covers the icon and makes the tool refuse (measured 2026-09-17, the
        # first version of this check did exactly that and the wake fell back to ShowWindow).
        # So ask the same question the tool asks — what is on top at the icon — and press only
        # when the desktop is not the answer.
        presses = 0
        for _ in range(3):
            if centre is not None and _top_window_at(centre) == int(row):
                break
            _athand("key", "--hwnd", row, "--keys", "win", "d", env=env)
            presses += 1
            time.sleep(0.9)
        showing = centre is not None and _top_window_at(centre) == int(row)
        check(
            "the desktop is showing, so the icon is reachable",
            showing,
            f"{presses} win d press(es); the window at {centre} is 0x{_top_window_at(centre):X}",
        )

        # `--via shell` is the tool's own switch for "take the application's door, do not judge
        # for me": `auto` reads the window first and, for a plain Win32 window, rightly decides
        # ShowWindow is enough — which is not the door under test (measured 2026-09-17).
        _set_console_title(original_title)  # nothing of this check may look like the window
        code, out = _athand("restore", "--hwnd", hidden_hwnd, "--via", "shell", env=env)
        door = next((line.strip() for line in out.splitlines() if "桌面图标" in line), "")
        launched = _titled("normal")
        if not door:
            # The tool's words are the evidence: without them this check can only guess why the
            # door was not taken, so show the whole answer.
            print("      the tool said:\n" + "\n".join(f"        {line}" for line in out.splitlines()))
        check(
            "restore opens the application through its desktop icon",
            code == 0 and door != "" and bool(launched),
            f"exit {code}; {len(launched)} window(s) titled {TITLE!r} on screen",
        )
        check("the tool says which door it used", door != "", door[:140] or "(no door line)")
        return 1 if failures else 0
    finally:
        # Give the user their screen back — but only if this check is what took it away: `win d`
        # toggles, so pressing it with their windows already up would *hide* them instead.
        row = desktop_hwnd()
        if row and centre is not None and _top_window_at(centre) == int(row):
            _athand("key", "--hwnd", row, "--keys", "win", "d", env=env)
            print("win d again: the windows you had open are back")
        for hwnd in _titled("normal") + _titled("hidden") + _titled("minimized"):
            _close(hwnd)
        if hidden is not None:
            _wait_or_kill(hidden)
        if shortcut is not None and shortcut.exists():
            with contextlib.suppress(OSError):
                shortcut.unlink()
            print(
                f"removed {shortcut.name}"
                if not shortcut.exists()
                else f"COULD NOT REMOVE {shortcut} — delete it by hand"
            )
        shutil.rmtree(work, ignore_errors=True)
        _set_console_title(original_title)
        print(f"console title back to {original_title!r}")


if __name__ == "__main__":
    sys.exit(main())
