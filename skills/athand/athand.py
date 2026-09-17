"""athand — the desk at hand: look at this machine's desktop and act on it.

A script, not a service (user, 2026-09-17: "我真的只想做 skill+脚本"): one file, a handful of
subcommands, no daemon, no MCP server, no CLI product. Every call is a fresh process, so
what has to survive between calls is written to `%TEMP%\athand`: the numbered listing of a
window (`<hwnd>.json`, read back by the next call) and the pictures (`*.png`, printed by
path — pixels cannot travel through stdout).

The load-bearing decisions, carried over verbatim from the tool this was cut out of
(Fungi's `fungi/tools/screen.py`; the measurements behind them live in that repo's
`docs/spec.md` §35-§46, cited in the comments below as `spec §NN`):

- **Coordinates are never the model's.** Every gesture names a target from `targets` (the
  program-side a11y/OCR/shape candidates), so the pixel comes from the OS or from the
  program's own cut-outs. Measured 2026-09-13: model-produced coordinates miss by 15-68px;
  picking a candidate number hit 3/3.
- **Every input action verifies itself** against program-side truth (a11y value, focus,
  window rectangle, frame diff). A strike counter stops the third fruitless attempt — not
  by asking a user (a script has none) but by saying so and refusing to retry.
- **触手可及**: opening a window goes through the application's *own* on-screen entry point —
  its taskbar button, its tray icon or its desktop icon — because that is the path the
  application listens to. `ShowWindow` is the fallback for an app with no entry at all, and
  its result is the "visible but asleep" window.
- **Text is typed**, one character at a time, and the clipboard is never touched.
- An elevated window (UAC secure desktop) stays out of reach: UIPI. That is the hard
  boundary, not an implementation gap.

Windows only. The architecture ports; the measurements do not.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import ctypes.wintypes as wt
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageGrab, ImageStat

FAILURE_LIMIT = 3  # strikes before the run stops trying and says so (Fungi asked its user
# here; a script has nobody to ask, so this is where `ESCALATED:` comes from)
MAX_CANDIDATES = 60
DIFF_THRESHOLD = 0.002  # fraction of changed pixels that counts as an effect
SETTLE_S = 0.30  # let the app repaint before the verifying frame
DOUBLE_CLICK_GAP_S = 0.06  # well inside the system's double-click time (default 0.5s)
MOVE_STEPS = 14  # intermediate points on the way to a click target — a glide, not a teleport
MOVE_DURATION_S = 0.22  # total travel time: a person's flick, and small against SETTLE_S
MOVE_TOLERANCE_PX = 2  # measured round-trip error of the 0..65535 space: 0px, -1px at a corner
# A drag is the one gesture whose path matters to the application (spec §46): the button
# is down, so every point on the way is a position the app may act on, and a drop target
# decides on the hover before the release.
DRAG_MIN_STEPS = 12  # even a short carry is a path, not a jump
DRAG_MAX_STEPS = 48  # …and a long one stops here (48 points x 12ms = 0.6s of travel)
DRAG_PX_PER_STEP = 12  # the path's resolution: about one point per 12px of travel
DRAG_STEP_SLEEP_S = 0.012  # between points — the prototype's warning is that a denser
# stream gets coalesced on the application's side, so the pace is a calibrated one
DRAG_HOLD_S = 0.08  # between the press and the first move: the press has to be received
DRAG_DWELL_S = 0.25  # hover the drop point before letting go, so a drop target lights up
DRAG_VIA_WAIT_S = 3.0  # hovering a taskbar button until the shell brings its window forward:
# the shell's own drag-over-taskbar activation is what a person relies on (user, 2026-09-17),
# and it takes about a second — the window is not asked to appear, it is waited for
DRAG_VIA_POLL_S = 0.15  # how often that wait re-reads whether the drop point is reachable
STRIKE_WINDOW_S = 600  # how long a fruitless attempt counts against the next one: the counter
# outlives the process (a run performs one action, so it has to), and "three in a row" must
# not mean "three spread over a day"
SM_CYCAPTION = 4  # GetSystemMetrics: the height of a window's own title bar
BUTTON_VK = {"left": 0x01, "right": 0x02}  # GetAsyncKeyState: is the button still down?
TYPE_CHAR_DELAY_S = 0.15  # 逐字输入: the gap between characters — a *watchable* pace
# (user 2026-09-14: five to ten characters a second, and 0.15 lands mid-band). 0.03 (33 a
# second) is a blur: the text appears as if it had been pasted, which is the thing this
# style exists to avoid. The knob stays — `char_delay` overrides it per call (spec §37).
LAUNCH_WAIT_S = 3.0  # a gesture that starts a process: its window is not up immediately
SHOT_MAX_DIM = 1568  # same vision sweet spot as files.IMAGE_MAX_DIM

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi = ctypes.WinDLL("gdi32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_u32.GetSystemMetrics.argtypes = [ctypes.c_int]
_u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_u32.GetSystemMetrics.restype = ctypes.c_int
_u32.GetWindowDC.argtypes = [wt.HWND]
_u32.GetWindowDC.restype = ctypes.c_void_p
_u32.ReleaseDC.argtypes = [wt.HWND, ctypes.c_void_p]
_u32.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_uint]
_u32.PrintWindow.restype = wt.BOOL
_u32.GetClipboardData.argtypes = [ctypes.c_uint]
_u32.GetClipboardData.restype = ctypes.c_void_p
_u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_u32.SetClipboardData.restype = ctypes.c_void_p
_gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.SelectObject.restype = ctypes.c_void_p
_gdi.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi.GetDIBits.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint,
]
_k32.OpenProcess.argtypes = [ctypes.c_uint, wt.BOOL, ctypes.c_uint]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

# Every one of these returns a window handle: left to ctypes' default (c_int) a
# handle above 0x7FFFFFFF comes back negative, and comparing it with a window id
# then silently fails. Declared here for the ones this module reads.
_u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
_u32.FindWindowW.restype = wt.HWND
_u32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
_u32.FindWindowExW.restype = wt.HWND
_u32.WindowFromPoint.argtypes = [wt.POINT]
_u32.WindowFromPoint.restype = wt.HWND
_u32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_u32.GetAncestor.restype = wt.HWND
_u32.GetForegroundWindow.restype = wt.HWND
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_u32.GetCursorPos.restype = wt.BOOL
# A held mouse button is verified against the system, not against our own bookkeeping
# (SHORT, and negative when the key is down: the high bit is what matters).
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short
_u32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
_u32.PostMessageW.restype = wt.BOOL
_u32.SetWindowPos.argtypes = [
    wt.HWND,
    wt.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
_u32.SetWindowPos.restype = wt.BOOL
WM_CLOSE = 0x0010
SWP_NOSIZE, SWP_NOZORDER = 0x0001, 0x0004

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2
_u32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_u32.OpenInputDesktop.restype = ctypes.c_void_p
_u32.CloseDesktop.argtypes = [ctypes.c_void_p]
_u32.GetUserObjectInformationW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]


class ScreenUnavailable(RuntimeError):
    """The machine cannot be driven from this process right now — and saying which way is
    half the answer: an injection that cannot land and a screen that cannot be read are the
    same failure to the caller ("nothing happened"), and its two causes (a lock screen, a
    session that is not the console's) need different things from the user."""


def input_desktop_name() -> str:
    """The desktop that currently receives input: 'Default' when the user is at the machine,
    something else ('Screen-saver', 'Winlogon') when a lock screen or a screensaver owns it.

    Measured on this box 2026-09-17, with the session locked: the input desktop was
    'Screen-saver' while this process sat on 'Default', and from there `SendInput` fails with
    ERROR_ACCESS_DENIED, `GetCursorPos` fails, `GetForegroundWindow` returns 0 and the screen
    capture comes back empty — every one of which the tool used to report as its own fault
    ("INJECTION FAILED"), with no hint that the user is simply not there.
    """
    handle = _u32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wt.DWORD()
        if not _u32.GetUserObjectInformationW(
            handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(need)
        ):
            return ""
        return buf.value
    finally:
        _u32.CloseDesktop(handle)


def desktop_problem() -> str | None:
    """Why this machine's desktop cannot be acted on at all, or None."""
    name = input_desktop_name()
    if name and name.casefold() != "default":
        return (
            f"the input desktop is {name!r}, not this session's 'Default' — the machine is "
            "locked or a screensaver is up. No key or click can be injected and no screen "
            "pixels can be read until the user is back; nothing was sent."
        )
    return None


def _ensure_dpi() -> None:
    """One-shot: make screen coordinates and captured pixels the same space.

    A DPI-unaware process (a CLI run, or the exe) reads GetSystemMetrics in
    logical units while ImageGrab returns physical ones — measured on this box
    2026-09-13: 1493x933 vs 2240x1400. Everything after this call is physical
    pixels, so a rect read from a11y is a pixel in the frame. Qt already sets
    per-monitor-v2 for the GUI, in which case this call just fails harmlessly.
    """
    if getattr(_ensure_dpi, "_done", False):
        return
    _ensure_dpi._done = True  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        _u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2


def _virtual_origin() -> tuple[int, int]:
    return (
        int(_u32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(_u32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
    )


def _virtual_size() -> tuple[int, int]:
    return (
        int(_u32.GetSystemMetrics(SM_CXVIRTUALSCREEN)) or 1,
        int(_u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)) or 1,
    )


# ── window identity: hwnd + process, never a title match ────────────────────
@dataclass(frozen=True)
class Win:
    hwnd: int
    title: str
    cls: str
    rect: tuple[int, int, int, int]
    pid: int
    proc: str
    state: str = "normal"  # normal | minimized | hidden | untitled

    @property
    def size(self) -> str:
        return f"{self.rect[2] - self.rect[0]}x{self.rect[3] - self.rect[1]}"


def _window_text(hwnd: int) -> str:
    n = _u32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        _k32.CloseHandle(ctypes.c_void_p(handle))


def _keep_window(state: str, title: str, *, include_hidden: bool) -> bool:
    """Which top-level windows a listing shows.

    Default: what the user can see or un-minimize. Asked for: the tray-resident
    ones too (a hidden window with a title), plus the untitled system surfaces
    the taskbar is made of — those are how a tray-only application is reached.
    """
    if not include_hidden:
        return state != "hidden" and bool(title)
    return state == "normal" or bool(title)


def list_windows(include_hidden: bool = False) -> list[Win]:
    """Top-level windows in z-order.

    Default: what the user can see and name. `include_hidden=True` adds the
    windows that live in the notification area (a tray-only app keeps a hidden
    or minimized window with a title) and the untitled system windows the taskbar
    is made of — without which there is no way to hand the model an hwnd for
    either (spec §35.7).
    """
    _ensure_dpi()
    out: list[Win] = []
    callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        state = window_state(hwnd)
        title = _window_text(hwnd)
        if not _keep_window(state, title, include_hidden=include_hidden):
            return True
        rect = window_rect(hwnd)
        if rect is None:
            return True
        # A minimized window's GetWindowRect is its icon slot (-48000,-48000 measured
        # on this box): read the restored geometry *before* the size filter, or every
        # minimized window is thrown out as icon-sized plumbing — which made the
        # every-window view lose exactly the windows `restore` exists for, and made
        # `shell_wake` answer "the window is gone" for them (2026-09-13).
        if state == "minimized" and (restored := normal_rect(hwnd)) is not None:
            rect = restored
        # In the every-window view, skip icon-sized plumbing (1x1 bridges, driver
        # status windows): measured 59 rows -> 40 useful ones on this box.
        if include_hidden and (rect[2] - rect[0] < 40 or rect[3] - rect[1] < 40):
            return True
        pid = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append(
            Win(
                int(hwnd),
                title,
                _class_name(hwnd),
                rect,
                int(pid.value),
                _process_name(pid.value),
                state if title else "untitled",
            )
        )
        return True

    _u32.EnumWindows(callback(visit), 0)
    return out


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    # Every coordinate that leaves this module is physical, so the awareness
    # switch has to happen before the first read — not merely before the first
    # capture. Measured 2026-09-13: a rect read while the process was still
    # DPI-unaware came back 300,200 where the same window was at 450,300.
    _ensure_dpi()
    rect = wt.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    box = (rect.left, rect.top, rect.right, rect.bottom)
    return box if box[2] > box[0] and box[3] > box[1] else None


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", wt.POINT),
        ("ptMaxPosition", wt.POINT),
        ("rcNormalPosition", wt.RECT),
    ]


def window_state(hwnd: int) -> str:
    """normal | minimized | hidden | untitled.

    A minimized window is still 'visible' to IsWindowVisible and keeps its title
    — that is how a window pushed into the notification area looks from outside,
    and why the state has to be reported instead of guessed from visibility.
    """
    if not _u32.IsWindowVisible(hwnd):
        return "hidden"
    if _u32.IsIconic(hwnd):
        return "minimized"
    return "normal"


def normal_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's restored geometry — a minimized window's GetWindowRect is
    its icon slot (-48000,-48000 measured on this box 2026-09-13), which would
    make a captured frame a 356x59 smear."""
    _ensure_dpi()
    _u32.GetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if not _u32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return None
    rect = placement.rcNormalPosition
    box = (rect.left, rect.top, rect.right, rect.bottom)
    return box if box[2] > box[0] and box[3] > box[1] else None


def ensure_on_screen(hwnd: int) -> str:
    """Bring a minimized or tray-hidden window back, and say what it was.

    `SW_RESTORE` shows a hidden window too, so this is also the way to wake an
    application that lives only in the notification area (spec §35.7). Both
    `targets` and every input action need it: a minimized window's controls keep
    off-screen rectangles and cannot be clicked where they claim to be.
    """
    state = window_state(hwnd)
    if state == "normal":
        return state
    _u32.ShowWindow(hwnd, SW_RESTORE)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and window_state(hwnd) != "normal":
        time.sleep(0.1)
    time.sleep(0.15)  # let it repaint before anyone measures or captures it
    return state


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's client area in screen coordinates.

    This is where an application draws its own controls; the title-bar buttons sit
    outside it. That distinction is what decides whether a window needs the
    picture tiers at all (spec §35.1): a window whose only *actionable* a11y
    elements are its window buttons is a self-drawn surface.
    """
    _ensure_dpi()
    rect = wt.RECT()
    if not _u32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wt.POINT(0, 0)
    if not _u32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom)


def inside_client(hwnd: int, target: Target) -> bool:
    client = client_rect(hwnd)
    if client is None:
        return True  # unknown: do not pretend the tiers are needed
    centre_x, centre_y = target.center
    return client[0] <= centre_x <= client[2] and client[1] <= centre_y <= client[3]


def foreground_hwnd() -> int:
    return int(_u32.GetForegroundWindow() or 0)


def set_foreground(hwnd: int) -> bool:
    """Raise the target before clicking: otherwise the click lands on whatever
    covers it (a hit on the wrong window measured in the prototype)."""
    if foreground_hwnd() == hwnd:
        return True
    _u32.ShowWindow(hwnd, SW_RESTORE)
    _u32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return foreground_hwnd() == hwnd

# ── pixels: capture ────────────────────────────────────────────────────────
@dataclass
class Frame:
    """A captured bitmap plus the screen coordinate of its pixel (0,0)."""

    image: Image.Image
    origin: tuple[int, int]
    hwnd: int | None
    ts: float

    def screen_rect(self) -> tuple[int, int, int, int]:
        ox, oy = self.origin
        return (ox, oy, ox + self.image.width, oy + self.image.height)

    def to_local(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        ox, oy = self.origin
        return (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def grab_screen() -> Frame:
    """Whole virtual desktop. Below IMAGE_MAX_DIM on a typical box, so the model
    gets near-native pixels; still call the window shot for small text.

    Raises `ScreenUnavailable` rather than PIL's bare "screen grab failed" when this process
    is not on the input desktop (measured 2026-09-17 with the session locked): the pixels are
    not blank, they are unavailable, and the caller has to be told which of the two it is.
    """
    _ensure_dpi()
    try:
        img = ImageGrab.grab(all_screens=True).convert("RGB")
    except OSError as exc:
        raise ScreenUnavailable(
            desktop_problem()
            or f"the screen could not be captured ({exc}) — a locked, disconnected or "
            "otherwise headless session gives the display device nothing to read"
        ) from exc
    return Frame(img, _virtual_origin(), None, time.time())


def _dpi_unaware(hwnd: int) -> bool:
    """True when the target's process is DPI-unaware.

    Windows then gives it a *scaled* physical rect while its client area still
    draws at 100%, and PrintWindow returns that unscaled content in the top-left
    of a full-size bitmap: half black, with every control rectangle off by the
    scale factor (measured on this box 2026-09-13, 2240px screen at 150%).
    """
    with contextlib.suppress(Exception):
        ctx = _u32.GetWindowDpiAwarenessContext(hwnd)
        return bool(_u32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-1)))  # UNAWARE
    return False


def _print_window(hwnd: int, rect: tuple[int, int, int, int]) -> Frame | None:
    """PrintWindow(PW_RENDERFULLCONTENT): captures a window that is behind
    another one without stealing focus (verified on Win11 Notepad, 2026-09-13)."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    hdc = _u32.GetWindowDC(hwnd)
    mem = _gdi.CreateCompatibleDC(hdc)
    bmp = _gdi.CreateCompatibleBitmap(hdc, width, height)
    old = _gdi.SelectObject(mem, bmp)
    try:
        if not _u32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
            _u32.PrintWindow(hwnd, mem, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        info = _BITMAPINFOHEADER(
            ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0
        )
        if not _gdi.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(info), 0):
            return None
        image = Image.frombytes("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
    finally:
        _gdi.SelectObject(mem, old)
        _gdi.DeleteObject(bmp)
        _gdi.DeleteDC(mem)
        _u32.ReleaseDC(hwnd, hdc)
    return Frame(image, (rect[0], rect[1]), hwnd, time.time())


def capture_problem(hwnd: int) -> str | None:
    """Why this window's pixels cannot be captured right now, or None.

    The one case that has no honest answer: a DPI-unaware process that is behind
    another window. PrintWindow then returns the content at 1:1 inside a
    scaled-size bitmap — 58% black padding with every control off by the scale
    factor (measured 2026-09-13) — and the picture cannot be trusted for picking
    targets. Raising the window without activating does not move the z-order (the
    shell ignores it; WindowFromPoint still reports the covering window), so the
    fix belongs to the caller: bring it to the front, which is what restore and
    every input action do.
    """
    if _dpi_unaware(hwnd) and foreground_hwnd() != hwnd:
        return (
            f"window 0x{hwnd:X} {_window_text(hwnd)!r} runs in a DPI-unaware process and is "
            "currently behind another window, where Windows hands back a scaled stub instead of "
            "its pixels. Bring it to the front first: restore "
            f"(hwnd={hwnd}) — that does it and is also how a minimized or tray-resident window "
            "comes back."
        )
    return None


def grab_window(hwnd: int) -> Frame | None:
    """A window's pixels, in the same coordinate space as its a11y rectangles.

    A DPI-unaware window is cropped out of the screen capture: that is the only
    path that gives the pixels at the scale the user sees. Checked callers ask
    `capture_problem` first, so the unanswerable case (unaware *and* covered)
    becomes an explicit error instead of a silently misaligned picture.
    """
    _ensure_dpi()
    rect = window_rect(hwnd)
    if rect is None:
        return None
    if _dpi_unaware(hwnd):
        if foreground_hwnd() != hwnd:
            return None
        screen = grab_screen().image
        origin = _virtual_origin()
        box = (rect[0] - origin[0], rect[1] - origin[1], rect[2] - origin[0], rect[3] - origin[1])
        return Frame(screen.crop(box), (rect[0], rect[1]), hwnd, time.time())
    frame = _print_window(hwnd, rect)
    if frame is not None and foreground_hwnd() != hwnd:
        low, high = frame.image.convert("L").getextrema()
        if low == high:
            # A single-colour bitmap is not "an empty window": some renderers
            # (Chromium/Electron surfaces) hand PrintWindow nothing at all while
            # they sit behind another window. Passing that on would tell the model
            # "the panel is blank" — a wrong answer shaped like a real one, which
            # cost a user an hour on 2026-09-13.
            return None
    return frame


def _diff_ratio(before: Frame, after: Frame) -> float:
    """Fraction of pixels that changed — the 'did that do anything' signal when
    a control exposes no readable state. PIL does the diff in C; a pure-Python
    per-pixel loop over 1M pixels would cost more than the action itself."""
    if before.image.size != after.image.size:
        return 1.0
    width = min(before.image.width, after.image.width)
    height = min(before.image.height, after.image.height)
    left = before.image.crop((0, 0, width, height)).convert("L")
    right = after.image.crop((0, 0, width, height)).convert("L")
    diff = ImageChops.difference(left, right).point(lambda p: 255 if p > 24 else 0)
    return ImageStat.Stat(diff).mean[0] / 255.0

# ── what a run leaves behind: %TEMP%\athand ────────────────────────────────
# A script has no memory between calls, so the two things one call produces are written
# down instead of held in a process: the numbered listing (`<hwnd>.json`, read back by the
# next call) and the picture — a PNG whose *path* is printed, because that is the only shape
# a picture can take in stdout.
def _data_dir() -> Path:
    """`%TEMP%\\athand`, or `ATHAND_DIR` verbatim.

    The override is the test hook: a selftest run then leaves nothing of its own in the
    user's directory, and every child it starts is pointed at the same place.
    """
    override = os.environ.get("ATHAND_DIR")
    if override:
        return Path(override)
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return Path(temp) / "athand"


DATA_DIR = _data_dir()


def _data_file(stem: str, suffix: str) -> Path:
    """A file of its own in DATA_DIR: the name carries the moment and two random hex
    digits, so two calls in the same second cannot overwrite each other's picture."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}{suffix}"


