"""Verification target for the drag gesture: the application's own record of the path.

Every press / move / release it receives is appended to its JSONL log with both the client
point and the screen point, the Edit's selection (EM_GETSEL) is read back on release, and a
shell file drop (WM_DROPFILES, which only arrives if a real OLE drag-and-drop ran) logs the
paths it was handed.

    python probes/drag_probe.py --log %TEMP%\\athand-drag.jsonl --hwnd-file %TEMP%\\athand-drag.hwnd
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes as wt

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)
shell = ctypes.WinDLL("shell32", use_last_error=True)


def _arg(name: str, default: str) -> str:
    """`--log PATH` / `--hwnd-file PATH`, or the default under %TEMP%."""
    argv = sys.argv[1:]
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    return default


_TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
LOG = _arg("--log", os.path.join(_TEMP, "athand-drag.jsonl"))
HWND_FILE = _arg("--hwnd-file", os.path.join(_TEMP, "athand-drag.hwnd"))
CLASS = "AthandDragProbe"
EDIT, ZONE = 101, 102
state = {"down": 0, "move": 0, "up": 0}

u.CreateWindowExW.restype = wt.HWND
u.CreateWindowExW.argtypes = [
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.HWND,
    wt.HMENU,
    wt.HINSTANCE,
    ctypes.c_void_p,
]
u.GetDlgItem.restype = wt.HWND
u.GetDlgItem.argtypes = [wt.HWND, ctypes.c_int]
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
u.SetCapture.argtypes = [wt.HWND]
u.SetCapture.restype = wt.HWND
u.ReleaseCapture.argtypes = []
u.ReleaseCapture.restype = wt.BOOL
u.GetCapture.argtypes = []
u.GetCapture.restype = wt.HWND
u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
u.GetCursorPos.restype = wt.BOOL
u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
k.GetModuleHandleW.restype = wt.HMODULE
k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
shell.DragAcceptFiles.argtypes = [wt.HWND, wt.BOOL]
shell.DragQueryFileW.argtypes = [wt.HANDLE, ctypes.c_uint, wt.LPWSTR, ctypes.c_uint]
shell.DragQueryFileW.restype = ctypes.c_uint
shell.DragFinish.argtypes = [wt.HANDLE]


def log(event: dict) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def where() -> list[int]:
    point = wt.POINT()
    u.GetCursorPos(ctypes.byref(point))
    return [point.x, point.y]


def selection(hwnd) -> list[int]:
    edit = u.GetDlgItem(hwnd, EDIT)
    start, end = wt.DWORD(0), wt.DWORD(0)
    u.SendMessageW(
        edit,
        0x00B0,  # EM_GETSEL
        ctypes.cast(ctypes.byref(start), ctypes.c_void_p).value,
        ctypes.cast(ctypes.byref(end), ctypes.c_void_p).value,
    )
    return [start.value, end.value]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)


def wndproc(hwnd, msg, wparam, lparam):
    client = [lparam & 0xFFFF, (lparam >> 16) & 0xFFFF]
    if msg == 0x0113:  # WM_TIMER: the Edit takes its own press, so its selection is polled
        now = selection(hwnd)
        if now != state.get("selection"):
            state["selection"] = now
            log({"ev": "selection", "sel": now})
    elif msg == 0x0201:  # WM_LBUTTONDOWN
        state["down"] += 1
        state["move"] = 0
        u.SetCapture(hwnd)
        log({"ev": "down", "client": client, "screen": where(), "t": round(time.time(), 3)})
    elif msg == 0x0200:  # WM_MOUSEMOVE
        if u.GetCapture() == hwnd:
            state["move"] += 1
            log({"ev": "move", "client": client, "screen": where()})
    elif msg == 0x0202:  # WM_LBUTTONUP
        state["up"] += 1
        u.ReleaseCapture()
        log(
            {
                "ev": "up",
                "client": client,
                "screen": where(),
                "moves_this_gesture": state["move"],
                "selection": selection(hwnd),
            }
        )
    elif msg == 0x0233:  # WM_DROPFILES: only a real OLE drag-and-drop gets here
        count = shell.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
        buf = ctypes.create_unicode_buffer(600)
        names = []
        for index in range(count):
            shell.DragQueryFileW(wparam, index, buf, 600)
            names.append(buf.value)
        shell.DragFinish(wparam)
        log({"ev": "dropfiles", "count": count, "names": names})
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
        0, CLASS, "athand drag", 0x00CF0000, 240, 160, 900, 650, None, None, instance, None
    )
    if not hwnd:
        print("CreateWindow failed", ctypes.get_last_error())
        return 1
    child = 0x40000000 | 0x10000000
    u.CreateWindowExW(
        0,
        "Edit",
        "abcdefghijklmnopqrstuvwxyz0123456789 the quick brown fox jumps over the lazy dog",
        child | 0x00800000,
        20,
        20,
        820,
        26,
        hwnd,
        EDIT,
        instance,
        None,
    )
    # A Static in the open client area: it hit-tests as transparent, so a press there
    # belongs to the window itself and the whole path arrives here.
    u.CreateWindowExW(
        0, "Static", "拖拽区", child | 0x00000001, 270, 380, 360, 140, hwnd, ZONE, instance, None
    )
    shell.DragAcceptFiles(hwnd, True)
    u.SetTimer.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    u.SetTimer(hwnd, 1, 150, None)
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
