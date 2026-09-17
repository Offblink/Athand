"""Verification target for athand.py: a plain Win32 window that records what reached it.

An Edit (a11y ValuePattern), a Button, a Static showing the click count, and a ListBox with
60 items (ScrollItemPattern). Every event it receives is appended to its JSONL log — the
application's own account, which is the only truth a click test can trust, and the account
the selftest checks instead of believing the tool's own summary.

    python probes/target.py --log %TEMP%\\athand-target.jsonl --hwnd-file %TEMP%\\athand-target.hwnd
"""

import contextlib
import ctypes
import json
import os
import sys
from ctypes import wintypes as wt

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)


def _arg(name: str, default: str) -> str:
    """`--log PATH` / `--hwnd-file PATH`, or the default under %TEMP%: the selftest points
    both at its own work directory so a run leaves nothing of its own behind."""
    argv = sys.argv[1:]
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    return default


_TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
LOG = _arg("--log", os.path.join(_TEMP, "athand-target.jsonl"))
HWND_FILE = _arg("--hwnd-file", os.path.join(_TEMP, "athand-target.hwnd"))
CLASS = "AthandTargetProbe"
EDIT, BUTTON, STATIC, LIST = 101, 102, 103, 104
count = {"clicks": 0}

if "--dpi-aware" in sys.argv:
    # Off by default: a DPI-unaware probe is the common real-world target, and it is what
    # exercises the tool's "unaware and behind another window" refusal. With no foreground
    # window to be raised to (a locked session) that probe cannot be captured at all, so the
    # selftest asks for an aware one instead — its pixels come from PrintWindow either way.
    with contextlib.suppress(Exception):
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2

u.CreateWindowExW.restype = wt.HWND
u.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
]
u.GetDlgItem.restype = wt.HWND
u.GetDlgItem.argtypes = [wt.HWND, ctypes.c_int]
u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u.GetWindowTextLengthW.argtypes = [wt.HWND]
u.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
u.SetWindowTextW.restype = wt.BOOL
u.SendMessageW.restype = wt.LPARAM
u.SendMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
u.DefWindowProcW.restype = ctypes.c_longlong
u.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
u.RegisterClassW.restype = wt.ATOM
u.RegisterClassW.argtypes = [ctypes.c_void_p]
u.DestroyWindow.argtypes = [wt.HWND]
u.PostQuitMessage.argtypes = [ctypes.c_int]
u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u.UpdateWindow.argtypes = [wt.HWND]
u.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, ctypes.c_uint, ctypes.c_uint]
u.TranslateMessage.argtypes = [ctypes.c_void_p]
u.DispatchMessageW.argtypes = [ctypes.c_void_p]
k.GetModuleHandleW.restype = wt.HMODULE
k.GetModuleHandleW.argtypes = [wt.LPCWSTR]


def log(event: dict) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_edit(hwnd) -> str:
    """The Edit's text as *this* process reads it.

    A reader outside the process cannot get this: `GetWindowText` on another process's control
    hands back whatever title it was last told, so a test that asks from outside sees stale text
    while the typing worked (measured 2026-09-17 — two red rows against a working tool). The
    application's own account is the only one worth checking against.
    """
    edit = u.GetDlgItem(hwnd, EDIT)
    length = u.GetWindowTextLengthW(edit)
    buf = ctypes.create_unicode_buffer(length + 1)
    u.GetWindowTextW(edit, buf, length + 1)
    return buf.value


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)


def wndproc(hwnd, msg, wparam, lparam):
    if msg == 0x0111:  # WM_COMMAND
        cid, code = wparam & 0xFFFF, (wparam >> 16) & 0xFFFF
        log({"ev": "command", "id": cid, "code": code})
        if cid == EDIT and code == 0x0300:  # EN_CHANGE: what the control now holds
            log({"ev": "text", "value": read_edit(hwnd)})
        if cid == BUTTON and code == 0:  # BN_CLICKED
            count["clicks"] += 1
            u.SetWindowTextW(u.GetDlgItem(hwnd, STATIC), f"clicks={count['clicks']}")
            u.SetWindowTextW(hwnd, f"athand target clicks={count['clicks']}")
    elif msg == 0x0201:  # WM_LBUTTONDOWN
        log({"ev": "lbuttondown", "x": lparam & 0xFFFF, "y": (lparam >> 16) & 0xFFFF})
    elif msg == 0x0010:  # WM_CLOSE
        u.DestroyWindow(hwnd)
    elif msg == 0x0002:  # WM_DESTROY
        u.PostQuitMessage(0)
    return u.DefWindowProcW(hwnd, msg, wparam, lparam)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


def main() -> int:
    open(LOG, "w", encoding="utf-8").close()
    instance = k.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.hInstance = instance
    wc.lpszClassName = CLASS
    wc.hbrBackground = 6  # COLOR_WINDOW + 1
    if not u.RegisterClassW(ctypes.byref(wc)):
        print("RegisterClass failed", ctypes.get_last_error())
        return 1
    hwnd = u.CreateWindowExW(
        0, CLASS, "athand target clicks=0", 0x00CF0000,
        300, 200, 520, 460, None, None, instance, None,
    )
    if not hwnd:
        print("CreateWindow failed", ctypes.get_last_error())
        return 1
    child = 0x40000000 | 0x10000000
    u.CreateWindowExW(0, "Edit", "初始文本", child | 0x00800000, 20, 20, 320, 26, hwnd, EDIT, instance, None)
    u.CreateWindowExW(0, "Button", "按下", child, 360, 20, 120, 26, hwnd, BUTTON, instance, None)
    u.CreateWindowExW(0, "Static", "clicks=0", child, 20, 56, 200, 24, hwnd, STATIC, instance, None)
    listbox = u.CreateWindowExW(
        0, "ListBox", "", child | 0x00800000 | 0x00200000,
        20, 90, 460, 320, hwnd, LIST, instance, None,
    )
    for i in range(1, 61):
        item = ctypes.c_wchar_p(f"项目 {i:02d}")  # keep alive across the call
        u.SendMessageW(listbox, 0x0180, 0, ctypes.cast(item, ctypes.c_void_p).value)  # LB_ADDSTRING
    u.ShowWindow(hwnd, 5)  # SW_SHOW
    u.UpdateWindow(hwnd)
    with open(HWND_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(hwnd))
    log({"ev": "started", "hwnd": hwnd})
    msg = wt.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))
    log({"ev": "exited"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