def _attach(summary: str, frame: Frame, stem: str = "frame") -> str:
    """The summary plus where to look at the pixels: a path, not a blob."""
    path = _data_file(stem, ".png")
    frame.image.save(path, "PNG")
    return (
        f"{summary}\n"
        f"[frame: {path} ({frame.image.width}x{frame.image.height} PNG) — read that path to "
        "look at it]"
    )

INK_CONTRAST = 8  # a pixel this much darker than its neighbourhood counts as ink
INK_CLOSE = 9  # closing kernel: the strokes of one glyph become one blob
MIN_SIDE, MAX_SIDE = 8, 900  # the prototype's candidate size window (pixels)


def _visual_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Binarise *inside the program* and hand back boxes — never the binary image.

    The prototype measured why (2026-09-13): a thresholded picture sent to the
    model cost 21-296px of error and 3x the latency, while the same threshold fed
    to the *algorithm* produced 94 candidates on a real desktop, 75 of them
    carrying text of their own. So the threshold stops here and the model only
    ever chooses between numbers.

    Pillow does the pixels (BoxBlur / MaxFilter / MinFilter / point run in C), the
    components are run-length + union-find, and OpenCV stays out of the install.
    """
    grey = image.convert("L")
    # adaptive threshold (mean of a ~21px neighbourhood, C=8) built from C-speed
    # parts: ink is a pixel at least INK_CONTRAST levels darker than its area
    local = grey.filter(ImageFilter.BoxBlur(10))
    ink = ImageChops.subtract(local, grey).point(lambda v: 255 if v >= INK_CONTRAST else 0)
    # close it, so the strokes of one glyph become one blob
    ink = ink.filter(ImageFilter.MaxFilter(INK_CLOSE)).filter(ImageFilter.MinFilter(INK_CLOSE))
    # Filter *before* merging: the window's own border is a connected ring whose
    # bounding box is the whole frame, and merging first let it swallow every real
    # candidate (measured on the canvas probe, 2026-09-13). A box that spans the
    # frame is chrome or background, never a control.
    width, height = image.size
    return _merge_boxes(
        [
            box
            for box in _components(ink)
            if MIN_SIDE <= box[2] - box[0] <= MAX_SIDE
            and MIN_SIDE <= box[3] - box[1] <= MAX_SIDE
            and not (box[2] - box[0] >= 0.9 * width and box[3] - box[1] >= 0.9 * height)
        ]
    )


def _components(mask: Image.Image) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of a mask's connected components.

    Run-length per row plus union-find across rows: two runs that overlap in x are
    the same blob. Only runs are touched, so a 780x690 window costs a few hundred
    milliseconds in pure Python.
    """
    width, height = mask.size
    data = mask.tobytes()
    parent: dict[int, int] = {}
    runs: list[tuple[int, int, int, int]] = []  # (id, y, x0, x1)

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path compression
            parent[node], node = root, parent[node]
        return root

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    previous: list[tuple[int, int, int, int]] = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        current: list[tuple[int, int, int, int]] = []
        start = row.find(b"\xff")
        while start != -1:
            end = row.find(b"\x00", start)
            if end == -1:
                end = width
            run_id = len(parent)
            parent[run_id] = run_id
            current.append((run_id, y, start, end))
            start = row.find(b"\xff", end)
        for run_id, _y, x0, x1 in current:
            for other, _other_y, other_x0, other_x1 in previous:
                if x0 < other_x1 and other_x0 < x1:
                    union(run_id, other)
        runs.extend(current)
        previous = current
    boxes: dict[int, list[int]] = {}
    for run_id, y, x0, x1 in runs:
        box = boxes.setdefault(find(run_id), [x0, y, x1, y + 1])
        box[0], box[2] = min(box[0], x0), max(box[2], x1)
        box[1], box[3] = min(box[1], y), max(box[3], y + 1)
    return [tuple(box) for box in boxes.values()]


def _merge_boxes(
    boxes: list[tuple[int, int, int, int]], *, gap: int = 6
) -> list[tuple[int, int, int, int]]:
    """Fold the blobs that belong to one control together, drop the noise.

    A smiley is three blobs and a list row a dozen: anything within `gap` pixels
    is the same candidate. Icon-sized and window-sized boxes are not candidates —
    the prototype's 8..900 filter, which is also what keeps the frame border and
    the scrollbar out of the list.
    """
    merged = list(boxes)
    changed = True
    while changed:
        changed = False
        out: list[tuple[int, int, int, int]] = []
        for box in merged:
            for index, other in enumerate(out):
                if (
                    box[0] - gap <= other[2]
                    and other[0] - gap <= box[2]
                    and box[1] - gap <= other[3]
                    and other[1] - gap <= box[3]
                ):
                    out[index] = (
                        min(box[0], other[0]),
                        min(box[1], other[1]),
                        max(box[2], other[2]),
                        max(box[3], other[3]),
                    )
                    changed = True
                    break
            else:
                out.append(box)
        merged = out
    return merged


def _visual_targets(frame: Frame, start: int = 0, limit: int = 40) -> list[Target]:
    """The numbered candidates cut out of the picture, in screen coordinates."""
    origin_x, origin_y = frame.origin
    out: list[Target] = []
    for box in _visual_boxes(frame.image)[:limit]:
        out.append(
            Target(
                start + len(out) + 1,
                "",
                "",
                (origin_x + box[0], origin_y + box[1], origin_x + box[2], origin_y + box[3]),
                (),
                "visual",
            )
        )
    return out


def _mark_targets(frame: Frame, targets: list[Target]) -> Image:
    """Draw the numbers onto the frame: the model sees the picture it is choosing
    from, carrying the same numbers the listing printed."""
    marked = frame.image.copy()
    draw = ImageDraw.Draw(marked)
    for target in targets:
        left, top, right, bottom = frame.to_local(target.rect)
        draw.rectangle(
            (left, top, max(left + 1, right - 1), max(top + 1, bottom - 1)), outline=(255, 0, 0)
        )
        draw.text((left + 2, max(0, top + 1)), str(target.n), fill=(255, 255, 0))
    return marked


# ── a11y: UI Automation, the coordinate source that has no error ────────────
_thread_local = threading.local()


def _uia():
    """Per-thread UIA instance: COM objects are apartment-bound, and the agent
    runs tool calls on worker threads."""
    cached = getattr(_thread_local, "uia", None)
    if cached is not None:
        return cached
    try:
        import comtypes.client  # noqa: PLC0415 (heavy: only for desktop work)
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise RuntimeError(
            "screen control needs the comtypes package (UIAutomation); "
            "install it with: pip install comtypes"
        ) from exc
    comtypes.client.GetModule("UIAutomationCore.dll")
    # The type-library wrapper module is generated on the spot by GetModule, so
    # it is looked up by name instead of with an import statement.
    uia_client = importlib.import_module("comtypes.gen.UIAutomationClient")

    automation = comtypes.client.CreateObject(
        uia_client.CUIAutomation, interface=uia_client.IUIAutomation
    )
    _thread_local.uia = (automation, uia_client)
    return _thread_local.uia


def _pattern_labels(uia_client) -> tuple[tuple[int, str], ...]:
    return (
        (uia_client.UIA_InvokePatternId, "Invoke"),
        (uia_client.UIA_ValuePatternId, "Value"),
        (uia_client.UIA_SelectionItemPatternId, "Select"),
        (uia_client.UIA_ExpandCollapsePatternId, "ExpandCollapse"),
        (uia_client.UIA_ScrollItemPatternId, "ScrollItem"),
    )


def _child_of(walker, element):
    with contextlib.suppress(Exception):
        return walker.GetFirstChildElement(element)
    return None


def _sibling_of(walker, element):
    with contextlib.suppress(Exception):
        return walker.GetNextSiblingElement(element)
    return None


def _has_pattern(element, pattern_id: int) -> bool:
    try:
        return bool(element.GetCurrentPattern(pattern_id))
    except Exception:
        return False


@dataclass
class Target:
    """One selectable thing inside a window: the unit the model names instead of
    a coordinate."""

    n: int
    name: str
    cls: str
    rect: tuple[int, int, int, int]
    patterns: tuple[str, ...] = ()
    source: str = "a11y"

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def label(self) -> str:
        if self.source == "visual":
            kind, name = "[shape]", self.name or "(no text)"
        else:
            kind = f"[{'+'.join(self.patterns)}]" if self.patterns else "[text]"
            name = self.name or self.cls or "(unnamed)"
        number = f"#{self.n} " if self.n else ""  # a label carries no listing number
        return (
            f"{number}{kind} {name!r} cls={self.cls or '-'} rect={self.rect} centre={self.center}"
        )


def _scan(hwnd: int, limit: int = MAX_CANDIDATES) -> list[tuple[Target, Any]]:
    """Elements inside one window: (numbered target, live UIA element).

    Walks from the window element itself — never from the desktop root, which
    would mix in another window's controls (a wrong-click bug in the prototype)
    and cost an order of magnitude more time.
    """
    _ensure_dpi()
    automation, uia_client = _uia()
    try:
        window = automation.ElementFromHandle(hwnd)
    except Exception:
        return []
    if window is None:
        return []
    table = _pattern_labels(uia_client)
    walker = automation.RawViewWalker
    found: list[tuple[str, str, tuple[int, int, int, int], tuple[str, ...], Any]] = []

    def walk(element, depth: int) -> None:
        # UIA providers raise COM errors on windows that are minimized, closing
        # or gone ('invalid pointer' measured on a minimized window 2026-09-13):
        # a failed child lookup ends that branch, it never kills the scan.
        child = _child_of(walker, element)
        while child is not None and len(found) < limit * 6:
            try:
                rect = child.CurrentBoundingRectangle
                box = (rect.left, rect.top, rect.right, rect.bottom)
                if box[2] > box[0] and box[3] > box[1]:
                    patterns = tuple(label for pid, label in table if _has_pattern(child, pid))
                    name = child.CurrentName or ""
                    if name or patterns:
                        found.append((name, child.CurrentClassName or "", box, patterns, child))
            except Exception:
                pass
            if depth < 6:
                walk(child, depth + 1)
            child = _sibling_of(walker, child)

    walk(window, 0)
    found.sort(key=lambda row: (row[2][1], row[2][0]))
    return [
        (Target(n, name, cls, box, patterns), element)
        for n, (name, cls, box, patterns, element) in enumerate(found[:limit], 1)
    ]


def _focused() -> dict | None:
    """The focused element — program-side truth that a key or click landed.

    UIA answers in the calling process's DPI space, so this reads physical
    coordinates only after `_ensure_dpi` (see window_rect).
    """
    _ensure_dpi()
    try:
        automation, _uia_client = _uia()
        element = automation.GetFocusedElement()
        rect = element.CurrentBoundingRectangle
        return {
            "name": element.CurrentName or "",
            "cls": element.CurrentClassName or "",
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
        }
    except Exception:
        return None


def _value_of(element, uia_client) -> str:
    try:
        pattern = element.GetCurrentPattern(uia_client.UIA_ValuePatternId)
        if pattern:
            return pattern.QueryInterface(uia_client.IUIAutomationValuePattern).CurrentValue or ""
    except Exception:
        pass
    return ""


def _read_back(hwnd: int, target: Target | None) -> tuple[str, str]:
    """(value, which element it came from) — the read-back half of 'type'."""
    pairs = _scan(hwnd, limit=MAX_CANDIDATES)
    _automation, uia_client = _uia()
    if target is not None:
        element = _match_element(pairs, target)
        if element is not None:
            return _value_of(element, uia_client), target.name or target.cls
    focused = _focused()
    if focused is not None:
        for cand, element in pairs:
            if cand.rect == focused["rect"]:
                value = _value_of(element, uia_client)
                if value:
                    return value, cand.name or cand.cls
    for cand, element in pairs:
        if "Value" in cand.patterns:
            return _value_of(element, uia_client), cand.name or cand.cls
    return "", ""


def _read_back_settled(hwnd: int, target: Target | None) -> tuple[str, str]:
    """`_read_back` with one retry: a single empty read also happens when the app
    is mid-repaint, and the read is what decides whether the text is verified."""
    value, where = _read_back(hwnd, target)
    if value:
        return value, where
    time.sleep(0.25)
    return _read_back(hwnd, target)


def _match_element(pairs: list[tuple[Target, Any]], target: Target):
    """Re-find a candidate after the window may have moved: same name+class,
    nearest centre. Candidates are metadata, elements are per-call objects."""
    best = None
    best_distance = None
    for cand, element in pairs:
        if (not cand.name and not cand.cls) or cand.name != target.name or cand.cls != target.cls:
            continue  # nothing to identify it by
        cx, cy = cand.center
        tx, ty = target.center
        distance = abs(cx - tx) + abs(cy - ty)
        if best_distance is None or distance < best_distance:
            best, best_distance = element, distance
    return best


def _ocr_targets(frame: Frame, start: int = 0) -> list[Target]:
    """Optional fallback for windows with no usable a11y (pure canvas UI).

    rapidocr is a heavy extra (`pip install rapidocr-onnxruntime`), so its
    absence is reported rather than papered over. Boxes come back in frame
    pixels and are converted to screen coordinates with the frame origin — the
    prototype's two-point calibration problem does not exist here because the
    capture and the click share one coordinate space (see _ensure_dpi).
    """
    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        return []
    try:
        import numpy as np  # noqa: PLC0415
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
    except ImportError:
        return []
    engine = getattr(_thread_local, "ocr", None)
    if engine is None:
        engine = RapidOCR()
        _thread_local.ocr = engine
    result, _ = engine(np.array(frame.image.convert("RGB"))[:, :, ::-1])
    ox, oy = frame.origin
    out: list[Target] = []
    for item in result or []:
        points = np.array(item[0], dtype=float)
        left, top = int(points[:, 0].min()), int(points[:, 1].min())
        box = (
            ox + left,
            oy + top,
            ox + int(points[:, 0].max()),
            oy + int(points[:, 1].max()),
        )
        out.append(Target(start + len(out) + 1, str(item[1]), "", box, (), "ocr"))
    return out

# ── input injection ────────────────────────────────────────────────────────
class _MOUSEINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("pad", ctypes.c_byte * 32),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_: ClassVar[list[tuple[str, Any]]] = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
KEYEVENTF_UNICODE = 0x0004  # wScan carries a UTF-16 unit: the character, not a keycode
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}
# Every move this module injects is absolute over the whole virtual desktop; a press or a
# release is the same event plus the button's flag, so the pointer stays where it is.
_MOVE_ABS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

# Keyboard vocabulary: the Ophio key_map's names and aliases, with 'delete'
# pointing at the real Del and the desktop-only keys (home/end/insert/pageup/
# pagedown/printscreen) added. pyautogui is not a dependency here, so the table
# carries VK codes and injection goes through SendInput.
_KEY_VK: dict[str, int] = {
    "escape": 0x1B,
    "esc": 0x1B,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "space": 0x20,
    " ": 0x20,
    "capslock": 0x14,
    "up": 0x26,
    "arrowup": 0x26,
    "down": 0x28,
    "arrowdown": 0x28,
    "left": 0x25,
    "arrowleft": 0x25,
    "right": 0x27,
    "arrowright": 0x27,
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "option": 0x12,
    "win": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
    "command": 0x5B,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "insert": 0x2D,
    "ins": 0x2D,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "numpad_multiply": 0x6A,
    "numpad_add": 0x6B,
    "numpad_subtract": 0x6D,
    "numpad_decimal": 0x6E,
    "numpad_divide": 0x6F,
    **{chr(0x61 + i): 0x41 + i for i in range(26)},
    **{str(i): 0x30 + i for i in range(10)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
_EXTENDED_VK = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x6F}
)
_MODIFIER_VK = frozenset({0x10, 0x11, 0x12, 0x5B})
MODIFIER_NAMES = frozenset({"shift", "ctrl", "control", "alt", "win", "meta", "cmd", "command"})
_held: set[int] = set()
_held_buttons: set[str] = set()


def resolve_key(name: str) -> int | None:
    """Key name (aliases and either case) → virtual-key code, else None."""
    text = str(name).strip()
    return _KEY_VK.get(text) or _KEY_VK.get(text.lower())


def _send(payload: _INPUT) -> int:
    return int(_u32.SendInput(1, ctypes.byref(payload), ctypes.sizeof(_INPUT)))


def _mouse_event(x: int, y: int, flags: int) -> int:
    return _send(_INPUT(INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(x, y, 0, flags, 0, None))))


def _norm_point(x: int, y: int) -> tuple[int, int]:
    """Physical pixel → SendInput's 0..65535 space over the whole virtual desktop.

    Not just the primary monitor: the virtual desk flags make the same numbers mean
    the same pixels on every display.
    """
    _ensure_dpi()
    ox, oy = _virtual_origin()
    width, height = _virtual_size()
    return (
        int((x - ox) * 65535 / max(1, width - 1)),
        int((y - oy) * 65535 / max(1, height - 1)),
    )


def cursor_pos() -> tuple[int, int] | None:
    """Where the pointer is, in physical pixels (None if the OS will not say)."""
    point = wt.POINT()
    return (point.x, point.y) if _u32.GetCursorPos(ctypes.byref(point)) else None


def _smoothstep(t: float) -> float:
    """0→1 with no jerk at either end: what makes a glide read as a hand and not a jerk."""
    return t * t * (3.0 - 2.0 * t)


def move_to(x: int, y: int, *, steps: int = MOVE_STEPS, duration: float = MOVE_DURATION_S) -> int:
    """Glide the pointer to physical (x, y); return the number of move events injected.

    A single absolute `MOUSEEVENTF_MOVE` teleports: the application sees one jump from
    wherever the pointer was straight onto the target. That is wrong for anything that
    tracks the pointer rather than just reading its position — hover states and tooltips
    never fire, a canvas or drag-style UI gets no intermediate coordinates, and a
    mis-aimed jump cannot be seen coming. So the travel is a short eased path (`MOVE_STEPS`
    points over `MOVE_DURATION_S`, smoothstep) whose last point is exactly the target.

    The pointer moves through the same `SendInput` channel as the clicks, never
    `SetCursorPos`: one injection path for everything this tool does, so an application
    that watches for injected input sees one continuous gesture.

    pyautogui would do this in one call (`moveTo(duration=, tween=)`) and is installed on
    dev boxes, but it is not a dependency here, and taking it would cost more than the
    ~15 lines here — measured 2026-09-14: its Windows move is `SetCursorPos` and its
    buttons are the superseded `mouse_event` (two more injection channels), its
    `num_steps` is capped at one point per `MINIMUM_SLEEP`=50ms (4 points where this
    makes 14), every public call adds `PAUSE`=0.1s (0.378s vs 0.225s), and its FAILSAFE
    raises when the pointer sits in a screen corner. User decision the same day: ours
    (spec §36).
    """
    move = _MOVE_ABS
    nx, ny = _norm_point(x, y)
    here = cursor_pos()
    if here is None or here == (x, y):
        # Nothing to travel: keep the position pinned with the one event a click needs.
        _mouse_event(nx, ny, move)
        return 1
    hx, hy = _norm_point(*here)
    sleep = duration / max(1, steps)
    for step in range(1, steps + 1):
        eased = _smoothstep(step / steps)
        # the last step is eased == 1.0, so it lands on (nx, ny) exactly — no extra event
        _mouse_event(int(hx + (nx - hx) * eased), int(hy + (ny - hy) * eased), move)
        if step < steps:
            time.sleep(sleep)
    return steps


def _button_event(button: str, *, down: bool, at: tuple[int, int] | None = None) -> int:
    """One press or release, remembered while it is down.

    A press is the only thing this tool injects that has to survive *between* two points
    in time (a drag), and a left button left down makes the machine unusable until a human
    clicks. So a release the OS did not take (SendInput returns the number of events it
    inserted, and 0 means it refused) stays in the book — the action's `finally`, or a
    disarm, then tries again rather than assuming it worked.
    """
    flags = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])[0 if down else 1]
    point = _norm_point(*(at if at is not None else (cursor_pos() or (0, 0))))
    result = _mouse_event(*point, _MOVE_ABS | flags)
    if down:
        _held_buttons.add(button)
    elif result:
        _held_buttons.discard(button)
    return result


def _button_is_down(button: str) -> bool:
    """Whether the OS still has that button down — the judge is the system, not our book."""
    return bool(_u32.GetAsyncKeyState(BUTTON_VK.get(button, 0x01)) & 0x8000)


def lift_button(button: str = "left") -> int:
    """Release one button and make sure it really is released.

    The system is asked afterwards (`GetAsyncKeyState`), and if it still reports the button
    down the release is sent once more from wherever the pointer is: a held left button
    drags whatever the pointer crosses for the rest of the session, and only a human can
    clear it (§35.2's reason for `release_all_keys`, applied to the mouse).
    """
    released = _button_event(button, down=False)
    if _button_is_down(button):
        released += _button_event(button, down=False)
    return released


def release_all_buttons() -> list[str]:
    """Lift every button this session may still be holding (the safety net)."""
    lifted = [name for name in sorted(_held_buttons) if lift_button(name)]
    _held_buttons.clear()
    return lifted


def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> bool:
    """Absolute click(s) at physical screen coordinates, after gliding there.

    Normalized against the virtual desktop — SendInput's 0..65535 space covers
    every monitor, not just the primary one. The pointer travels (`move_to`) rather
    than teleports, so the application sees it arrive.

    `clicks=2` is the double-click: both press/release pairs land inside the
    system's double-click time, with no pointer movement between them, which is
    what makes the shell (and an app's own hit-testing) read it as one gesture
    rather than two clicks.
    """
    moves = move_to(x, y)
    # Did it actually get there? A coordinate outside the virtual screen gets clamped by
    # the OS (measured 2026-09-14: x=-185 became 0), and then a press would land on
    # whatever is at the edge — a wrong click nobody asked for. No arrival, no press.
    landed = cursor_pos()
    if landed is None or max(abs(landed[0] - x), abs(landed[1] - y)) > MOVE_TOLERANCE_PX:
        return False
    injected = moves
    for index in range(clicks):
        if index:
            time.sleep(DOUBLE_CLICK_GAP_S)
        injected += _button_event(button, down=True, at=(x, y))
        injected += _button_event(button, down=False, at=(x, y))
    # the travel plus a press/release pair per click; a struct-size mistake makes
    # SendInput return 0 silently, which is what this number is here to catch
    return injected == moves + 2 * clicks


def _drag_path(start: tuple[int, int], end: tuple[int, int], steps: int) -> list[tuple[int, int]]:
    """The carried path: eased like a click's glide, a point every ~12px of travel, and
    the last point exactly the drop point (eased == 1.0 there, so no extra event)."""
    (x0, y0), (x1, y1) = start, end
    return [
        (
            int(x0 + (x1 - x0) * _smoothstep(index / steps)),
            int(y0 + (y1 - y0) * _smoothstep(index / steps)),
        )
        for index in range(1, steps + 1)
    ]


def _drag_steps(start: tuple[int, int], end: tuple[int, int]) -> int:
    """How many points one leg of a drag travels in: about one every `DRAG_PX_PER_STEP`, and
    never fewer than `DRAG_MIN_STEPS` — a short carry is still a path, not a jump."""
    travel = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    return max(DRAG_MIN_STEPS, min(DRAG_MAX_STEPS, travel // DRAG_PX_PER_STEP))


def _cancel_gesture() -> None:
    """Escape, the way a person abandons a drag.

    A shell drag-and-drop loop reads Escape as "give the item back": the release that
    follows drops nothing. An application that draws its own drag has no such contract,
    so the result says the drop did not happen — never that the app rolled back.
    """
    _key_event(0x1B, down=True)
    _key_event(0x1B, down=False)


def drag_to(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    button: str = "left",
    hold: float = DRAG_HOLD_S,
    step_sleep: float = DRAG_STEP_SLEEP_S,
    dwell: float = DRAG_DWELL_S,
    via: tuple[int, int] | None = None,
    wait: Callable[[], bool] | None = None,
    wait_s: float | None = None,
    refresh: Callable[[], tuple[int, int] | str | None] | None = None,
    should_abort: Callable[[], bool] | None = None,
    pre_release: Callable[[], str | None] | None = None,
) -> dict:
    """Carry the pointer from `start` to `end` with the button held down.

    A drag is the one gesture whose *middle* is part of the effect: the application reads
    the press, then a path, then the release — a drop target decides during the hover, and
    anything tracking the pointer (a rubber band, a slider, a canvas) never sees a jump. So
    the pointer walks a real eased path (`DRAG_MIN_STEPS`..`DRAG_MAX_STEPS` points, one per
    `DRAG_PX_PER_STEP`), the press is held for `hold` before the first move, and the drop
    point is hovered for `dwell` before the release.

    `via` is a waypoint on the way: the pointer is carried there and *held* while `wait()`
    is polled (up to `wait_s`). That is the user's own route out of a covered window
    (2026-09-17): carry the thing onto the destination's taskbar button and hold — the
    shell brings that window to the front by itself — then continue into it. `refresh` is
    asked once the wait succeeded, because a window that came forward may have been restored
    or moved: it returns the drop point to use (or a reason to cancel instead).

    Three things end a drag early, and all three cancel it the way a person does — Escape,
    then the release, in a `finally`, because a held button is not something to leave
    behind:

    * the turn was aborted — `should_abort` is asked before every point, like `type_text`'s
      per-character check, because a drag is a second of work and a stop press must end it;
    * the pointer never arrived: `_norm_point` clamps a coordinate outside the virtual
      screen (measured 2026-09-14: x=-185 arrived at 0), so the release would drop onto
      whatever sits at the edge — no arrival, no press; and if it stops arriving on the way
      (or the waypoint's wait runs out), the drop is cancelled rather than released elsewhere;
    * `pre_release` returned a reason: the caller reads the world again at the last moment
      (which window owns the drop point now) and refuses a drop that would land elsewhere.

    Returns the facts: `started`, how many `points` went out of `steps`, whether the pointer
    `arrived`, how far `off` it was, how long the waypoint `waited`, the injection count
    against what was `expected`, whether the button was `released` (checked against the
    system), and `stopped` — why, if it did.
    """
    facts: dict = {
        "started": False,
        "steps": 0,
        "points": 0,
        "seconds": 0.0,
        "waited": 0.0,
        "arrived": False,
        "off": None,
        "injected": 0,
        "expected": 0,
        "released": False,
        "stopped": None,
    }
    injected = move_to(*start)
    planned = injected
    here = cursor_pos()
    if here is None or max(abs(here[0] - start[0]), abs(here[1] - start[1])) > MOVE_TOLERANCE_PX:
        facts["stopped"] = (
            f"the pointer never reached the grab point {start} (it is at {here}) — "
            "nothing was pressed"
        )
        return facts
    if not _button_event(button, down=True, at=start):
        injected += lift_button(button)
        facts["injected"] = injected
        facts["stopped"] = "the press was not injected (SendInput returned 0) — nothing moved"
        return facts
    facts["started"] = True
    injected += 1
    planned += 2  # the press and the release
    began = time.monotonic()
    try:
        if hold > 0:
            time.sleep(hold)

        def walk(target: tuple[int, int]) -> str | None:
            """One leg of the carried path; returns why it stopped, or None."""
            nonlocal injected, planned
            here = cursor_pos() or start
            steps = _drag_steps(here, target)
            facts["steps"] += steps
            planned += steps
            for index, point in enumerate(_drag_path(here, target, steps), 1):
                if should_abort is not None and should_abort():
                    return f"stopped after {facts['points']} points: the turn was aborted"
                injected += _mouse_event(*_norm_point(*point), _MOVE_ABS)
                facts["points"] += 1
                if step_sleep > 0 and index < steps:
                    time.sleep(step_sleep)
            return None

        def hold_at(waypoint: tuple[int, int]) -> str | None:
            """Hover the waypoint until the caller says the drop point is within reach.

            The timeout is read here, not taken as a default argument: a default is bound at
            import time, and then the knob cannot be tuned (or patched in a test) at all."""
            limit = DRAG_VIA_WAIT_S if wait_s is None else wait_s
            began_wait = time.monotonic()
            deadline = began_wait + max(0.0, limit)
            try:
                while not (wait() if wait is not None else True):
                    if should_abort is not None and should_abort():
                        return "stopped while waiting at the waypoint: the turn was aborted"
                    if time.monotonic() >= deadline:
                        return (
                            f"the waypoint {waypoint} was hovered for {limit:g}s and the drop "
                            "point never came within reach — cancelled instead of released there"
                        )
                    time.sleep(DRAG_VIA_POLL_S)
            finally:
                facts["waited"] = round(time.monotonic() - began_wait, 2)
            return None

        stopped = walk(via if via is not None else end)
        if stopped is None and via is not None:
            stopped = hold_at(via)
        if stopped is None and via is not None:
            # The drop point is read once more: a window the shell brought forward may have
            # been restored or moved, and the release has to land on a point it really owns.
            fresh = refresh() if refresh is not None else None
            if isinstance(fresh, str):
                stopped = fresh
            elif fresh is not None:
                end = fresh
        if stopped is None and via is not None:
            stopped = walk(end)
        facts["stopped"] = stopped
        facts["seconds"] = max(0.0, time.monotonic() - began)
        if facts["stopped"] is None:
            if dwell > 0:
                time.sleep(dwell)
            landed = cursor_pos()
            facts["off"] = max(abs(landed[0] - end[0]), abs(landed[1] - end[1])) if landed else None
            facts["arrived"] = facts["off"] is not None and facts["off"] <= MOVE_TOLERANCE_PX
            if not facts["arrived"]:
                facts["stopped"] = (
                    f"the pointer is at {landed}, not at the drop point {end} — cancelled "
                    "instead of released somewhere else"
                )
            elif pre_release is not None:
                facts["stopped"] = pre_release()
        if facts["stopped"] is not None:
            _cancel_gesture()
    finally:
        injected += lift_button(button)
    facts["injected"] = injected
    facts["expected"] = planned
    facts["released"] = not _button_is_down(button)
    return facts


def _key_event(vk: int, *, down: bool) -> int:
    flags = 0 if down else KEYEVENTF_KEYUP
    if vk in _EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    result = _send(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, None))))
    if down:
        _held.add(vk)
    else:
        _held.discard(vk)
    return result


def _char_units(char: str) -> list[int]:
    """The UTF-16 code units this character is sent as: one, or two for an astral
    character (an emoji is a surrogate pair, and Windows takes one unit per event)."""
    code = ord(char)
    if code <= 0xFFFF:
        return [code]
    code -= 0x10000
    return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]


def _char_event(unit: int, *, down: bool) -> int:
    """One character as `KEYEVENTF_UNICODE`: the character itself, not a keycode.

    That is what makes CJK work without touching the input method — no layout, no IME
    state, nothing to translate. The `_held` bookkeeping is deliberately not involved:
    a character is not a key that can be left down.
    """
    flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
    return _send(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(0, unit, flags, 0, None))))


def type_text(
    text: str,
    *,
    delay: float = TYPE_CHAR_DELAY_S,
    should_abort: Callable[[], bool] | None = None,
) -> tuple[int, str | None]:
    """Type `text` one character at a time — the effect a human typing has.

    The user's own Typer tool is the reference (2026-09-14): pynput's
    `keyboard.type(char)` per character with an adjustable gap between them, no
    clipboard involved. Here each character is an explicit `KEYEVENTF_UNICODE`
    down/up pair, so the keyboard layout and the IME are bypassed, and the gap is
    `TYPE_CHAR_DELAY_S` unless the caller asks for another one.

    `\\n`/`\\r` go out as Enter and `\\t` as Tab, exactly as Typer maps them: a raw
    U+000A character is not what an application reads as "next line".

    `should_abort` is asked before every character. At a human pace a paragraph is a
    minute of typing, so a stop press has to end the run *during* it — one character
    of latency, not the whole text.

    Returns `(characters typed, error)`. A run that dies half way stops there and
    reports how far it got — the text that already landed is a fact the caller has to
    be told, not something to hide behind an exception.
    """
    typed = 0
    for index, char in enumerate(text):
        if should_abort is not None and should_abort():
            return typed, (
                f"stopped after {typed} of {len(text)} character(s): the turn was aborted"
            )
        if char in "\r\n":
            events = [_key_event(0x0D, down=True), _key_event(0x0D, down=False)]
        elif char == "\t":
            events = [_key_event(0x09, down=True), _key_event(0x09, down=False)]
        else:
            units = _char_units(char)
            events = [
                result
                for unit in units
                for result in (_char_event(unit, down=True), _char_event(unit, down=False))
            ]
        if any(result != 1 for result in events):
            return typed, (
                f"SendInput refused {char!r} (character {index + 1} of {len(text)}) — "
                f"{typed} character(s) had already been typed"
            )
        typed += 1
        if delay > 0 and index < len(text) - 1:
            time.sleep(delay)
    return typed, None


def send_keys(names: list[str]) -> tuple[list[str], str | None]:
    """Press a sequence; leading modifiers make it a combination.

    Every name is resolved *before* any injection: silently skipping an unknown
    key would turn "ctrl+s" into a bare Ctrl held down — the stuck-modifier
    fault this module's disarm exists to prevent.
    """
    codes: list[int] = []
    for name in names:
        vk = resolve_key(name)
        if vk is None:
            return [], f"unknown key name: {name!r} (see the key list in the tool description)"
        codes.append(vk)
    if not codes:
        return [], "no keys given"
    if len(codes) > 1 and all(code in _MODIFIER_VK for code in codes[:-1]):
        for code in codes[:-1]:
            _key_event(code, down=True)
        _key_event(codes[-1], down=True)
        _key_event(codes[-1], down=False)
        for code in reversed(codes[:-1]):
            _key_event(code, down=False)
    else:
        for code in codes:
            _key_event(code, down=True)
            _key_event(code, down=False)
    return [str(name) for name in names], None


def held_keys() -> list[str]:
    return sorted(f"0x{vk:02X}" for vk in _held)


def release_all_keys() -> list[str]:
    """Release every key this process may have left down. Called on every
    refusal path, on disarm, and at process exit: a host Ctrl stuck down is a
    fault the user cannot fix from inside a tool call."""
    stuck = [vk for vk in _held if vk in _MODIFIER_VK]
    for vk in stuck:
        with contextlib.suppress(Exception):
            _key_event(vk, down=False)
    _held.clear()
    return sorted(f"0x{vk:02X}" for vk in stuck)


# ── the session: candidates, and where the window was when they were cut ────
@dataclass
class Listing:
    """One window's numbered candidates, and where that window was when they were cut.

    Per window, not one slot for the whole session: a drag names a target in its source
    window *and* one in its destination window, and a single remembered listing throws the
    first window's numbers away the moment the second one is listed (2026-09-17).
    """

    candidates: dict[int, Target] = field(default_factory=dict)
    rect: tuple[int, int, int, int] | None = None


@dataclass
class Session:
    """What this run knows about the windows it has been asked about.

    A fresh process holds no pixels of its own: every action greps the frames it needs (one
    before the gesture, one after) and compares them there. Fungi kept the last two frames to
    answer "did that change anything" across tool calls; here nothing outlives the call, so
    nothing keeps them."""

    # No permission state lives here: running this script is the consent (the user's
    # decision of 2026-09-13, unchanged) and a script is the same kind of switch —
    # whoever runs it pointed an agent at the desktop. What stays is bookkeeping
    # that makes one action trustworthy: which window is where, which keys are down.
    listings: dict[int, Listing] = field(default_factory=dict)
    # What the model called a shape ("发送"), bound to the rectangle it saw it at
    # (spec §35.10). Numbers are per-listing; a rectangle survives a re-listing.
    labels: dict[str, Target] = field(default_factory=dict)
    labels_hwnd: int = 0
    labels_rect: tuple[int, int, int, int] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def disarm(self) -> None:
        self.labels.clear()
        self.labels_hwnd = 0
        self.labels_rect = None
        self.listings.clear()


_session = Session()


def disarm() -> None:
    """Releases keys and mouse buttons, and forgets candidates and listings. Bound to the
    process exit, so a crash cannot leave the host's keyboard half-pressed or its left button
    still dragging whatever the pointer crosses.

    Nothing is announced: a tray toast pops over the very screen being driven and
    steals focus from it (user decision 2026-09-13 — spec §35.14)."""
    release_all_keys()
    release_all_buttons()
    _session.disarm()


atexit.register(disarm)



# ── the listing on disk: numbers are per-listing, so the file says which one ─
def _listing_path(hwnd: int) -> Path:
    return DATA_DIR / f"{hwnd}.json"


def _labels_path(hwnd: int) -> Path:
    return DATA_DIR / f"{hwnd}.labels.json"


def _target_record(target: Target) -> dict:
    return {
        "n": target.n,
        "name": target.name,
        "cls": target.cls,
        "rect": list(target.rect),
        "patterns": list(target.patterns),
        "source": target.source,
    }


def _target_from_record(record: dict) -> Target:
    return Target(
        int(record.get("n") or 0),
        str(record.get("name") or ""),
        str(record.get("cls") or ""),
        tuple(record["rect"]),
        tuple(record.get("patterns") or ()),
        str(record.get("source") or "a11y"),
    )


def _write_json(path: Path, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_listing(hwnd: int, records: list[dict], frame_path: str, note: str) -> Path:
    """The numbers, written down with the rectangle they were cut from: a later call can only
    tell a fresh listing from a stale one by asking where the window was.

    Takes the candidate *records* rather than the targets: what is stored has to be what the
    program read (names and all), never the labels laid over them for display.
    """
    path = _listing_path(hwnd)
    _write_json(
        path,
        {
            "hwnd": hwnd,
            "title": _window_text(hwnd),
            "note": note,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rect": list(window_rect(hwnd) or ()),
            "state": window_state(hwnd),
            "frame": frame_path,
            "targets": records,
        },
    )
    return path


def _save_labels(hwnd: int) -> None:
    """Labels are written the moment they are given: for a picture-only listing they are the
    only way to address a shape twice, and the process that gave them is already gone."""
    if _session.labels_hwnd != hwnd:
        return
    _write_json(
        _labels_path(hwnd),
        {
            "hwnd": hwnd,
            "rect": list(_session.labels_rect or ()),
            "labels": {name: _target_record(cand) for name, cand in _session.labels.items()},
        },
    )


def _load_labels(hwnd: int) -> None:
    """The names given to shapes are their own file, and every listing and every `--name`
    call reads them back: for a picture-only listing they are the only way to address a
    shape twice, and the process that gave them is long gone."""
    labels = _read_json(_labels_path(hwnd))
    if not labels:
        return
    with _session.lock:
        _session.labels = {
            name: _target_from_record(item) for name, item in (labels.get("labels") or {}).items()
        }
        _session.labels_hwnd = hwnd
        _session.labels_rect = tuple(labels.get("rect") or ()) or None


def _load_listing(hwnd: int) -> str | None:
    """Read this window's last listing back into the session — or explain why it cannot be used.

    A listing whose rectangle is not where the window is now is refused outright: every
    number in it, and every rectangle a label is bound to, was read off a picture the window
    has since moved out of, so a click resolved from it would land somewhere else.
    """
    _load_labels(hwnd)
    record = _read_json(_listing_path(hwnd))
    if not record:
        return None
    stored = tuple(record.get("rect") or ()) or None
    if stored != window_rect(hwnd):
        return (
            f"ERROR: hwnd=0x{hwnd:X} is not where its listing left it ({record.get('at')}, "
            f"rect {tuple(record.get('rect') or ())}) — those numbers belong to the picture "
            f"they were cut from. Run `targets --hwnd {hwnd}` again."
        )
    with _session.lock:
        _session.listings[hwnd] = Listing(
            {cand.n: cand for cand in map(_target_from_record, record.get("targets") or [])},
            stored,
        )
    return None

# ── target resolution: the model names one, the program locates it ─────────
def resolve_target(hwnd: int, args: dict, *, allow_ocr: bool = True) -> Target | str:
    """Turn `target=<n>` or `name=<text>` into a rectangle in screen space.

    Ambiguity is surfaced, never guessed: several matches come back as a list
    for the model to pick from (the prototype's "don't silently take the first"
    rule). A name with no match is `no_target`, with the a11y candidates listed.
    """
    number = args.get("target")
    name = str(args.get("name") or "").strip()
    if number is None and not name:
        return (
            "ERROR: pass target=<number from targets> or name=<visible text>. "
            "Call the targets action first to see the numbers."
        )
    pairs = _scan(hwnd)
    if number is not None:
        try:
            wanted = int(number)
        except (TypeError, ValueError):
            return f"ERROR: target must be a number, got {number!r}"
        listing = _session.listings.get(hwnd)
        if listing is not None and wanted in listing.candidates:
            stored = listing.candidates[wanted]
            if stored.source != "a11y":
                # Its rectangle came out of the picture, not out of the tree, so
                # re-matching it against a11y elements picks a *different* control
                # (two empty-named elements matched each other on 2026-09-13 and the
                # click went to the wrong place). The picture's own frame origin
                # only holds while the window stays where it was.
                if listing.rect != window_rect(hwnd):
                    return (
                        "ERROR: the window moved or resized since that listing — run targets again "
                        f"(hwnd={hwnd}); those numbers belong to the picture they were cut from."
                    )
                return stored
            live = _match_element(pairs, stored)
            if live is not None:
                for cand, element in pairs:
                    if element is live:
                        return cand
            return (
                f"ERROR: target #{wanted} ({stored.name!r}) is not on screen any more — "
                "run targets again (the window changed since it was listed)."
            )
        return (
            f"ERROR: no candidate #{wanted} for this window. "
            "Call targets with this hwnd first; numbers do not survive a new listing."
        )
    lowered = name.casefold()

    # Match the same text the model reads in the listing: a control with no
    # accessible name is identified by its class ('Edit', 'ListBox'), and
    # matching only the empty name would make those unreachable by name.
    def key_of(cand: Target) -> str:
        return (cand.name or cand.cls).casefold()

    exact = [cand for cand, _ in pairs if key_of(cand) == lowered]
    if len(exact) == 1:
        return exact[0]  # a real control with that name outranks a label
    labelled = _find_label(hwnd, lowered)
    if isinstance(labelled, str):
        return labelled
    if isinstance(labelled, Target):
        return labelled
    loose = [cand for cand, _ in pairs if lowered in key_of(cand)]
    hits = exact or loose
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        listed = "\n".join(cand.label for cand in hits[:12])
        return f"ERROR: {len(hits)} controls match {name!r} — pick a number with target=\n{listed}"
    if allow_ocr:
        frame = grab_window(hwnd)
        if frame is not None:
            start = len(pairs)
            for cand in _ocr_targets(frame, start=start):
                if lowered in cand.name.casefold():
                    return cand
            if importlib.util.find_spec("rapidocr_onnxruntime") is None:
                return (
                    f"ERROR: no_target: nothing in this window is named {name!r}, and the OCR "
                    "fallback is not installed (pip install rapidocr-onnxruntime) — for a "
                    "canvas-drawn UI there is no a11y to fall back on."
                )
    listed = "\n".join(cand.label for cand, _ in pairs[:20])
    known = _session.labels if _session.labels_hwnd == hwnd else {}
    tail = f" Labels you set here: {', '.join(sorted(known))}." if known else ""
    return f"ERROR: no_target: nothing named {name!r} in this window.{tail} Candidates:\n{listed}"

# ── read-only actions ─────────────────────────────────────────────────────
def _windows_report(limit: int = 20, include_hidden: bool = False) -> str:
    windows = list_windows(include_hidden=include_hidden)
    if not windows:
        return "WINDOWS: none visible"
    front = foreground_hwnd()
    windowed = [win for win in windows if win.state != "untitled"]
    tray = [win for win in windows if win.cls == "Shell_TrayWnd"]
    if include_hidden:
        # On-screen windows first, then the ones hiding in the tray; biggest first
        # inside each group — the meaningful ones then survive the row cap.
        windows = sorted(
            windows,
            key=lambda win: (
                win.state != "normal",
                -((win.rect[2] - win.rect[0]) * (win.rect[3] - win.rect[1])),
            ),
        )
    lines = [
        f"WINDOWS ({len(windowed)} application windows"
        + (
            f", {len(windows)} rows incl. system/tray (helper windows under 40px skipped)"
            if include_hidden
            else ""
        )
        + "; hwnd is the identity, titles are not)"
    ]
    printed = windows[:limit]
    for win in printed:
        mark = " ← foreground" if win.hwnd == front else ""
        note = "" if win.state == "normal" else f" [{win.state}]"
        # An untitled row is a system surface: its class is the only identity the
        # model can reason about ('Shell_TrayWnd' == the taskbar).
        label = repr(win.title) if win.title else f"<no title, cls={win.cls}>"
        lines.append(
            f"  hwnd={win.hwnd} 0x{win.hwnd:X} {win.size} {win.proc or win.cls} {label}{note}{mark}"
        )
    # The hints name a row "above": only say that about a row that is actually in the
    # listing, because the row cap can cut the taskbar or the desktop off (both sort
    # last — the desktop is at the bottom of the z-order, the taskbar row is untitled).
    shown = {win.hwnd for win in printed}
    if tray and any(win.hwnd in shown for win in tray):
        lines.append(
            "  the taskbar/notification area is the Shell_TrayWnd row above: "
            "targets --hwnd <it> lists the tray icons, and a tray-only app usually also has its "
            "own [hidden] or [minimized] row here — targets --hwnd <that hwnd> restores it."
        )
    desktop = _desktop_surface()
    if desktop in shown:
        lines.append(
            f"  the desktop is the 0x{desktop:X} row above: targets --hwnd {desktop} lists the "
            "desktop icons (one row per icon), and double-click --hwnd <it> --name <icon text> "
            "opens that one — this is how an application with no window, no taskbar button and no "
            "tray icon is started. Icons are covered by whatever window is on top, so that click "
            "is refused (and told why) until the desktop itself is showing."
        )
    if not include_hidden:
        lines.append(
            '  (hidden/tray windows are left out; call windows again with --include all to see '
            "them plus the notification area)"
        )
    return "\n".join(lines)


def _action_windows(args: dict) -> str:
    include = str(args.get("include") or "visible").strip().lower()
    return _windows_report(limit=40 if include == "all" else 20, include_hidden=include == "all")


def _frame_for(hwnd: int | None) -> tuple[Frame | None, str]:
    if hwnd is None:
        return grab_screen(), "SCREEN"
    if not _u32.IsWindow(hwnd):
        return None, f"ERROR: no such window: 0x{hwnd:X}"
    frame = grab_window(hwnd)
    if frame is None:
        return None, f"ERROR: could not capture window 0x{hwnd:X}"
    return frame, "WINDOW"


def _action_shot(args: dict) -> str:
    hwnd = args.get("hwnd")
    hwnd = int(hwnd) if hwnd not in (None, "") else None
    if hwnd is not None and _u32.IsWindow(hwnd):
        was = window_state(hwnd)
        if was != "normal":
            return (
                f"ERROR: window 0x{hwnd:X} {_window_text(hwnd)!r} is {was} — that picture would "
                f"be its icon slot, not its UI. Use restore --hwnd {hwnd} first; a "
                "whole-screen shot (no hwnd) still shows everything that is on screen."
            )
        problem = capture_problem(hwnd)
        if problem:
            return f"ERROR: {problem}"
    frame, kind = _frame_for(hwnd)
    if frame is None:
        return kind
    title = ""
    if hwnd is not None:
        title = f" {_window_text(hwnd)!r}"
    summary = (
        f"{kind} {frame.image.width}x{frame.image.height} at {frame.origin}{title} "
        f"({'physical pixels; this tool sets DPI awareness' if hwnd is None else f'hwnd=0x{hwnd:X}'})"
    )
    if hwnd is None:
        summary += "\n" + _windows_report(limit=12)
        summary += (
            "\nFor small text, shot a single window: hwnd=<the one you care about>, or act "
            "directly with targets --hwnd ...."
        )
    return _attach(summary, frame, f"shot-0x{hwnd:X}" if hwnd is not None else "shot")


def _action_targets(args: dict) -> str:
    hwnd = args.get("hwnd")
    if hwnd in (None, ""):
        return "ERROR: targets needs hwnd=<window id from windows>"
    hwnd = int(hwnd)
    if not _u32.IsWindow(hwnd):
        return f"ERROR: no such window: 0x{hwnd:X}"
    was = window_state(hwnd)
    if was != "normal":
        return (
            f"ERROR: window 0x{hwnd:X} {_window_text(hwnd)!r} is {was} — its controls keep "
            "off-screen rectangles, so a listing here would be fiction. Use restore "
            f"(hwnd={hwnd}) to bring it back on screen first: that is also how an app sitting in "
            "the notification area comes back."
        )
    problem = capture_problem(hwnd)
    if problem:
        return f"ERROR: {problem}"
    pairs = _scan(hwnd)
    frame = grab_window(hwnd)
    if frame is None:
        return (
            f"ERROR: could not capture window 0x{hwnd:X} — its renderer keeps the pixels to "
            "itself while it sits behind another window. Bring it to the front with "
            f"restore --hwnd {hwnd} and read it again."
        )
    # Drop what cannot be addressed: no name and no class means the model has
    # nothing to say about it (Chromium/Electron's anonymous shells), and a
    # pattern-less whole-client-area box is only a temptation to click blind.
    targets = [cand for cand, _ in pairs if cand.name or cand.cls]
    note = ""
    # A self-drawn UI (WeChat 4.x renders every control into one surface element)
    # lists one candidate with no pattern: nothing to click by rectangle. Then the
    # picture takes over in the order spec §35.1 measured — a11y, then OCR text,
    # then the shapes the *program* cuts out of the pixels for the model to pick by
    # number. The binarised image itself never reaches the model (21-296px of error
    # and 3x the latency, measured).
    # A window whose only clickable a11y elements are its own window buttons (the
    # title bar) draws its controls itself: that is when the picture takes over.
    # "Clickable" needs a *name or class* to be addressable at all: Chromium/Electron
    # expose seven identical unnamed classless ScrollItem shells over the whole
    # client area (QQ measured 2026-09-13), which counted as actionable and so
    # silently switched the OCR and shape tiers off — the model got seven targets
    # it could neither name nor distinguish and no text at all.
    # The judgment has a name now (`self_drawn_reason`; user rule 2026-09-14: a self-drawn
    # UI is judged UP FRONT and also gets the desktop-control route) — and the listing says
    # which way it went, so the model need not infer it from the absence of names.
    drawn = self_drawn_reason(hwnd, pairs)
    if drawn:
        found_a11y = targets
        ocr = _ocr_targets(frame, start=len(found_a11y))
        shapes = _visual_targets(frame, start=len(found_a11y) + len(ocr))
        # A shape sitting inside a text box is that box's own ink: keep the text
        # (addressable by name), drop the duplicate blob.
        shapes = [
            shape for shape in shapes if not any(_overlaps(shape.rect, text.rect) for text in ocr)
        ]
        # The window's own chrome is already a11y's job: what the tiers add is the
        # drawing area. Keeping the title bar here only made noise.
        ocr = [cand for cand in ocr if inside_client(hwnd, cand)]
        shapes = [cand for cand in shapes if inside_client(hwnd, cand)]
        targets = _renumber([*found_a11y, *ocr, *shapes])
        note = _candidate_note(found_a11y, ocr, shapes, lead=f"self-drawn: {drawn}")
    # The records are taken *before* the labels are laid over the names: a label is a display
    # name for the model, while a number has to stay re-matchable against the window's live
    # a11y tree — and a stored name of "ping" matches nothing there (measured 2026-09-17: the
    # click that used a number from a labelled listing came back "target #8 ('ping') is not on
    # screen any more").
    records = [_target_record(cand) for cand in targets]
    if _session.labels_hwnd == hwnd and _session.labels_rect == window_rect(hwnd):
        for bound_name, bound in _session.labels.items():
            for cand in targets:
                if cand.rect == bound.rect:
                    cand.name = bound_name  # the semantic name the model gave it
    with _session.lock:
        _session.listings[hwnd] = Listing(
            {record["n"]: _target_from_record(record) for record in records}, window_rect(hwnd)
        )

    title = _window_text(hwnd)
    head = f"TARGETS in hwnd=0x{hwnd:X} {title!r}{note} — pick one by number"
    listing = "\n".join(cand.label for cand in targets) or "  (none)"
    frame_path = ""
    if targets:
        path = _data_file(f"targets-0x{hwnd:X}", ".png")
        _mark_targets(frame, targets).save(path, "PNG")
        frame_path = str(path)
    listing_path = _write_listing(hwnd, records, frame_path, note)
    summary = f"{head}\n{listing}\n[listing: {listing_path}]"
    if frame_path:
        summary += f"\n[numbered frame: {frame_path}]"
    return summary

def covering_window(point: tuple[int, int]) -> int:
    """The top-level window a click at this point would actually reach; 0 if none.

    The tool has two decisions that both rest on this one question — "may I click this
    rectangle" and "is that desktop icon still reachable" — and a rectangle existing in
    the a11y tree answers neither: measured 2026-09-14, the desktop's 微信 icon kept its
    coordinates while a terminal covered it, so a click there would have gone to the
    terminal.
    """
    at = int(_u32.WindowFromPoint(wt.POINT(point[0], point[1])) or 0)
    return int(_u32.GetAncestor(at, GA_ROOT) or 0) if at else 0


def target_problem(hwnd: int, target: Target) -> str | None:
    """Why this target must not be clicked — all three cases are measured ones.

    * Outside the window the caller asked to act on: the click lands on whatever
      is underneath it. A mis-resolved target put a real click on a desktop file
      on 2026-09-13.
    * Something else covers the point: the click would go to *that* window. Measured
      2026-09-14: with a terminal in front, the desktop's own 微信 icon still resolves
      (its rectangle is right there in the a11y tree) while `WindowFromPoint` at its
      centre returns the terminal — so "the rectangle exists" is not permission to
      click. The caller raises the window first, so for a normal window this only
      refuses targets that are genuinely covered.
    * A rectangle that is the whole window surface with no pattern at all: a
      self-drawn UI exposes exactly one such element (WeChat 4.x's
      MMUIRenderSubWindowHW), and its centre is not a control — it is a guess.
    """
    window = window_rect(hwnd)
    if window is None:
        return f"window 0x{hwnd:X} has no rectangle any more"
    left, top, right, bottom = window
    centre_x, centre_y = target.center
    if not (left <= centre_x <= right and top <= centre_y <= bottom):
        return (
            f"{target.label} sits outside window 0x{hwnd:X} "
            f"({left},{top},{right},{bottom}) — clicking there would hit whatever is underneath"
        )
    at = covering_window((centre_x, centre_y))
    if not at:
        return (
            f"{target.label} is at ({centre_x},{centre_y}), where no window answers at all — "
            "there is nothing to click there"
        )
    if at != hwnd:
        cover = _window_text(at) or _class_name(at) or f"0x{at:X}"
        return (
            f"{target.label} is covered: ({centre_x},{centre_y}) belongs to {cover!r} "
            f"(0x{at:X}), so the click would go there instead of to 0x{hwnd:X}. Move that "
            "window out of the way first (or click what you actually want on it)"
        )
    area = (target.rect[2] - target.rect[0]) * (target.rect[3] - target.rect[1])
    window_area = (right - left) * (bottom - top)
    if not target.patterns and window_area and area >= 0.75 * window_area:
        return (
            f"{target.label} is the whole window surface, not a control: this UI draws itself, so "
            "there is no rectangle to trust. Name the thing you want by its visible text instead "
            "(name=<text>; OCR reads the picture), or drive it with keys"
        )
    return None


@dataclass
class DragEnds:
    """Both ends of one drag, as the program read them (spec §46).

    `source` is the candidate that was grabbed — `None` when the grab is the window's own
    title bar — and `target` is the candidate a drop was aimed at, if any: the two are what
    the a11y read-back verification has to work with.
    """

    grab: tuple[int, int]
    grab_label: str
    source: Target | None
    drop: tuple[int, int]
    drop_label: str
    target: Target | None


def titlebar_point(hwnd: int) -> tuple[int, int] | str:
    """Where this window's own title bar is — the grab point for a reposition.

    The OS says how tall a caption is (`SM_CYCAPTION`) and the window rect says where the
    window is, so this is still a rectangle the program read rather than a coordinate the
    model guessed (spec §35.1). The middle of the caption is the grab point; an application
    that paints its own chrome across the whole caption band (a browser's tab strip) is why
    the result prints the grab point it used — a candidate is the better handle there.
    """
    rect = window_rect(hwnd)
    if rect is None:
        return f"ERROR: window 0x{hwnd:X} has no rectangle to read a title bar from"
    left, top, right, _bottom = rect
    caption = int(_u32.GetSystemMetrics(SM_CYCAPTION)) or 30
    return ((left + right) // 2, top + max(6, caption // 2))


def point_problem(hwnd: int, point: tuple[int, int]) -> str | None:
    """Why nothing must happen at this point — the two measured ways a point goes astray.

    Asked of both ends of a drag (a carried-the-window-by-its-title-bar grab, and the drop
    point) and of nothing else: a *candidate* grab goes through `target_problem`, which
    also knows the rectangle rules a click needs.

    * Outside the window the caller named: what happens there is whatever sits there
      instead (the same accident a mis-resolved click is, 2026-09-13).
    * Something else covers the point: during a drag the window *under the pointer* is what
      receives the drop, so this is asked twice — before the press (nothing is injected if
      it fails) and again just before the release, because a drag that passes over the
      taskbar can hand the pointer to another window on hover.
    """
    rect = window_rect(hwnd)
    if rect is None:
        return f"window 0x{hwnd:X} has no rectangle any more"
    left, top, right, bottom = rect
    if not (left <= point[0] <= right and top <= point[1] <= bottom):
        return (
            f"the point {point} is outside window 0x{hwnd:X} ({left},{top},{right},{bottom})"
            " — the press or the drop would land on whatever is there"
        )
    at = covering_window(point)
    if not at:
        return f"there is no window at {point} to press or drop on"
    if at != hwnd:
        cover = _window_text(at) or _class_name(at) or f"0x{at:X}"
        return (
            f"the point {point} belongs to {cover!r} (0x{at:X}), not to 0x{hwnd:X} — the press "
            "or the drop would go there instead. Put that window in front, or aim the drag "
            "somewhere that is not covered"
        )
    return None


def resolve_drop(
    hwnd: int, args: dict, grab: tuple[int, int], *, carry: bool
) -> tuple[tuple[int, int], str, Target | None] | str:
    """Where the carried thing is let go: an anchor the program reads, shifted by the
    caller's relative `dx`/`dy`.

    Three anchors, because a drag has three shapes (all of them read by the program):

    * `to_target`/`to_name` — a control in the destination window (a text box, a drop zone);
    * `to_hwnd` with no target — that window itself, which is the file-into-an-application
      case: the drop point is its client-area centre, and the window takes it from there;
    * neither — a carry inside the same window, so the drop point is the *grab point*
      shifted by `dx`/`dy` (a selection, a slider, a window moved by its title bar, where
      only the delta `drop - grab` means anything).

    A drop is not a click. What it lands on is a *window* — or a control inside it — and a
    drop target that covers the whole client area is the normal case (a chat window takes a
    file anywhere), so `target_problem`'s "that is the whole surface, not a control" refusal
    has no business here. The rule that does apply is `point_problem`: it has to land inside
    the window that was named, and that window has to be what is at that point.
    """
    try:
        dx = int(args.get("dx") or 0)
        dy = int(args.get("dy") or 0)
    except (TypeError, ValueError):
        return f"ERROR: dx and dy must be whole pixels, got {args.get('dx')!r}/{args.get('dy')!r}"
    wants = args.get("to_target") is not None or bool(args.get("to_name"))
    target: Target | None = None
    if wants:
        resolved = resolve_target(
            hwnd, {"target": args.get("to_target"), "name": args.get("to_name")}
        )
        if isinstance(resolved, str):
            return resolved
        target = resolved
        anchor, why = target.center, f"{target.label} in hwnd=0x{hwnd:X}"
    elif carry:
        anchor, why = grab, "a carry inside the same window"
    else:
        area = client_rect(hwnd) or window_rect(hwnd)
        if area is None:
            return f"ERROR: window 0x{hwnd:X} has no rectangle to drop into"
        anchor = ((area[0] + area[2]) // 2, (area[1] + area[3]) // 2)
        why = f"the client-area centre of hwnd=0x{hwnd:X}"
    if dx or dy:
        why += f" shifted by ({dx:+d},{dy:+d})"
    return ((anchor[0] + dx, anchor[1] + dy), why, target)


def _drag_ends(hwnd: int, to_hwnd: int, args: dict, grab_from: str) -> DragEnds | str:
    """Resolve both ends of a drag. No coverage checks here: those ask what covers each
    point, and the answer during the drag is the state *after* the source window has been
    raised, so the action runs them once that has happened."""
    source: Target | None = None
    if grab_from == "titlebar":
        point = titlebar_point(hwnd)
        if isinstance(point, str):
            return point
        grab, grab_label = point, "the title bar"
    else:
        found = resolve_target(hwnd, args)
        if isinstance(found, str):
            return found
        source, grab, grab_label = found, found.center, found.label
    drop = resolve_drop(to_hwnd, args, grab, carry=args.get("to_hwnd") in (None, ""))
    if isinstance(drop, str):
        return drop
    point, drop_label, target = drop
    if point == grab:
        return (
            f"ERROR: the grab point and the drop point are both {point} — a drag nowhere moves"
            " nothing. Pass dx/dy (or a to_target/to_name) to say where it is carried to."
        )
    return DragEnds(grab, grab_label, source, point, drop_label, target)


def _crop(frame: Frame, rect: tuple[int, int, int, int]) -> Frame | None:
    """The part of a whole-screen capture that a screen rectangle covers (None if none of
    it is in the frame) — a Frame of its own, so the two pictures a drag compares are the
    same kind of object the diff already understands.

    Screen crops rather than window captures: the two pictures are of a *place*, and the
    destination may sit behind the window the drag started from — whose window capture
    would either come back blank (a covered renderer keeps its pixels) or measure the wrong
    surface entirely.
    """
    left, top, right, bottom = frame.to_local(rect)
    left, top = max(0, left), max(0, top)
    right, bottom = min(frame.image.width, right), min(frame.image.height, bottom)
    if right - left < 2 or bottom - top < 2:
        return None
    origin = (frame.origin[0] + left, frame.origin[1] + top)
    return Frame(frame.image.crop((left, top, right, bottom)), origin, frame.hwnd, time.time())


LABEL_MAX = 24


def _find_label(hwnd: int, lowered: str) -> Target | str | None:
    """A shape the model named earlier, or why it cannot be used any more.

    Labels are bound to a rectangle rather than to a number: numbers are only
    valid inside one listing, while "the thing I called 发送" stays the thing at
    that rectangle for as long as the window does not move.
    """
    if not _session.labels or _session.labels_hwnd != hwnd:
        return None
    exact = [bound for name, bound in _session.labels.items() if name.casefold() == lowered]
    loose = [bound for name, bound in _session.labels.items() if lowered in name.casefold()]
    hits = exact or loose
    if not hits:
        return None
    if _session.labels_rect != window_rect(hwnd):
        return (
            "ERROR: the window moved or resized since you labelled that — the label's rectangle "
            f"belongs to the old picture. Run targets again (hwnd={hwnd}) and label it once more."
        )
    return hits[0]


def _renumber(targets: list[Target]) -> list[Target]:
    """Numbers are how the model refers to things: after dropping duplicates they
    must still run 1..N, or it will copy a number that is not in the listing."""
    for index, target in enumerate(targets, 1):
        target.n = index
    return targets


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int], iou: float = 0.3) -> bool:
    """IoU at or above `iou`, or one box inside the other — the prototype's merge
    rule (2026-09-13) for dropping a blob that is only the ink of a text box."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return False
    inter = (x1 - x0) * (y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(1, area_a + area_b - inter) >= iou or inter >= 0.9 * min(area_a, area_b)


def self_drawn_reason(hwnd: int, pairs: list[tuple[Target, Any]] | None = None) -> str | None:
    """Why this window draws itself, or None — decided *before* the picture is read.

    The user's rule (2026-09-14): a self-drawn UI is judged UP FRONT and also gets the
    desktop-control route. A self-drawn surface is not discovered by watching a11y fail —
    route changes with it, because for these windows the pixel tiers (OCR text, and the
    shapes the program cuts out) are not a fallback — they are the tool face.

    Two measured signals, either one is enough:

    * nothing **inside the client area** is both named/classed and has a pattern — the
      "screenshot with a title bar" shape: a11y offers the window buttons and nothing
      else (WeChat 4.x is one `MMUIRenderSubWindowHW` element over the whole client area);
    * the class is a self-drawn family (Chromium/Electron, Qt, MMUIRender — the same list
      `shell_reason` wakes through the shell) **and** nothing inside the client area is
      addressable, which is QQ's signature: seven anonymous ScrollItem shells.

    The class alone is deliberately not enough: Edge is Chromium and exposes named
    controls, and treating it as self-drawn would throw away the better source.
    """
    pairs = _scan(hwnd) if pairs is None else pairs
    addressable = [
        cand
        for cand, _elem in pairs
        if cand.patterns and (cand.name or cand.cls) and inside_client(hwnd, cand)
    ]
    if addressable:
        return None
    cls = _class_name(hwnd)
    empty = "nothing inside its client area is addressable"
    if cls.startswith(SELF_DRAWN_CLASSES):
        return f"{cls} is a self-drawn family and {empty} — the picture is the tool face here"
    return f"{empty} — the picture is the tool face here"


def _candidate_note(
    found_a11y: list[Target], ocr: list[Target], shapes: list[Target], lead: str = ""
) -> str:
    """Say where the candidates came from: a listing that hides its origin invites
    the model to treat a cut-out shape as if it were a named control. `lead` puts the
    reason first (a self-drawn surface says so before listing what the pixels gave)."""
    parts = ["a11y has nothing clickable here" if found_a11y else "no a11y at all"]
    if lead:
        parts.insert(0, lead)
    if ocr:
        parts.append(f"{len(ocr)} text boxes read off the picture by OCR (found by name)")
    elif importlib.util.find_spec("rapidocr_onnxruntime") is None:
        parts.append("no OCR installed (pip install rapidocr-onnxruntime)")
    if shapes:
        parts.append(
            f"{len(shapes)} shapes the program cut out of the picture — no text, so pick them by "
            "number"
        )
    return " (" + "; ".join(parts) + ")"


def _action_label(args: dict) -> str:
    """Remember what the model calls a shape it picked off the picture (spec §35.10).

    Choosing *which* shape is the send button is the model's job — it is the one
    looking at the picture; making that choice stick is the program's. Without this
    the semantics lived only in the conversation, and the next listing handed back
    "(no text)" again.
    """
    hwnd = args.get("hwnd")
    if hwnd in (None, ""):
        return "ERROR: label needs hwnd=<the window the listing came from>"
    hwnd = int(hwnd)
    if not _u32.IsWindow(hwnd):
        return f"ERROR: no such window: 0x{hwnd:X}"
    text = " ".join(str(args.get("label") or "").split())[:LABEL_MAX]
    if not text:
        return "ERROR: label needs label=<the name to remember, e.g. 发送>"
    if args.get("target") is None:
        return "ERROR: label needs target=<the candidate number to name>"
    resolved = resolve_target(hwnd, {"target": args["target"]}, allow_ocr=False)
    if isinstance(resolved, str):
        return resolved
    with _session.lock:
        _session.labels[text] = Target(
            0, text, resolved.cls, resolved.rect, resolved.patterns, resolved.source
        )
        _session.labels_hwnd = hwnd
        _session.labels_rect = window_rect(hwnd)
        known = ", ".join(sorted(_session.labels))

    _save_labels(hwnd)

    return (
        f"LABELLED {text!r} -> {resolved.label}\n"
        f"  click/type/scroll with name={text!r} find it while 0x{hwnd:X} stays where it is; "
        f"labels here: {known}"
    )



# ── input actions: armed, verified, and stopping at the strike limit ───────

def _strikes_path() -> Path:
    return DATA_DIR / "strikes.json"


def _strike(key: str) -> int:
    """One more fruitless attempt at `key`, counted where the next call can see it.

    It has to be on disk: a run performs exactly one action, so a counter in this process
    could never reach `FAILURE_LIMIT` and the whole escalation would be decoration
    (measured 2026-09-17). A run that gave up a while ago is not this run's evidence, so an
    entry older than `STRIKE_WINDOW_S` starts the count over.
    """
    with _session.lock:
        book = _read_json(_strikes_path())
        entry = book.get(key) if isinstance(book.get(key), dict) else {}
        count = int(entry.get("count") or 0) + 1
        if time.time() - float(entry.get("at") or 0) > STRIKE_WINDOW_S:
            count = 1
        book[key] = {"count": count, "at": round(time.time(), 3)}
        # Bounded: a key names a window and a target, and only the recent few mean anything.
        recent = dict(sorted(book.items(), key=lambda item: item[1].get("at", 0))[-40:])
        _write_json(_strikes_path(), recent)
        return count


def _clear_strikes(key: str) -> None:
    with _session.lock:
        book = _read_json(_strikes_path())
        if book.pop(key, None) is not None:
            _write_json(_strikes_path(), book)


def _escalate(sink, key: str, what: str, detail: str, should_abort, on_answer, call_id) -> str:
    """The strike limit as a script: nobody to ask, nobody to answer.

    Fungi asks its user here. A script cannot — the caller *is* the model — so the run ends
    with the same honesty the question carried: what was tried, what the machine looked like,
    and that it will not be tried again. The signature is the tool's own, so every action
    body reaches this the same way it always did.
    """
    _clear_strikes(key)
    return (
        f"ESCALATED: {FAILURE_LIMIT} attempts at {what} with no visible effect.\n"
        f"  at the last one: {detail}\n"
        "  no user to ask from a script, and it will not be retried: read the frame path this "
        "run printed, run `targets` again (the window may have moved), or `restore` it; if it "
        "still does nothing, report that instead of clicking again."
    )



def _guarded_input(hwnd: int) -> tuple[bool, str]:
    """The part of every input action that is not the action itself.

    Nothing here asks for permission: whoever ran this script pointed an agent at the
    desktop, and that *is* the consent (the user's decision of 2026-09-13 — the
    experimental switch means "I already allowed it" — carried over unchanged when
    the tool became a script). Nothing is announced either — a toast lands on top of
    the screen being driven (spec §35.14). What remains is what the script owes the
    machine: it wakes a window that is minimized or hiding in the notification area
    before anything tries to measure or click it.
    """
    # A minimized window's controls sit at their icon coordinates and a hidden one
    # has no on-screen geometry at all: wake it before anything measures or clicks,
    # through the path that actually wakes its application (see wake_window).
    was = window_state(hwnd)
    wake_window(hwnd)
    if window_state(hwnd) != "normal":
        return False, (
            f"ERROR: 0x{hwnd:X} is still {window_state(hwnd)} after a wake attempt — an "
            "elevated window (or one on another virtual desktop) cannot be driven from here."
        )
    return True, (f"  restored from {was}" if was != "normal" else "")


def _raise_for_input(hwnd: int) -> str | None:
    """Bring the window forward, and say why nothing may be typed if it cannot be.

    Keys and characters go to whatever holds the *focus*, not to the window the caller named.
    Injecting them while another window is in front types the caller's text into that window —
    measured 2026-09-17: a selftest run reported "type" as done while the probe's Edit still
    held its old text, and the characters had gone to whatever was in front (in that case the
    terminal the tool was being driven from). A click is safe without this: `target_problem`
    asks `WindowFromPoint` and refuses when the point belongs to another window. A keystroke
    has no point to check, so the check has to be the foreground.

    Shell surfaces are exempt: the desktop and the taskbar are never raised, by their own rule
    (`_is_shell_surface`), and `key --keys win d` is exactly how a person asks for the desktop.
    """
    if _is_shell_surface(hwnd):
        return None
    if set_foreground(hwnd) or foreground_hwnd() == hwnd:
        return None
    return (
        f"0x{hwnd:X} could not be brought to the foreground, and text and keys go to whatever "
        "has the focus — nothing was injected. Bring the window forward (restore) and try again"
    )


def _action_click(args: dict, sink, should_abort, on_answer, call_id) -> str:
    return _click_once(args, sink, should_abort, on_answer, call_id, clicks=1)


def _action_double_click(args: dict, sink, should_abort, on_answer, call_id) -> str:
    """Open something with the gesture a person would use.

    Back in the tool face on the user's call (2026-09-13): `shell_open` only covers
    what we already know the path of, and an icon drawn on the desktop, inside a
    list, or in an app that lives in neither the taskbar nor the tray can only be
    opened by being double-clicked. It is also the one "open" that starts a *fresh*
    process — the single wake path that never yields the "visible but asleep" window
    (spec §35.13).
    """
    return _click_once(args, sink, should_abort, on_answer, call_id, clicks=2)


def _click_once(args, sink, should_abort, on_answer, call_id, *, clicks: int) -> str:
    hwnd = int(args["hwnd"])
    gesture = "double_click" if clicks == 2 else "click"
    # Resolve before asking for permission: a name that does not exist should come
    # back as no_target, not as a card the user answers for nothing. A window that
    # is not on screen cannot be measured first, so that case waits for the wake-up.
    # Fail fast only when the pixels are trustworthy: an OCR fallback for a
    # window that is minimized, hidden or covered cannot read anything anyway.
    ready = window_state(hwnd) == "normal" and capture_problem(hwnd) is None
    if ready:
        first = resolve_target(hwnd, args)
        if isinstance(first, str):
            return first
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    # Raise it before measuring: a click has to land in the window the caller
    # named, and for an unaware process that is also what makes its pixels
    # readable at all.
    raised = True if _is_shell_surface(hwnd) else set_foreground(hwnd)
    target = resolve_target(hwnd, args) if not ready else first
    if isinstance(target, str):
        return target
    problem = target_problem(hwnd, target)
    if problem:
        return f"ERROR: {problem}"
    known_windows = {w.hwnd for w in list_windows()} if clicks == 2 else set()
    before = grab_window(hwnd)
    injected = click_at(*target.center, button=str(args.get("button") or "left"), clicks=clicks)
    time.sleep(SETTLE_S)
    # A gesture meant to open something: what it opens is a *new* window, and that
    # may be the only visible effect — the window the double-click landed in is
    # allowed to look unchanged (spec §35.13).
    launched: list = []
    if clicks == 2:
        deadline = time.monotonic() + LAUNCH_WAIT_S
        while True:
            launched = [w for w in list_windows() if w.hwnd not in known_windows]
            if launched or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    after = grab_window(hwnd)
    focused = _focused()
    changed = _diff_ratio(before, after) if before is not None and after is not None else 0.0
    focus_hit = bool(focused) and target.name and target.name in focused.get("name", "")
    verified = injected and (changed > DIFF_THRESHOLD or focus_hit or bool(launched))
    key = f"{gesture}:{hwnd}:{target.label}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = f"目标 {target.label}；注入={injected}；画面变化={changed:.3%}；前台={raised}"
        return _escalate(
            sink, key, f"{gesture} {target.label}", detail, should_abort, on_answer, call_id
        )
    if focus_hit:
        effect = "focus matched"
    elif launched:
        effect = f"opened {_window_text(launched[0].hwnd)!r}"
    else:
        effect = f"frame changed {changed:.2%}"
    lines = [
        f"{gesture.upper()} {target.label} at {target.center} in hwnd=0x{hwnd:X} "
        f"({'injected' if injected else 'INJECTION FAILED'}){note}",
        f"  verify: {effect} "
        f"→ {'verified' if verified else 'unverified (no visible effect)'}"
        f"{'' if raised else ' · could not raise the window to the foreground!'}",
        "  control: athand script — no per-action prompt (running it is the consent)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after) if after is not None else summary


def _action_type(args: dict, sink, should_abort, on_answer, call_id) -> str:
    hwnd = int(args["hwnd"])
    text = str(args.get("text") or "")
    if not text:
        return "ERROR: type needs text=<the string to enter>"
    wants = args.get("target") is not None or bool(args.get("name"))
    awake = window_state(hwnd) == "normal"
    target: Target | None = None
    if wants and awake:
        resolved = resolve_target(hwnd, args)
        if isinstance(resolved, str):
            return resolved
        target = resolved
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    blocked = _raise_for_input(hwnd)
    if blocked:
        return f"ERROR: {blocked}"
    if wants and not awake:  # raised after the arm: measure the controls only now
        resolved = resolve_target(hwnd, args)
        if isinstance(resolved, str):
            return resolved
        target = resolved
    if target is not None:
        _focus_target(hwnd, target)
    _before_value, where = _read_back_settled(hwnd, target)
    before_frame = grab_window(hwnd)
    style = str(args.get("style") or "type").strip().lower()
    if style != "type":
        # There is no paste route any more (user, 2026-09-14): text goes in one
        # character at a time, and a whole text dropped in through the clipboard is
        # not an option this tool offers. The clipboard is not ours to spend.
        return (
            f"ERROR: there is no {style!r} route — input is typed one character at a "
            "time; drop style= or use style='type'"
        )
    # 逐字输入 (spec §37): no clipboard at all — each character is injected on its
    # own, paced, the way the user's Typer tool does it.
    try:
        delay = float(args.get("char_delay") or TYPE_CHAR_DELAY_S)
    except (TypeError, ValueError):
        return f"ERROR: char_delay must be a number, got {args.get('char_delay')!r}"
    typed, typing_error = type_text(text, delay=max(0.0, delay), should_abort=should_abort)
    time.sleep(SETTLE_S)
    after_value, _where = _read_back_settled(hwnd, target)
    after_frame = grab_window(hwnd)
    changed = _diff_ratio(before_frame, after_frame) if before_frame and after_frame else 0.0
    key = f"type:{hwnd}:{target.label if target else where}"
    verified = text in after_value or after_value.strip() == text.strip()
    if verified or changed > DIFF_THRESHOLD:
        # A control that exposes no readable value is the self-drawn case (spec §40),
        # and there the picture moving IS the effect. Counting those runs as failures
        # asked the user after three typing calls that had all worked — measured
        # 2026-09-14 while typing into Notepad++, whose Scintilla editor has no value.
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = f"已落 {typed}/{len(text)} 字；回读到 {after_value!r}；画面变化={changed:.3%}"
        return _escalate(
            sink, key, f"type into {where or hex(hwnd)}", detail, should_abort, on_answer, call_id
        )
    if verified:
        verdict = f"verified: read back {after_value!r} from {where or 'the focused control'}"
    elif changed > DIFF_THRESHOLD:
        verdict = f"unverified: the picture changed ({changed:.2%}) but this control exposes no readable value"
    else:
        verdict = "unverified: nothing changed on screen"
    lines = [
        f"TYPE {len(text)} chars into hwnd=0x{hwnd:X}"
        + (f" target {target.label}" if target else " (current focus)")
        + note,
        f"  typed: {typed}/{len(text)} characters" + (f" · {typing_error}" if typing_error else ""),
        f"  verify: {verdict}",
        "  method: 逐字输入 (one character at a time; the clipboard is never touched)",
    ]
    lines.append("  control: athand script — no per-action prompt (running it is the consent)")
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary


def _action_key(args: dict, sink, should_abort, on_answer, call_id) -> str:
    hwnd = int(args["hwnd"])
    names = [str(k) for k in args.get("keys") or []]
    if not names:
        return 'ERROR: key needs keys=[...] (e.g. ["ctrl","s"] or ["enter"])'
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    blocked = _raise_for_input(hwnd)
    if blocked:
        return f"ERROR: {blocked}"
    before_focus = _focused()
    before_value, _where = _read_back(hwnd, None)
    before_frame = grab_window(hwnd)
    sent, error = send_keys(names)
    if error:
        return f"ERROR: {error}"
    time.sleep(SETTLE_S)
    after_focus = _focused()
    after_value, _where = _read_back(hwnd, None)
    after_frame = grab_window(hwnd)
    changed = _diff_ratio(before_frame, after_frame) if before_frame and after_frame else 0.0
    focus_moved = bool(before_focus) and bool(after_focus) and before_focus != after_focus
    value_changed = after_value != before_value
    verified = focus_moved or value_changed or changed > DIFF_THRESHOLD
    key = f"key:{hwnd}:{'+'.join(names)}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = f"焦点 {before_focus} → {after_focus}；画面变化={changed:.3%}"
        return _escalate(
            sink, key, f"key {'+'.join(names)}", detail, should_abort, on_answer, call_id
        )
    signal = (
        "focus moved"
        if focus_moved
        else ("control value changed" if value_changed else f"frame changed {changed:.2%}")
    )
    lines = [
        f"KEY {'+'.join(sent)} into hwnd=0x{hwnd:X}{note}",
        f"  verify: {signal} → {'verified' if verified else 'unverified (no visible effect)'}",
        f"  held keys after: {', '.join(held_keys()) or 'none'}",
        "  control: athand script — no per-action prompt (running it is the consent)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary


def _focus_target(hwnd: int, target: Target) -> bool:
    """a11y SetFocus: moves the input focus without touching the mouse or the
    z-order (the prototype's reason to prefer it over clicking a field)."""
    pairs = _scan(hwnd)
    element = _match_element(pairs, target)
    if element is None:
        return False
    try:
        element.SetFocus()
        return True
    except Exception:
        return False


def _action_scroll(args: dict, sink, should_abort, on_answer, call_id) -> str:
    hwnd = int(args["hwnd"])
    ready = window_state(hwnd) == "normal" and capture_problem(hwnd) is None
    if ready:
        first = resolve_target(hwnd, args)
        if isinstance(first, str):
            return first
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    set_foreground(hwnd)
    target = resolve_target(hwnd, args) if not ready else first
    if isinstance(target, str):
        return target
    pairs = _scan(hwnd)
    element = _match_element(pairs, target)
    if element is None:
        return f"ERROR: {target.label} is no longer in the tree — run targets again"
    before_frame = grab_window(hwnd)
    _automation, uia_client = _uia()
    scrolled = False
    try:
        pattern = element.GetCurrentPattern(uia_client.UIA_ScrollItemPatternId)
        if pattern:
            pattern.QueryInterface(uia_client.IUIAutomationScrollItemPattern).ScrollIntoView()
            scrolled = True
    except Exception:
        scrolled = False
    time.sleep(SETTLE_S)
    rect = window_rect(hwnd)
    after_frame = grab_window(hwnd)
    changed = _diff_ratio(before_frame, after_frame) if before_frame and after_frame else 0.0
    verified = scrolled and changed > DIFF_THRESHOLD
    key = f"scroll:{hwnd}:{target.label}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = (
            f"ScrollItemPattern={'ok' if scrolled else 'unavailable'}；画面变化={changed:.3%}；"
            f"窗口 rect={rect}"
        )
        return _escalate(
            sink, key, f"scroll to {target.label}", detail, should_abort, on_answer, call_id
        )
    lines = [
        f"SCROLL to {target.label} in hwnd=0x{hwnd:X}{note}",
        f"  verify: ScrollItemPattern={'used' if scrolled else 'not available'} · "
        f"frame changed {changed:.2%} → {'verified' if verified else 'unverified'}",
        "  control: athand script — no per-action prompt (running it is the consent)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary


def _action_drag(args: dict, sink, should_abort, on_answer, call_id) -> str:
    """Carry something with the left button held down (user, 2026-09-17).

    What it is for: content that is not text — a file, a picture — going into a text box,
    and the gestures a person makes on a window or a canvas (reposition by the title bar,
    drag a selection, carry a shape). A drop needs no focus, which is exactly why it reaches
    a UI that draws itself, where §35.13 found no addressable input control at all.

    Neither end is a coordinate the model produced: the grab end is a candidate in the
    source window or that window's own title bar, and the drop end is an anchor the program
    reads (a candidate in the destination window, or its client-area centre) shifted by the
    caller's `dx`/`dy`. Nothing is injected until both ends resolve and each point has been
    confirmed to belong to the window it was named in; that check runs again just before the
    release (`drag_to`), because a drop that lands somewhere else is worse than no drop.
    """
    hwnd = int(args["hwnd"])
    to_hwnd = args.get("to_hwnd")
    to_hwnd = int(to_hwnd) if to_hwnd not in (None, "") else hwnd
    if not _u32.IsWindow(to_hwnd):
        return f"ERROR: no such window to drop into: 0x{to_hwnd:X}"
    grab_from = str(args.get("from") or "").strip().lower()
    if grab_from not in ("", "titlebar"):
        return f"ERROR: from must be 'titlebar' (or left out), got {grab_from!r}"
    if grab_from == "titlebar" and _is_shell_surface(hwnd):
        return f"ERROR: 0x{hwnd:X} is the desktop or the taskbar — it has no title bar to carry"
    route_kind = str(args.get("route") or "").strip().lower()
    if route_kind not in ("", "taskbar"):
        return f"ERROR: route must be 'taskbar' (or left out), got {route_kind!r}"
    # The route out of a covered window (user, 2026-09-17): carry the thing onto the
    # destination's own taskbar button and hold, and the shell brings that window forward.
    # Resolved before anything is injected — no taskbar button means no route.
    route: Entry | None = None
    if route_kind == "taskbar":
        win = next((w for w in list_windows(include_hidden=True) if w.hwnd == to_hwnd), None)
        if win is None:
            return f"ERROR: 0x{to_hwnd:X} is not in the window list, so there is nothing to route through"
        found = via_entry(win)
        if isinstance(found, str):
            return f"ERROR: the drag cannot go through the taskbar: {found}"
        route = found
    # Resolve before waking anything, when both windows are readable: a name that does not
    # exist comes back as no_target, not as a wake-up nobody needed.
    ready = (
        window_state(hwnd) == "normal"
        and window_state(to_hwnd) == "normal"
        and capture_problem(hwnd) is None
    )
    ends: DragEnds | str | None = _drag_ends(hwnd, to_hwnd, args, grab_from) if ready else None
    if isinstance(ends, str):
        return ends
    # The destination's own pixels, read *now* — before the source window is raised over
    # it. Taken later, every "before" picture would be the source window and every drag
    # would look like it changed the destination.
    dst_rect = window_rect(to_hwnd)
    dst_state = window_state(to_hwnd)
    before = _crop(grab_screen(), dst_rect) if dst_rect else None
    value_before = ""
    if ends is not None and ends.target is not None:
        value_before, _ = _read_back(to_hwnd, ends.target)
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    if to_hwnd != hwnd and route is None:
        ok, to_note = _guarded_input(to_hwnd)
        if not ok:
            return to_note
        note += to_note
    if ends is None:
        ends = _drag_ends(hwnd, to_hwnd, args, grab_from)
        if isinstance(ends, str):
            return ends
    # The press has to land in the window the caller named, so the source is raised last:
    # the drop point was measured against what covers it, and that is what the pointer is
    # going to find during the drag.
    raised = True if _is_shell_surface(hwnd) else set_foreground(hwnd)
    problems = [
        target_problem(hwnd, ends.source)
        if ends.source is not None
        else point_problem(hwnd, ends.grab),
    ]
    if route is None:
        problems.append(point_problem(to_hwnd, ends.drop))
    else:
        # The whole point of a route is that the drop point is *not* reachable yet, so what
        # has to be reachable now is the waypoint itself.
        problems.append(point_problem(route.surface, route.target.center))
    for problem in problems:
        if problem:
            return f"ERROR: {problem}"  # refused before a single event was injected
    # The point the release lands on, re-read whenever the world is re-read: a window that
    # the shell brought forward may have been restored or moved on the way.
    drop_box = {"point": ends.drop}

    def refresh_drop() -> tuple[int, int] | str | None:
        fresh = resolve_drop(to_hwnd, args, ends.grab, carry=args.get("to_hwnd") in (None, ""))
        if isinstance(fresh, str):
            return fresh
        drop_box["point"] = fresh[0]
        return fresh[0]

    def drop_reachable() -> bool:
        """Is the drop point somewhere the destination really is?

        The state is asked first, and that is not a formality: a minimized window reports a
        0x0 client rect and an icon-slot window rect (measured 2026-09-17), and
        `WindowFromPoint` at that icon slot *does* answer with the window — so geometry alone
        says yes while there is nothing on screen to drop into. What the wait is really for is
        the moment the window is back."""
        if window_state(to_hwnd) != "normal":
            return False
        point = refresh_drop()
        return not isinstance(point, str) and point_problem(to_hwnd, point) is None

    facts = drag_to(
        ends.grab,
        ends.drop,
        button=str(args.get("button") or "left"),
        via=route.target.center if route is not None else None,
        wait=drop_reachable if route is not None else None,
        refresh=refresh_drop if route is not None else None,
        should_abort=should_abort,
        pre_release=lambda: point_problem(to_hwnd, drop_box["point"]),
    )
    released_at = drop_box["point"]
    time.sleep(SETTLE_S)
    # Bring the destination forward for the picture: the window the drag started from is
    # still in front (the press needed it there), so the drop's own repaint may be behind it.
    if to_hwnd != hwnd and not _is_shell_surface(to_hwnd):
        set_foreground(to_hwnd)
        time.sleep(SETTLE_S)
    dst_rect_after = window_rect(to_hwnd)
    after = _crop(grab_screen(), dst_rect_after) if dst_rect_after else None
    after_frame = grab_window(to_hwnd)
    value_after = ""
    if ends.target is not None:
        value_after, _ = _read_back_settled(to_hwnd, ends.target)
    moved = dst_rect != dst_rect_after
    # A destination that was minimized (or hidden in the tray) when the drag started has no
    # on-screen geometry and no pixels to compare — coming back on screen *is* the effect,
    # and it is the shell that did it, through the application's own path.
    came_back = dst_state != "normal" and window_state(to_hwnd) == "normal"
    changed = _diff_ratio(before, after) if before is not None and after is not None else 0.0
    value_changed = bool(value_after) and value_after != value_before
    injected_ok = bool(facts["started"] and facts["injected"] == facts["expected"])
    verified = (
        injected_ok
        and facts["stopped"] is None
        and (moved or came_back or value_changed or changed > DIFF_THRESHOLD)
    )
    key = f"drag:{hwnd}:{ends.grab_label}->{to_hwnd}:{ends.drop_label}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        at = covering_window(released_at)
        cover = (_window_text(at) or _class_name(at) or f"0x{at:X}") if at else "nothing"
        detail = (
            f"抓取点 {ends.grab}（{ends.grab_label}）→ 落点 {released_at}（{ends.drop_label}）；"
            f"注入={facts['injected']}/{facts['expected']}；点数={facts['points']}/{facts['steps']}；"
            f"落点归属={cover}；画面变化={changed:.3%}；窗口矩形 {dst_rect} → {dst_rect_after}"
            + (f"；{facts['stopped']}" if facts["stopped"] else "")
        )
        return _escalate(
            sink,
            key,
            f"drag {ends.grab_label} → {ends.drop_label}",
            detail,
            should_abort,
            on_answer,
            call_id,
        )
    if came_back:
        signal = f"the destination came back on screen (was {dst_state}, now normal)"
    elif moved:
        signal = f"the window moved {dst_rect} → {dst_rect_after}"
    elif value_changed:
        signal = f"read back {value_after!r} from {ends.drop_label}"
    elif changed > DIFF_THRESHOLD:
        signal = f"destination pixels changed {changed:.2%}"
    else:
        signal = "nothing changed on screen"
    lines = [
        f"DRAG {ends.grab_label} from hwnd=0x{hwnd:X} to {ends.drop_label} at {released_at} "
        f"in hwnd=0x{to_hwnd:X}{note}",
    ]
    if facts["stopped"]:
        lines.append(
            f"  stopped: {facts['stopped']}"
            + (" (Escape sent, button released)" if facts["started"] else "")
        )
    if route is not None:
        lines.append(
            f"  via: its {route.where} {route.label!r} at {route.target.center} — hovered "
            f"{facts['waited']:.2f}s"
            + (
                " until the drop point came within reach"
                if facts["stopped"] is None
                else " waiting for the window to come forward"
            )
        )
    lines += [
        f"  path: {facts['points']}/{facts['steps']} points in {facts['seconds']:.2f}s from "
        f"{ends.grab} · held {DRAG_HOLD_S:g}s before the first move, hovered {DRAG_DWELL_S:g}s "
        f"before letting go · "
        + (
            "button released (verified against the system)"
            if facts["released"]
            else "RELEASE NOT VERIFIED — the button may still be down"
        ),
        f"  verify: {signal} → {'verified' if verified else 'unverified'}",
    ]
    if not raised:
        lines.append("  could not raise the source window to the foreground!")
    lines.append("  control: athand script — no per-action prompt (running it is the consent)")
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary



TRAY_OVERFLOW_HINTS = ("显示隐藏的图标", "Show hidden icons", "显示隐藏的图标 ")
TRAY_FLYOUT_CLASSES = ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow")
SHELL_WAKE_S = (
    4.0  # the app's own wake path is asynchronous: a heavy tray app (WeChat) needs seconds
)
TRAY_FLYOUT_S = 2.0  # the overflow flyout: 0.12s to open, ~0.3s to fill (measured)
DESKTOP_CLASSES = ("Progman", "WorkerW")  # whichever of them hosts SHELLDLL_DefView
TRAY_BUTTON_PREFIX = "SystemTray."  # the notification strip, vs Taskbar.TaskListButton*
PINNED_HINTS = ("已固定", "Pinned")  # a pinned button is shown while the app is *not* running
GA_ROOT = 2


@dataclass(frozen=True)
class Entry:
    """A window's own door on screen: the icon or button a person would click.

    "触手可及" (at hand) is this tool's name for it (spec §35.15) — the application's
    entry point is on screen *now*, on the desktop, in the taskbar or in the tray, so
    opening the window can run the application's own path instead of moving the OS's
    idea of the window. Measured 2026-09-13: the OS path produced a window that was
    visible, foreground and screenshot-able, and swallowed every input (§35.12).

    `raise_first` is set for the tray strip only: that raise is what the pre-existing
    taskbar path did and it is measured working. A flyout is opened by us and is
    already in front, and the desktop is left alone — raising it is not what a person
    does before double-clicking an icon."""

    surface: int  # the window the entry is drawn in
    target: Target  # the rectangle to click
    where: str  # 桌面图标 | 任务栏按钮 | 托盘图标
    clicks: int  # a desktop icon opens on the second click
    label: str  # the row's own name, for the report
    raise_first: bool = False  # raise the surface before clicking (the tray strip only)


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def _norm(text: str) -> str:
    """Comparable form of a title or a row name: casefolded, zero-width characters
    removed.

    Measured on this box 2026-09-13: Edge's title is 'Fungi - 个人 - Microsoft\\u200b Edge'
    — a zero-width space between the two words — so the taskbar button
    'Microsoft Edge - 1 个运行窗口' shares no plain substring with it, and the window
    looked like it had no entry at all.
    """
    return text.translate(_ZERO_WIDTH).casefold()


def _app_tokens(win: Win) -> list[str]:
    """What a shell row for this application could be called.

    (the window title, the process stem). The taskbar names a button after the
    app's display name, not after its exe — measured on this box: '智能终端 - 1
    个运行窗口' for WindowsTerminal.exe — so the title is the better token, and the
    tray tooltip happens to carry it too (' QQ: 3754901636…').
    """
    tokens = [_norm(win.title).strip(), _norm(Path(win.proc).stem) if win.proc else ""]
    return [token for token in tokens if len(token) >= 2]


def _row_app_name(name: str) -> str:
    """A taskbar row's application name: '文件资源管理器 - 1 个运行窗口' → '文件资源管理器'.

    Windows 11 names a taskbar button after the app's display name and appends the
    window count; a desktop icon and a tray tooltip carry the bare name.
    """
    head, sep, tail = name.partition(" - ")
    if sep and ("运行窗口" in tail or tail.strip().isdigit()):
        return head.strip()
    return name.strip()


def _mentions(haystack: str, needle: str) -> bool:
    """Is `needle` a whole word of `haystack`?

    Whole words only, because the names are short: a desktop icon called 'OS'
    matched mid-word in 'Microsoft Edge' and in 'Task Host Window' — entries for
    applications that have nothing to do with it (measured 2026-09-13).
    """
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _shell_row(rows: list[Target], win: Win) -> Target | None:
    """The row in a shell surface that belongs to `win`.

    Both directions are needed, because a row and a window name the same application
    differently (measured 2026-09-13):

    * the row **contains** the window's title or process stem — 'QQ' on the desktop,
      'QQ: 3754901636' in the tray, 'Microsoft Edge - 1 个运行窗口' for a title of
      'Fungi - 个人 - Microsoft\u200b Edge';
    * the row's application name is **contained in** the window's title — the Explorer
      case that the first direction alone never matched: the button says
      '文件资源管理器 - 1 个运行窗口' while the window says 'Fungi - 文件资源管理器'.

    The second direction is only trusted where a row's name really is an application's
    name — a taskbar button or a desktop icon. A tray tooltip is free-form text chosen
    by the app, and measured on this box it can be a bare word that also appears in a
    foreign title: the tray icon 'Fungi' would otherwise claim the window titled
    'Fungi - 个人 - Microsoft Edge'.
    """
    names = [(_norm(cand.name), cand) for cand in rows]
    for token in _app_tokens(win):
        for name, cand in names:
            if _mentions(name, token):
                return cand
    title = _norm(win.title).strip()
    if title:
        for _, cand in names:
            if cand.cls.startswith(TRAY_BUTTON_PREFIX):
                continue
            app = _norm(_row_app_name(cand.name))
            if len(app) >= 2 and _mentions(title, app):
                return cand
    return None


def _entry_sized(cand: Target, surface: int) -> bool:
    """Is this row an icon/button, rather than the container it is drawn in?

    Both surfaces expose their own container as a row covering everything — the
    desktop's '桌面' SysListView32 is 2240x1400, the taskbar's frame is the whole bar —
    and clicking a container opens nothing (measured 2026-09-13).
    """
    box = window_rect(surface)
    area = (cand.rect[2] - cand.rect[0]) * (cand.rect[3] - cand.rect[1])
    if box is None:
        return True
    whole = (box[2] - box[0]) * (box[3] - box[1])
    return not whole or area * 4 < whole


def _surface_rows(surface: int) -> list[Target]:
    """The rows of a shell surface that could be an entry.

    Containers are out (see `_entry_sized`), and so is a pinned taskbar button:
    Windows shows that form only while the application is *not* running, so it can
    never be the way to a window we are already holding.
    """
    return [
        cand
        for cand, _ in _scan(surface, limit=80)
        if _entry_sized(cand, surface) and not any(hint in cand.name for hint in PINNED_HINTS)
    ]


def _desktop_surface() -> int:
    """The window the desktop icons are drawn in, or 0.

    Explorer draws them into a `SysListView32` under a `SHELLDLL_DefView`, hosted by
    `Progman` — or by a `WorkerW` when something else owns the wallpaper. Measured on
    this box 2026-09-13: Progman 0x10148 → SHELLDLL_DefView → SysListView32, whose rows
    are one per icon ('QQ', '微信', '学习', …) with the container row '桌面' on top.
    """
    for win in list_windows(include_hidden=True):
        if win.cls not in DESKTOP_CLASSES:
            continue
        if int(_u32.FindWindowExW(win.hwnd, 0, "SHELLDLL_DefView", None) or 0):
            return win.hwnd
    return 0


def _desktop_reachable(point: tuple[int, int]) -> bool:
    """Is the desktop icon still the thing at this point?

    Anything covering the desktop covers its icons with it, and a blind double-click
    would land on that window — the wrong-click class this tool refuses everywhere
    else. Measured 2026-09-14: with a browser maximized, `WindowFromPoint` at a desktop
    icon returned the browser's render host; with the desktop showing, it returned the
    desktop's own SysListView32 (root: Progman). Raising the desktop does not help:
    `set_foreground(Progman)` makes 'Program Manager' the foreground window and leaves
    the covering window exactly where it was.
    """
    desktop = _desktop_surface()
    return bool(desktop) and covering_window(point) == desktop


def _entry(surface: int, found: Target, where: str, clicks: int, *, raise_first=False) -> Entry:
    """One door on screen. The label is the row's own first line, trimmed — the tray
    writes its tooltip with a leading space (' QQ: 3754901636…')."""
    return Entry(surface, found, where, clicks, found.name.splitlines()[0].strip(), raise_first)


def _tray_flyout() -> Win | None:
    """The overflow flyout *while it is open*, or None.

    Measured on this box 2026-09-13: the island window is not created on demand — it
    already exists, hidden, before the arrow is ever clicked. So "it exists" says
    nothing; its state does ('hidden' → 'normal' 0.12s after the arrow click), and its
    rows appear a moment later still (12 icons read at 0.30s).
    """
    return next(
        (
            w
            for w in list_windows(include_hidden=True)
            if w.cls in TRAY_FLYOUT_CLASSES and w.state == "normal"
        ),
        None,
    )


def _close_tray_flyout() -> None:
    """Put the overflow flyout away again — but only when it is open: an Escape sent
    into a hidden flyout goes to whatever window has the focus instead."""
    if _tray_flyout() is not None:
        send_keys(["escape"])


def _tray_overflow_entry(win: Win, tray_rows: list[Target]) -> Entry | None:
    """Look behind the overflow chevron: that is where a tray-only app's icon lives.

    Opens the flyout, keeps looking until it is open *and* populated (both are later
    than the click), and closes it again when the application is not in there. Left
    open when it is — the caller clicks the icon it returns.
    """
    chevron = next((c for c in tray_rows if any(h in c.name for h in TRAY_OVERFLOW_HINTS)), None)
    if chevron is None:
        return None
    # The arrow is a *toggle*, measured the hard way: an earlier attempt that left the
    # flyout open made the next arrow click close it, and the search then found nothing.
    # So: only click it when the flyout is actually closed.
    opened = _tray_flyout() is None
    if opened:
        tray = int(_u32.FindWindowW("Shell_TrayWnd", None) or 0)
        set_foreground(tray)
        click_at(*chevron.center)
    deadline = time.monotonic() + TRAY_FLYOUT_S
    while time.monotonic() < deadline:
        flyout = _tray_flyout()
        if flyout is not None:
            found = _shell_row(_surface_rows(flyout.hwnd), win)
            if found is not None:
                return _entry(flyout.hwnd, found, "托盘图标", 1)
        time.sleep(0.1)
    if opened:
        _close_tray_flyout()
    return None


def at_hand(win: Win) -> Entry | None:
    """The entry on screen that opens this window — 触手可及 — or None (spec §35.15).

    The order is the user's decision (2026-09-13): 桌面、任务栏、托盘 all count as at
    hand, but a taskbar button and a tray icon *activate* the window that is already
    running, while a desktop icon is a double-click that *launches* the application
    when it is not — so the desktop goes last.
    """
    tray = int(_u32.FindWindowW("Shell_TrayWnd", None) or 0)
    if tray:
        rows = _surface_rows(tray)
        found = _shell_row(rows, win)
        if found is not None:
            where = "托盘图标" if found.cls.startswith(TRAY_BUTTON_PREFIX) else "任务栏按钮"
            return _entry(tray, found, where, 1, raise_first=True)
        entry = _tray_overflow_entry(win, rows)
        if entry is not None:
            return entry
    desktop = _desktop_surface()
    if desktop:
        icon = _shell_row(_surface_rows(desktop), win)
        if icon is not None and _desktop_reachable(icon.center):
            return _entry(desktop, icon, "桌面图标", 2)
    return None


# Families that draw themselves and only wake input/a11y on their own activation
# path: Chromium/Electron (QQ), Qt (WeChat), and Qt's rendered surfaces.
SELF_DRAWN_CLASSES = ("Chrome_WidgetWin", "Chrome_RenderWidgetHost", "Qt5", "Qt6", "MMUIRender")


def shell_reason(hwnd: int, win: Win, *, just_woken: bool = False) -> str | None:
    """Why this window wants the shell path *first*, or None if the cheap wake is fine.

    Decided before anything is touched, from three signals measured on 2026-09-13:

    * it is not on screen (minimized or hidden): that is the application's own tray
      state, and `ShowWindow` cannot make the app *believe* it left the tray;
    * its a11y has nothing addressable at all — no name and no class anywhere,
      which is the QQ signature (7 anonymous ScrollItem shells);
    * its class is a self-drawn family (Chromium/Electron, Qt, MMUIRender): those
      render inside themselves and only attach input and accessibility when their
      own activation path runs.
    """
    state = window_state(hwnd)
    if state != "normal":
        return f"it is {state}: the application's own wake path is what ends that state"
    if not any(cand.name or cand.cls for cand, _ in _scan(hwnd, limit=40)):
        return "its a11y offers nothing addressable — the app is still asleep"
    if just_woken and win.cls.startswith(SELF_DRAWN_CLASSES):
        # Only for a window we just brought back: a healthy Chromium app (Edge) has
        # named controls and needs no help, while one that was sitting in the tray
        # keeps its renderer asleep even once it is visible again.
        return f"it draws itself ({win.cls}) and was just brought back"
    return None


def _is_shell_surface(hwnd: int) -> bool:
    """The desktop or the taskbar: the two windows that are never raised.

    `set_foreground` there measured 2026-09-14 as pure loss: on the desktop it moved the
    foreground to 'Program Manager' and uncovered not one icon, so it takes the user's
    focus and buys nothing. Clicks do not need the help either — the taskbar is topmost,
    and the desktop's icons are only clicked while `covering_window` already says the
    desktop is what is there.
    """
    return hwnd in (int(_u32.FindWindowW("Shell_TrayWnd", None) or 0), _desktop_surface())


def via_entry(win: Win) -> Entry | str:
    """Where a drag may hover to bring this window forward: its taskbar button.

    The user's own route (2026-09-17): when something covers the window you are carrying
    something to, carry it onto that window's *taskbar button* and hold — the shell brings
    the window to the front by itself — then continue into it. Only the taskbar qualifies:
    a tray icon's flyout would have to be *clicked* open, and a click is not available while
    a drag is holding the button down, so an application that lives only in the notification
    area cannot be reached this way (the result says so rather than pretending).
    """
    tray = int(_u32.FindWindowW("Shell_TrayWnd", None) or 0)
    if not tray:
        return "there is no taskbar on this desktop to route the drag through"
    found = _shell_row(_surface_rows(tray), win)
    if found is None:
        return (
            f"{win.title!r} has no taskbar button right now, and the shell route to bring a "
            "window forward needs one"
        )
    if found.cls.startswith(TRAY_BUTTON_PREFIX):
        return (
            "its row in the notification area is a tray icon, not a taskbar button — that "
            "flyout has to be clicked open, which a drag cannot do while holding the button"
        )
    return _entry(tray, found, "任务栏按钮", 1)


def _no_entry_reason(win: Win) -> str:
    """Why nothing could be clicked: the windows an application has no entry in.

    A desktop icon that exists but is covered is worth naming — the application *does*
    have a door, it is just behind something right now, and a person would clear the
    screen before double-clicking it (spec §35.15)."""
    desktop = _desktop_surface()
    icon = _shell_row(_surface_rows(desktop), win) if desktop else None
    if icon is not None:
        return (
            f"its desktop icon {icon.name.splitlines()[0].strip()!r} is covered by another "
            "window — nothing to click while it is (bring the desktop to the front first)"
        )
    return "no taskbar button, no tray icon and no desktop icon to open it through"


def shell_wake(hwnd: int) -> str:
    """Open an application the way its own icon does — through the shell.

    `ShowWindow` + `SetForegroundWindow` only move the *operating system's* idea of
    the window: measured on this box (2026-09-13), a QQ window woken that way was
    the real foreground window, and yet every one of its windows ignored
    SendInput, batched SendInput, and even a directly posted WM_LBUTTONDOWN — while
    the same code drove a plain Win32 window (click confirmed by the app itself).
    The application still believed it was in the tray: no render surface attached,
    no a11y, no input.

    Clicking the icon in the taskbar or the notification area is what the user does,
    and it works because that click lands on *explorer*: the shell then delivers the
    application's own tray/activation callback, and the app runs its real
    "open my window" path. Measured the same afternoon: after that click the window
    went hidden -> normal and its a11y went from 7 anonymous shells to 20 elements
    with names.

    What gets clicked is whatever is 触手可及 (`at_hand`, spec §35.15): the taskbar
    button or tray icon when the app has one, else the desktop icon, double-clicked
    because a single click only selects it. Nothing at hand means nothing to click —
    the caller may still try `ShowWindow`, and should say out loud that the result is
    likely the "visible but asleep" window.
    """
    win = next((w for w in list_windows(include_hidden=True) if w.hwnd == hwnd), None)
    if win is None:
        return "the window is gone"
    if window_state(hwnd) == "normal" and foreground_hwnd() == hwnd:
        # Clicking the taskbar button of the window that is already in front *minimizes*
        # it — Windows toggles on that click. Nothing to open, so click nothing.
        return "it is already on screen and in front — nothing to open"
    entry = at_hand(win)
    if entry is None:
        _close_tray_flyout()
        return f"no entry at hand: {_no_entry_reason(win)}"
    if entry.raise_first:
        set_foreground(entry.surface)
    # A click on an entry can open a *different* window of the application, and for a
    # desktop icon that is the normal case. Measured 2026-09-13 on OneDrive: its tray
    # icon raised the 'Activity Center' while the window we were holding stayed a hidden
    # balloon host — so reporting only the held window would say "nothing happened" about
    # a click that plainly did something. The evidence is the window list, the same one
    # the double_click action uses (spec §35.13).
    before = {w.hwnd for w in list_windows()}
    click_at(*entry.target.center, clicks=entry.clicks)
    woke = _await_state(hwnd, "normal", SHELL_WAKE_S)
    opened = f"{entry.where} {entry.label!r}" + (" (double-click)" if entry.clicks == 2 else "")
    if woke:
        # A tray click leaves the overflow flyout open, and it has to stay that way: the
        # app's window often light-dismisses on any outside click, and clicking the arrow
        # to tidy up measured taking OneDrive's panel back down with it (2026-09-13). So
        # the flyout is reported, not cleaned up.
        note = " (the notification flyout is still open)" if _tray_flyout() is not None else ""
        return f"clicked its {opened} and it came up{note}"
    appeared = [w for w in list_windows() if w.hwnd not in before]
    if appeared:
        return f"clicked its {opened} and it opened {appeared[0].title!r} instead"
    _close_tray_flyout()
    return f"clicked its {opened} but it stayed hidden"


def _await_state(hwnd: int, want: str, timeout: float) -> bool:
    """Poll the window's own state until it matches — the app wakes on its own clock."""
    deadline = time.monotonic() + timeout
    while True:
        if window_state(hwnd) == want:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _named_a11y(hwnd: int) -> int:
    """How many addressable (named) controls the window's a11y offers right now."""
    return sum(1 for cand, _ in _scan(hwnd, limit=40) if cand.name)


def wake_window(hwnd: int, via: str = "auto") -> list[str]:
    """Get a window on screen, choosing the path the *application* needs.

    Returns the lines to report. `auto` reads the window before touching it (state,
    whether its a11y is addressable at all, whether it draws itself): for a
    tray-resident or self-drawn app the shell click runs **first and alone** —
    running `ShowWindow` first would only make a dead window visible, and it also
    destroys the very evidence the decision rests on. `ShowWindow` gets a turn only
    if the app still is not on screen afterwards.
    """
    win = next((w for w in list_windows(include_hidden=True) if w.hwnd == hwnd), None)
    if win is None:
        return ["  the window is gone"]
    notes: list[str] = []
    reason = shell_reason(hwnd, win) if via == "auto" else None
    if via == "shell" or reason is not None:
        if reason is not None:
            notes.append(f"  shell wake first: {reason}")
        notes.append(f"  shell wake: {shell_wake(hwnd)}")
        time.sleep(SETTLE_S)
        if window_state(hwnd) != "normal":
            # One clean retry: the first click can be eaten while the overflow flyout
            # is still closing.
            notes.append(f"  shell wake (retry): {shell_wake(hwnd)}")
            time.sleep(SETTLE_S)
    if window_state(hwnd) != "normal":
        restored = ensure_on_screen(hwnd)
        if restored != "normal" and window_state(hwnd) != "normal":
            # A window minimized by "show desktop" is held there by the shell, not by
            # the application: `ShowWindow(SW_RESTORE)` measured 2026-09-13 left five
            # of them minimized, while the taskbar button — the shell's own path —
            # brings them back. So the shell gets a turn whenever the API did not work.
            notes.append(f"  ShowWindow left it {restored}; shell path: {shell_wake(hwnd)}")
            time.sleep(SETTLE_S)
    if window_state(hwnd) != "normal":
        was = ensure_on_screen(hwnd)
        notes.append(
            f"  OS wake (ShowWindow): the shell path left it {was}"
            " — the application never ran its own wake path, so this window may look"
            " normal and still ignore every input (measured on QQ and WeChat, 2026-09-13)"
        )
        time.sleep(SETTLE_S)
    if not _is_shell_surface(hwnd):
        set_foreground(hwnd)
        time.sleep(SETTLE_S)
    return notes


def _action_restore(args: dict) -> str:
    """Bring a window back on screen, through the path the application needs.

    `via` defaults to `auto`: the shell entry (its taskbar button, or its tray icon
    behind the notification area's overflow) for a tray-resident or self-drawn app,
    `ShowWindow` otherwise — see `wake_window` and spec §35.12.
    """
    hwnd = int(args["hwnd"])
    via = str(args.get("via") or "auto").strip().lower()
    was = window_state(hwnd)
    notes = wake_window(hwnd, via)
    now = window_state(hwnd)
    frame = grab_window(hwnd) if now == "normal" else None
    summary = (
        f"RESTORE hwnd=0x{hwnd:X} {_window_text(hwnd)!r} → {now}"
        f" (was {was}, named controls: {_named_a11y(hwnd) if now == 'normal' else 0})\n"
        "  control: athand script — no per-action prompt (running it is the consent)"
    )
    if notes:
        summary += "\n" + "\n".join(notes)
    return _attach(summary, frame) if frame is not None else summary



# ── the command line ───────────────────────────────────────────────────────
# Actions that need no window identity at all.
_PLAIN_ACTIONS = {
    "windows": _action_windows,
    "shot": _action_shot,
}
# Read the stored listing back (the labels, and the numbers when they are still valid) but
# inject nothing: `targets` scans afresh and prints the labels, `label` resolves `--target`
# out of the numbers. Both ran before that read once, and a label then vanished between the
# process that gave it and the one that listed (measured 2026-09-17).
_LISTING_ONLY_ACTIONS = {"targets": _action_targets, "label": _action_label}
# May inject, directly or through the wake path a window may need first. Only these are
# refused when the input desktop is not this session's.
_INJECTING_ACTIONS = frozenset(
    {"click", "double_click", "drag", "type", "key", "scroll", "restore"}
)
_INPUT_ACTIONS = {
    "click": _action_click,
    "double_click": _action_double_click,
    "drag": _action_drag,
    "type": _action_type,
    "key": _action_key,
    "scroll": _action_scroll,
}
COMMANDS = (
    "windows",
    "shot",
    "targets",
    "label",
    "click",
    "double-click",
    "drag",
    "type",
    "key",
    "scroll",
    "restore",
    "release",
)


def _needs_listing(action: str, args: dict) -> bool:
    """Which calls cannot survive a window that has moved since its listing.

    A number is meaningless away from the picture it was cut from, and a label is bound to a
    rectangle — those are refused. A *name* is re-matched against the window's live a11y tree
    on every call, so it keeps working (a label reached by name refuses itself with its own
    message, see `_find_label`).
    """
    if action == "label":
        return True
    return args.get("target") is not None or args.get("to_target") is not None


def _dispatch(args: dict) -> str:
    """One action, from the same `args` dictionary the tool used to take."""
    action = str(args.get("action") or "")
    if action in _PLAIN_ACTIONS:
        return _PLAIN_ACTIONS[action](args)
    if action in _INJECTING_ACTIONS:
        blocked = desktop_problem()
        if blocked:
            # Asked before anything is measured: with the lock screen up every action
            # otherwise fails in its own way and blames itself (measured 2026-09-17).
            return f"ERROR: {blocked}"
    hwnd = args.get("hwnd")
    if hwnd in (None, ""):
        return f"ERROR: {action} needs --hwnd <window id from windows>; identity is never guessed"
    hwnd = int(hwnd)
    if not _u32.IsWindow(hwnd):
        return f"ERROR: no such window: 0x{hwnd:X}"
    if action == "drag":
        to_hwnd = args.get("to_hwnd")
        to_hwnd = int(to_hwnd) if to_hwnd not in (None, "") else hwnd
        if not _u32.IsWindow(to_hwnd):
            return f"ERROR: no such window to drop into: 0x{to_hwnd:X}"
        if to_hwnd != hwnd:
            stale_drop = _load_listing(to_hwnd)
            if stale_drop and args.get("to_target") is not None:
                return stale_drop
    try:
        stale = _load_listing(hwnd)
        if stale and _needs_listing(action, args):
            return stale
        if action == "restore":  # asks nothing, so it takes no ask plumbing
            return _action_restore(args)
        if action in _LISTING_ONLY_ACTIONS:
            return _LISTING_ONLY_ACTIONS[action](args)
        return _INPUT_ACTIONS[action](args, None, None, None, None)
    finally:
        # Belt and braces: no injection path may leave a key down or the left button held,
        # including the ones that raise or return early. A stuck button is a fault only a
        # human can clear, exactly like a stuck Ctrl.
        release_all_keys()
        release_all_buttons()


def run_action(args: dict) -> str:
    """`_dispatch`, plus the one failure that is a property of the machine and not of the
    call: a screen that cannot be read from this desktop."""
    try:
        return _dispatch(args)
    except ScreenUnavailable as exc:
        return f"ERROR: {exc}"


def _release_everything() -> str:
    """Lift whatever this machine still has pressed, and say what it was.

    A killed run cannot clean up after itself and its bookkeeping died with it, so the
    question goes to the OS instead: both mouse buttons and every key in this script's
    vocabulary are asked whether they are down, and the ones that are get a release event.
    Asking the system means it cannot tell a key this script left down from one the user is
    holding — that is the safe direction to be wrong in, and why this is the last resort.
    """
    lifted_buttons = [name for name in ("left", "right") if _button_is_down(name)]
    for name in lifted_buttons:
        lift_button(name)
    down_keys = sorted({vk for vk in _KEY_VK.values()} | set(_MODIFIER_VK))
    lifted_keys = [vk for vk in down_keys if int(_u32.GetAsyncKeyState(vk)) & 0x8000]
    for vk in lifted_keys:
        _key_event(vk, down=False)
    if not lifted_buttons and not lifted_keys:
        return "RELEASE\n  nothing was down."
    lines = ["RELEASE"]
    if lifted_buttons:
        lines.append(f"  mouse: lifted {', '.join(lifted_buttons)}")
    if lifted_keys:
        lines.append("  keys: lifted " + ", ".join(f"0x{vk:02X}" for vk in lifted_keys))
    return "\n".join(lines)


def _window_id(text: str) -> int:
    """`--hwnd 4394` and `--hwnd 0x112A` are the same window: `windows` prints both."""
    value = str(text).strip()
    try:
        return int(value, 0)
    except ValueError:
        pass
    try:
        return int(value, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a window id: {text!r}") from exc


def _target_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", type=int, help="candidate number from a targets listing")
    parser.add_argument("--name", help="the control's visible text instead of a number")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="athand.py",
        description=(
            "Look at this machine's desktop and act on it. Screen coordinates are never "
            "accepted: every gesture names a target that the program located itself."
        ),
    )
    subs = parser.add_subparsers(dest="command", required=True)

    windows = subs.add_parser("windows", help="the window list; hwnd is the identity")
    windows.add_argument(
        "--include",
        choices=("visible", "all"),
        default="visible",
        help="'all' adds minimized/hidden windows and the notification area",
    )

    shot = subs.add_parser("shot", help="write a PNG of the whole screen, or of one window")
    shot.add_argument("--hwnd", type=_window_id)

    targets = subs.add_parser(
        "targets", help="number this window's controls and draw the numbers on a picture"
    )
    targets.add_argument("--hwnd", type=_window_id, required=True)

    label = subs.add_parser("label", help="remember a name for a shape that has none")
    label.add_argument("--hwnd", type=_window_id, required=True)
    label.add_argument("--target", type=int, required=True)
    label.add_argument("--label", required=True)

    for name in ("click", "double-click"):
        gesture = subs.add_parser(
            name, help="click something, or open it with a double-click"
        )
        gesture.add_argument("--hwnd", type=_window_id, required=True)
        _target_flags(gesture)
        gesture.add_argument("--button", choices=("left", "right"), default="left")

    drag = subs.add_parser("drag", help="carry something with the button held down")
    drag.add_argument("--hwnd", type=_window_id, required=True)
    _target_flags(drag)
    drag.add_argument(
        "--from",
        dest="from_",
        choices=("titlebar",),
        help="grab the window's own title bar instead of a target (a reposition)",
    )
    drag.add_argument("--to-hwnd", dest="to_hwnd", type=_window_id, help="the destination window")
    drag.add_argument("--to-target", dest="to_target", type=int, help="drop point by number")
    drag.add_argument("--to-name", dest="to_name", help="drop point by visible text")
    drag.add_argument("--dx", type=int, help="shift the drop point horizontally (whole pixels)")
    drag.add_argument("--dy", type=int, help="shift the drop point vertically (whole pixels)")
    drag.add_argument("--route", choices=("taskbar",), help="hover the destination's taskbar button")
    drag.add_argument("--button", choices=("left", "right"), default="left")

    typing = subs.add_parser("type", help="enter text, one character at a time")
    typing.add_argument("--hwnd", type=_window_id, required=True)
    _target_flags(typing)
    typing.add_argument("--text", required=True)
    typing.add_argument("--char-delay", dest="char_delay", type=float, help="seconds per character")

    keys = subs.add_parser("key", help="press a key or a combination")
    keys.add_argument("--hwnd", type=_window_id, required=True)
    keys.add_argument("--keys", nargs="+", required=True)

    scroll = subs.add_parser("scroll", help="scroll a control into view")
    scroll.add_argument("--hwnd", type=_window_id, required=True)
    _target_flags(scroll)

    restore = subs.add_parser("restore", help="bring a minimized or tray-resident window back")
    restore.add_argument("--hwnd", type=_window_id, required=True)
    restore.add_argument("--via", choices=("auto", "window", "shell"), default="auto")

    subs.add_parser("release", help="lift every button and key this machine still has down")
    subs.add_parser(
        "selftest", help="run the bundled probes and check every gesture against them"
    ).add_argument("--keep", action="store_true", help="keep the selftest's temp directory")

    return parser


def _args_from_namespace(ns: argparse.Namespace) -> dict:
    args = {key: value for key, value in vars(ns).items() if value is not None}
    args.pop("command", None)
    args.pop("keep", None)
    if "from_" in args:
        args["from"] = args.pop("from_")
    return args


# ── selftest: every gesture against a probe that records what reached it ────
# The probes in `probes/` are the *application's* account of what arrived, so this checks the
# tool's claims rather than restating them: a click is judged by the WM_COMMAND the button
# sent, a selection drag by the EM_GETSEL the Edit reported, a reposition by the window
# rectangle the OS gives back. Run it after any change to the mechanics; `--keep` keeps the
# work directory (the JSONL the probes wrote, the PNGs, the listings) for a closer look.
class _Checks:
    """Three states, because a check that could not run is not a check that passed."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        state = "PASS" if ok else "FAIL"
        self.rows.append((name, state))
        print(f"{state}  {name}" + (f" — {detail}" if detail else ""), flush=True)
        return bool(ok)

    def skip(self, name: str, why: str) -> None:
        self.rows.append((name, "SKIP"))
        print(f"SKIP  {name} — {why}", flush=True)

    def failed(self) -> list[str]:
        return [name for name, state in self.rows if state == "FAIL"]


def _probe_script(name: str) -> str:
    return str(Path(__file__).resolve().parent / "probes" / name)


def _call(*argv: str, data: Path, timeout: float = 120.0) -> tuple[int, str]:
    """Run this script the way a caller does: same argv, same exit codes, same stdout."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *(str(item) for item in argv)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "ATHAND_DIR": str(data)},
        timeout=timeout,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _start_probe(
    script: str, work: Path, name: str, checks: _Checks, extra: tuple[str, ...] = ()
) -> tuple[subprocess.Popen, int] | None:
    log = work / f"{name}.jsonl"
    hwnd_file = work / f"{name}.hwnd"
    proc = subprocess.Popen(
        [
            sys.executable,
            _probe_script(script),
            "--log",
            str(log),
            "--hwnd-file",
            str(hwnd_file),
            *extra,
        ]
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if hwnd_file.exists():
            text = hwnd_file.read_text(encoding="utf-8").strip()
            if text:
                return proc, int(text)
        if proc.poll() is not None:
            checks.check(f"{script} comes up", False, f"the probe exited with {proc.returncode}")
            return None
        time.sleep(0.1)
    checks.check(f"{script} comes up", False, "no window after 20s")
    return None


def _close_probe(proc: subprocess.Popen, hwnd: int) -> None:
    """Close it the way a person would, then make sure it is gone: the point of a probe is
    that a run leaves nothing open behind it."""
    with contextlib.suppress(Exception):
        _u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def _events(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def _frame_path(text: str) -> str:
    match = re.search(r"\[(?:numbered f|f)rame: ([^\s]+?\.png)", text)
    return match.group(1) if match else ""


def _is_png(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _target_in(record: dict, *, cls: str = "", name: str = "") -> dict | None:
    for item in record.get("targets") or []:
        if cls and item.get("cls") != cls:
            continue
        if name and name not in str(item.get("name") or ""):
            continue
        return item
    return None


LABEL_CHECK = "a label outlives the process that gave it"
STALE_CHECK = "a number from a stale listing is refused, not guessed"
CLICK_CHECK = "click reaches the button (the app logged WM_COMMAND)"
LABEL_CLICK_CHECK = "a click by label reaches the same button (label read by a new process)"
TYPE_CHECK = "type puts the characters in the control (read back from the control itself)"
KEY_CHECK = "key presses land, and X replaces the Ctrl+A selection"
SCROLL_CHECK = "scroll uses ScrollItemPattern"
DRAG_MOVE_CHECK = "drag --from titlebar moves the window by the shift it was given"
DRAG_SELECT_CHECK = "drag carries a selection the app reports (EM_GETSEL)"
CANVAS_CHECK = "targets reads a canvas UI off the picture"
# Everything that injects: it cannot even be attempted without a listing to name targets in.
SKIPPED_WITHOUT_A_LISTING = (
    STALE_CHECK,
    CLICK_CHECK,
    LABEL_CLICK_CHECK,
    TYPE_CHECK,
    KEY_CHECK,
    SCROLL_CHECK,
    DRAG_MOVE_CHECK,
    DRAG_SELECT_CHECK,
    CANVAS_CHECK,
)
# Everything that goes through `_INJECTING_ACTIONS`, and so is refused before anything can
# be measured when the input desktop is not this session's.
SKIPPED_WHILE_LOCKED = (
    STALE_CHECK,
    CLICK_CHECK,
    LABEL_CLICK_CHECK,
    TYPE_CHECK,
    KEY_CHECK,
    SCROLL_CHECK,
    DRAG_MOVE_CHECK,
    DRAG_SELECT_CHECK,
)


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0][:110] if stripped else ""


def _last_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[-1][:110] if stripped else ""


def _verify_line(text: str) -> str:
    """The tool's own verdict — "verified: …" or "unverified: …" — which says *why* a gesture
    did not reach the control, and is the first thing to read when a check fails."""
    for line in text.splitlines():
        if "verify:" in line:
            return line.strip()[:150]
    return ""


def _hold(checks: _Checks, hwnd: int, name: str) -> bool:
    """Keep the probe in front for this one check, or say why it could not be.

    Everything guarded here injects into whatever is *focused* or under the pointer. If another
    window has come forward in the meantime (a person typing, the shell activating something),
    the gesture would land there — so the check is skipped rather than aimed at the wrong
    window (measured 2026-09-17: a run typed a test string into whatever held the foreground).
    """
    if foreground_hwnd() == hwnd or set_foreground(hwnd):
        return True
    checks.skip(name, "the probe lost the foreground: the machine is in use")
    return False


# The tool's own refusals that mean "the desktop moved under me", not "the tool is wrong": the
# window could not be raised, or something else is at the point. They are the machine's answer,
# so a check they interrupt is a SKIP — the alternative is a red row every time a person uses
# their own computer (measured 2026-09-17, twice).
_ENVIRONMENT_REFUSALS = (
    "could not be brought to the foreground",
    "could not raise the source window",
    "belongs to",
    "has no rectangle any more",
)


def _environment_refusal(out: str) -> str:
    for phrase in _ENVIRONMENT_REFUSALS:
        if phrase in out:
            return phrase
    return ""


def _selftest_summary(checks: _Checks, work: Path, keep: bool) -> int:
    failures = checks.failed()
    total = len([row for row in checks.rows if row[1] != "SKIP"])
    print(
        f"\n{total - len(failures)}/{total} checks passed"
        + (f"; failed: {', '.join(failures)}" if failures else "")
    )
    print(f"evidence: {work}" + ("" if keep else " (removed; pass --keep to look at it)"))
    return 1 if failures else 0


def _selftest(keep: bool = False) -> int:
    global DATA_DIR
    checks = _Checks()
    work = Path(tempfile.mkdtemp(prefix="athand-selftest-"))
    data = work / "data"
    # The children are handed this through ATHAND_DIR; this process has to look in the same
    # place, or it reads the listings of a directory nobody wrote to (measured, 2026-09-17).
    DATA_DIR = data
    probes: list[tuple[subprocess.Popen, int]] = []
    try:
        locked = desktop_problem()
        if locked:
            print(f"NOTE  {locked}")
            print("NOTE  input-dependent checks cannot pass until the user is back.\n")
        # The probe is asked to be DPI-aware: with a user at the machine an unaware window is
        # uncapturable the moment anything else takes the foreground, which made half of these
        # checks fail for a reason that had nothing to do with the tool (measured 2026-09-17).
        started = _start_probe("target.py", work, "target", checks, ("--dpi-aware",))
        if started is None:
            return 1
        probe, hwnd = started
        probes.append(started)
        log = work / "target.jsonl"

        # ── read-only: the list, the pixels, the numbers ───────────────────
        code, out = _call("windows", data=data)
        checks.check(
            "windows lists the probe by hwnd",
            code == 0 and f"hwnd={hwnd}" in out and "athand target" in out,
            f"exit {code}",
        )
        code, out = _call("shot", "--hwnd", hwnd, data=data)
        shot = _frame_path(out)
        checks.check(
            "shot --hwnd writes a PNG and prints its path",
            code == 0 and bool(shot) and _is_png(shot),
            shot or out.strip()[:90],
        )
        code, out = _call("targets", "--hwnd", hwnd, data=data)
        listing = _read_json(_listing_path(hwnd))
        button = _target_in(listing, cls="Button")
        edit = _target_in(listing, cls="Edit")
        checks.check(
            "targets numbers the probe's controls",
            code == 0
            and f"hwnd=0x{hwnd:X}" in out
            and bool(button)
            and bool(edit)
            and len(listing.get("targets") or []) >= 4,
            f"{len(listing.get('targets') or [])} candidates",
        )
        frame = str(listing.get("frame") or "")
        checks.check("the listing's numbered frame exists", bool(frame) and _is_png(frame), frame)

        # Everything below stands on a listing. Without one there is nothing to test *with*,
        # and the reason is the machine's, not the tool's — say so instead of blaming it.
        usable = bool(button and edit)
        if not usable:
            reason = "the window's controls could not be listed" + (
                " (the machine is locked)" if locked else ""
            )
            for name in SKIPPED_WITHOUT_A_LISTING:
                checks.skip(name, reason)
            return _selftest_summary(checks, work, keep)

        # ── a label is stored where the next process can read it ───────────
        code, out = _call(
            "label", "--hwnd", hwnd, "--target", button["n"], "--label", "ping", data=data
        )
        _code, relisted_out = _call("targets", "--hwnd", hwnd, data=data)
        relisted = _read_json(_listing_path(hwnd))
        # The label is a *display* name: the next listing prints it over the button's own text,
        # while the stored record keeps the name the program read (so its number stays
        # re-matchable — see `_action_targets`).
        shows = bool(re.search(r"'ping' cls=", relisted_out))
        checks.check(
            LABEL_CHECK,
            code == 0 and shows,
            f"label exit {code}; the next listing shows the label: {shows}",
        )
        button = _target_in(relisted, cls="Button") or button
        edit = _target_in(relisted, cls="Edit") or edit

        if locked:
            # The dispatch refuses input actions before it ever consults the listing, so with
            # the lock screen up these cannot run — and a check that could not run is a SKIP,
            # not a pass.
            for name in SKIPPED_WHILE_LOCKED:
                checks.skip(name, "the machine is locked: nothing can be injected")
            return _selftest_summary(checks, work, keep)

        # Everything from here injects real input and takes the foreground — which is where the
        # person who is using this machine loses theirs. Measured 2026-09-17: a run started
        # seconds after the user came back failed half its checks (their windows kept coming
        # forward over the probe) and stole focus from the terminal this tool was being driven
        # from. If the probe cannot be raised, somebody else is working: say so and stop.
        if not set_foreground(hwnd) or foreground_hwnd() != hwnd:
            for name in SKIPPED_WHILE_LOCKED:
                checks.skip(name, "the probe cannot hold the foreground: the machine is in use")
            return _selftest_summary(checks, work, keep)

        # ── the listing is bound to where the window was ───────────────────
        rect = window_rect(hwnd) or (0, 0, 0, 0)
        _u32.SetWindowPos(
            hwnd, 0, rect[0] + 7, rect[1] + 5, 0, 0, SWP_NOSIZE | SWP_NOZORDER
        )
        time.sleep(0.2)
        code, out = _call("click", "--hwnd", hwnd, "--target", button["n"], data=data)
        checks.check(
            STALE_CHECK,
            code == 2 and "targets --hwnd" in out,
            _first_line(out),
        )
        _u32.SetWindowPos(hwnd, 0, rect[0], rect[1], 0, 0, SWP_NOSIZE | SWP_NOZORDER)
        time.sleep(0.2)
        _call("targets", "--hwnd", hwnd, data=data)
        listing = _read_json(_listing_path(hwnd))
        button = _target_in(listing, cls="Button") or button
        edit = _target_in(listing, cls="Edit") or edit

        # ── input: judged by what the application itself recorded ──────────
        # Every check counts what the *probe* recorded before and after, so one failing gesture
        # cannot make the next one look like a pass (the first run of this selftest did: the
        # label click "failed" only because the earlier click had not landed).
        def button_clicks() -> int:
            return len(
                [e for e in _events(log) if e.get("ev") == "command" and e.get("id") == 102]
            )

        def edit_text() -> str:
            """What the *probe* says its Edit holds.

            Read from the probe's own log, never from this process with `GetWindowText`: for a
            control in another process that call hands back a stale title, so it would report a
            working `type` as a failure (measured 2026-09-17).
            """
            values = [e.get("value", "") for e in _events(log) if e.get("ev") == "text"]
            return str(values[-1]) if values else ""

        def interrupted(name: str, code: int, out: str) -> bool:
            """A check the desktop answered for: report it as skipped, with the tool's words."""
            if code == 0:
                return False
            reason = _environment_refusal(out)
            if not reason:
                return False
            checks.skip(name, f"the desktop moved under the probe ({reason}): {_first_line(out)}")
            return True

        if _hold(checks, hwnd, CLICK_CHECK):
            before = button_clicks()
            code, out = _call("click", "--hwnd", hwnd, "--target", button["n"], data=data)
            landed = button_clicks() - before
            if not interrupted(CLICK_CHECK, code, out):
                checks.check(
                    CLICK_CHECK,
                    code == 0 and "verified" in out and landed == 1,
                    f"exit {code}; the button logged {landed} click(s); {_first_line(out)}",
                )
        if _hold(checks, hwnd, LABEL_CLICK_CHECK):
            before = button_clicks()
            code, out = _call("click", "--hwnd", hwnd, "--name", "ping", data=data)
            landed = button_clicks() - before
            if not interrupted(LABEL_CLICK_CHECK, code, out):
                checks.check(
                    LABEL_CLICK_CHECK,
                    code == 0 and landed == 1,
                    f"exit {code}; the button logged {landed} click(s); {_first_line(out)}",
                )
        if _hold(checks, hwnd, TYPE_CHECK):
            code, out = _call(
                "type",
                "--hwnd",
                hwnd,
                "--target",
                edit["n"],
                "--text",
                "hello",
                "--char-delay",
                "0.05",
                data=data,
            )
            text = edit_text()
            if not interrupted(TYPE_CHECK, code, out):
                if "hello" not in text and foreground_hwnd() != hwnd:
                    checks.skip(TYPE_CHECK, "the foreground left the probe during the check")
                else:
                    checks.check(
                        TYPE_CHECK,
                        code == 0 and "hello" in text,
                        f"exit {code}; the Edit now holds {text!r}; {_verify_line(out)}",
                    )
        if _hold(checks, hwnd, KEY_CHECK):
            _call("key", "--hwnd", hwnd, "--keys", "ctrl", "a", data=data)
            code, out = _call(
                "type", "--hwnd", hwnd, "--text", "X", "--char-delay", "0.05", data=data
            )
            text = edit_text()
            if not interrupted(KEY_CHECK, code, out):
                if text != "X" and foreground_hwnd() != hwnd:
                    checks.skip(KEY_CHECK, "the foreground left the probe during the check")
                else:
                    checks.check(
                        KEY_CHECK,
                        code == 0 and text == "X",
                        f"the Edit now holds {text!r}; {_verify_line(out)}",
                    )
        item = _target_in(listing, cls="ListItem") or _target_in(listing, name="项目")
        if item is None:
            checks.skip(SCROLL_CHECK, "no list item in the listing")
        else:
            code, out = _call("scroll", "--hwnd", hwnd, "--target", item["n"], data=data)
            checks.check(
                SCROLL_CHECK,
                code == 0 and "ScrollItemPattern=used" in out,
                _last_line(out),
            )
        code, out = _call("release", data=data)
        checks.check("release lifts nothing when nothing is down", code == 0 and "RELEASE" in out)

        # ── drag: the path the application recorded, and the rectangle ─────
        started = _start_probe("drag_probe.py", work, "drag", checks)
        if started is None:
            return 1
        drag_probe, drag_hwnd = started
        probes.append(started)
        drag_log = work / "drag.jsonl"
        _call("targets", "--hwnd", drag_hwnd, data=data)
        before = window_rect(drag_hwnd) or (0, 0, 0, 0)
        if _hold(checks, drag_hwnd, DRAG_MOVE_CHECK):
            code, out = _call(
                "drag",
                "--hwnd",
                drag_hwnd,
                "--from",
                "titlebar",
                "--dx",
                "60",
                "--dy",
                "40",
                data=data,
            )
            after = window_rect(drag_hwnd) or (0, 0, 0, 0)
            moved = (after[0] - before[0], after[1] - before[1])
            if not interrupted(DRAG_MOVE_CHECK, code, out):
                checks.check(
                    DRAG_MOVE_CHECK,
                    code == 0 and abs(moved[0] - 60) <= 2 and abs(moved[1] - 40) <= 2,
                    f"moved {moved}, wanted (60, 40); exit {code}; {_first_line(out)}",
                )
        # The move above leaves the listing behind it: the tool refuses a number measured
        # against where the window *was* (measured 2026-09-17 — that refusal is the point of the
        # listing), so list the moved window again before naming its Edit.
        _call("targets", "--hwnd", drag_hwnd, data=data)
        drag_listing = _read_json(_listing_path(drag_hwnd))
        drag_edit = _target_in(drag_listing, cls="Edit")
        if drag_edit is None:
            checks.skip(DRAG_SELECT_CHECK, "the probe's Edit is not in the listing")
        elif _hold(checks, drag_hwnd, DRAG_SELECT_CHECK):
            code, out = _call(
                "drag", "--hwnd", drag_hwnd, "--target", drag_edit["n"], "--dx", "300", data=data
            )
            selections = [
                e["sel"] for e in _events(drag_log) if e.get("ev") == "selection" and e.get("sel")
            ]
            picked = [sel for sel in selections if sel[1] > sel[0]]
            if not interrupted(DRAG_SELECT_CHECK, code, out):
                checks.check(
                    DRAG_SELECT_CHECK,
                    code == 0 and bool(picked),
                    f"exit {code}; selections seen {selections[-3:]}; {_first_line(out)}",
                )
        _close_probe(drag_probe, drag_hwnd)

        # ── the third tier: a UI drawn in pixels ───────────────────────────
        if importlib.util.find_spec("rapidocr_onnxruntime") is None:
            checks.skip(CANVAS_CHECK, "rapidocr is not installed")
        else:
            canvas_log = work / "canvas.jsonl"
            canvas = subprocess.Popen(
                [sys.executable, _probe_script("canvas_probe2.py"), "--log", str(canvas_log)]
            )
            try:
                code, out = _call("windows", "--include", "all", data=data)
                match = re.search(r"hwnd=(\d+).*'athand canvas probe'", out)
                if match is None:
                    checks.check(
                        CANVAS_CHECK,
                        False,
                        "the Tk probe did not show up in windows --include all",
                    )
                else:
                    canvas_hwnd = int(match.group(1))
                    code, out = _call("targets", "--hwnd", canvas_hwnd, data=data)
                    record = _read_json(_listing_path(canvas_hwnd))
                    visual = [
                        item
                        for item in record.get("targets") or []
                        if item.get("source") == "visual"
                    ]
                    checks.check(
                        CANVAS_CHECK,
                        code == 0 and bool(visual),
                        f"exit {code}; {len(visual)} candidate(s) cut out of the pixels",
                    )
            finally:
                canvas.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    canvas.wait(timeout=5)

        return _selftest_summary(checks, work, keep)
    finally:
        for proc, hwnd in probes:
            _close_probe(proc, hwnd)
        release_all_keys()
        release_all_buttons()
        if not keep:
            with contextlib.suppress(OSError):
                shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command == "release":
        print(_release_everything())
        return 0
    if ns.command == "selftest":
        return _selftest(keep=bool(ns.keep))
    args = _args_from_namespace(ns)
    args["action"] = "double_click" if ns.command == "double-click" else ns.command
    try:
        result = run_action(args)
    except Exception as exc:  # a crash must not look like a gesture that happened
        release_all_keys()
        release_all_buttons()
        print(f"ERROR: {type(exc).__name__}: {exc}")
        raise
    try:
        print(result)
    except OSError:
        # The reader went away (`athand.py windows | head`): Windows raises EINVAL rather than
        # BrokenPipeError, and there is nowhere left to report anything anyway.
        return 0
    return 2 if result.startswith(("ERROR:", "ESCALATED:")) else 0


if __name__ == "__main__":
    sys.exit(main())
