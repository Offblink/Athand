"""Check the decider seam without a model: a stub decider, the bundled probe window, and the CLI.

The seam (`--intent`) is the one part of athand that can talk to a model — and no model is needed
to check any of it. What has to hold is that a *configured* decider is asked the right question,
that the answer becomes the number athand acts on, and that every way it can say "no" arrives
**before** anything is injected. So this file is both halves of the test: the stub that answers the
same way every time, and the driver that checks the rows.

```bash
python skills/athand/probes/decider_check.py            # ~5min; injects real clicks on the probe only
python skills/athand/probes/decider_check.py --keep     # keep the temp dir (requests, listings, PNGs)
python skills/athand/probes/decider_check.py --stub --label 按下 --dump req.json   # the double itself
```

Five minutes is athand's own cost, not the seam's: each row starts a fresh process, and a row that
clicks scans the window, injects, and captures the verifying frame twice.

The rows (`ATHAND_DECIDER` is set per row; nothing here reads `decider.json`):

1. nothing configured — the gesture is refused, and nothing is injected
2. the weights named by the config are not on this disk — same
3. weights present but nothing answers at the url — same
4. a malformed config is reported with its source, not swallowed — same
5. the process transport: the stub picks the button by label, athand clicks it, the probe logs it,
   and the *request* it received matches the contract (labels, boxes in image pixels, an image)
6. `UNDECIDED` is a hand-off (exit 2, the picture named, nothing injected)
7. an `id` that is not in the option list is not clicked either
8. an ambiguous `--name` with an `--intent` is decided by the decider, not by taking the first

Rows 5 and 8 click, so they need the foreground (and an unlocked session): the probe is raised first
and if it cannot hold the foreground those two rows report `SKIP` with the reason, never a pass.
The decider's own lifecycle (`--start`/`--stop`, the weights actually loading) is *not* here: it
needs a real model on the box and was measured by hand — see `NOTES.md`.
"""
import argparse
import contextlib
import ctypes
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "athand.py"
PROBE = HERE / "target.py"
MAIN_WINDOW = 0  # set once the probe is up; `_stub_argv` needs no window
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


# ── the double: a decider that always answers the same way ─────────────────
def stub() -> int:
    """`ask`'s transport, by hand: one request on stdin, one answer on stdout."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--mode", default="pick", choices=("pick", "undecided", "bogus"))
    ap.add_argument("--label", default="", help="pick the option with this label")
    ap.add_argument("--index", type=int, default=1, help="…or this 1-based position")
    ap.add_argument("--dump", default="", help="write the request received here")
    ap.add_argument("--p", type=float, default=0.99)
    args = ap.parse_args()
    request = json.loads(sys.stdin.read())
    if args.dump:
        pathlib.Path(args.dump).write_text(json.dumps(request, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    options = request["options"]

    def answer(index: int, decision: str = "YES") -> dict:
        return {
            "contract": 1, "decision": decision, "id": options[index - 1]["id"], "index": index,
            "p": args.p, "confidence": 0.98, "threshold": 0.5, "policy": "default",
            "marked": request.get("image", ""),
            "options": [{"index": i, "id": o["id"], "label": o["label"],
                         "p": args.p if i == index else 0.01}
                        for i, o in enumerate(options, 1)],
        }

    if args.mode == "undecided":
        out = answer(1, "UNDECIDED")
    elif args.mode == "bogus":
        out = answer(1)
        out["id"] = 999999
    else:
        picked = next((i for i, o in enumerate(options, 1) if o["label"] == args.label), None) \
            if args.label else args.index
        out = answer(picked or 1, "YES" if picked else "UNDECIDED")
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


def stub_argv(*extra: str) -> list[str]:
    return [sys.executable, str(pathlib.Path(__file__).resolve()), "--stub", *extra]


# ── the driver ─────────────────────────────────────────────────────────────
class Checks:
    """Three states: a check that could not run is not a check that passed."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, "PASS" if ok else "FAIL", detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    def skip(self, name: str, why: str) -> None:
        self.rows.append((name, "SKIP", why))
        print(f"  SKIP  {name} — {why}")

    def failed(self) -> list[str]:
        return [name for name, state, _ in self.rows if state == "FAIL"]


def foreground() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def input_desktop() -> str:
    handle = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
    buffer = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetUserObjectInformationW(handle, 2, buffer, 128, None)
    return buffer.value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="athand-decider-check-"))
    data = work / "data"
    data.mkdir()
    log = work / "target.jsonl"
    hwnd_file = work / "target.hwnd"
    dump = work / "request.json"
    run_env = {**ENV, "ATHAND_DIR": str(data)}

    def athand(*argv: str, env: dict | None = None) -> tuple[int, str]:
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True,
                              encoding="utf-8", env={**run_env, **(env or {})}, timeout=600)
        return done.returncode, (done.stdout or "").strip()

    def cfg(**extra) -> str:
        return json.dumps({"weights": str(SCRIPT.parent), **extra}, ensure_ascii=False)

    def clicks() -> int:
        if not log.exists():
            return 0
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        return sum(1 for row in rows if row == {"ev": "command", "id": 102, "code": 0})

    checks = Checks()
    probe = subprocess.Popen([sys.executable, str(PROBE), "--log", str(log),
                              "--hwnd-file", str(hwnd_file)])
    hwnd = 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if hwnd_file.exists() and hwnd_file.read_text(encoding="utf-8").strip():
            hwnd = int(hwnd_file.read_text(encoding="utf-8").strip())
            break
        if probe.poll() is not None:
            print(f"the probe exited with {probe.returncode}")
            return 2
        time.sleep(0.1)
    if not hwnd:
        print("no probe window after 20s")
        return 2
    time.sleep(0.8)
    print(f"probe window 0x{hwnd:X}\n")
    try:
        athand("targets", "--hwnd", str(hwnd))
        before = clicks()

        print("[1] nothing configured")
        rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                         env={"ATHAND_DECIDER": "{}"})
        checks.check("the gesture is refused (exit 2)", rc == 2, f"rc={rc}; {out[:90]}")
        checks.check("it says no decider is configured",
                     out.startswith("ERROR: no decider is configured"), out[:90])
        checks.check("nothing was injected", clicks() == before, f"{clicks()} clicks")

        print("[2] the weights are not on this disk")
        rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                         env={"ATHAND_DECIDER": cfg(url="http://127.0.0.1:8199",
                                                    weights=r"C:\no\model")})
        checks.check("the gesture is refused (exit 2)", rc == 2, f"rc={rc}")
        checks.check("it names the path it looked for", "not on this disk" in out, out[:100])
        checks.check("nothing was injected", clicks() == before, f"{clicks()} clicks")

        print("[3] weights present, nothing answers at the url")
        rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                         env={"ATHAND_DECIDER": cfg(url="http://127.0.0.1:8199")})
        checks.check("the gesture is refused (exit 2)", rc == 2, f"rc={rc}")
        checks.check("it names the address", "nothing answers at http://127.0.0.1:8199" in out,
                     out[:100])
        checks.check("nothing was injected", clicks() == before, f"{clicks()} clicks")

        print("[4] a malformed config")
        rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                         env={"ATHAND_DECIDER": "{not json"})
        checks.check("the gesture is refused (exit 2)", rc == 2, f"rc={rc}")
        checks.check("it says which config is broken", "not valid JSON" in out, out[:100])

        holdable = input_desktop() == "Default"
        if holdable:
            athand("restore", "--hwnd", str(hwnd))
            holdable = foreground() == hwnd
        clicking = "the desktop is locked" if input_desktop() != "Default" else \
            "the probe cannot hold the foreground: the machine is in use"

        print("[5] the process transport: the stub picks the button by label")
        if not holdable:
            checks.skip("a click by intent reaches the control", clicking)
        else:
            rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                             env={"ATHAND_DECIDER": cfg(
                                 ask=stub_argv("--label", "按下", "--dump", str(dump)), k=9)})
            checks.check("the gesture ran (exit 0)", rc == 0, f"rc={rc}; {out[:100]}")
            checks.check("it names the button the stub picked", "'按下'" in out, out[:110])
            checks.check("the result carries the provenance", "via decider p=0.99" in out, out[:120])
            checks.check("the click reached the app", clicks() == before + 1, f"{clicks()} clicks")
            request = json.loads(dump.read_text(encoding="utf-8")) if dump.exists() else {}
            options = request.get("options") or []
            checks.check("the request carries the intent", request.get("intent") == "点击按下按钮",
                         request.get("intent", "(none)"))
            checks.check("the request carries a picture", pathlib.Path(
                request.get("image", "")).is_file(), request.get("image", ""))
            checks.check("2-9 options, each with id/label/box", 2 <= len(options) <= 9 and all(
                set(o) == {"id", "label", "box"} for o in options), f"{len(options)} options")
            boxes = [o.get("box") or [0, 0, 0, 0] for o in options]
            checks.check("boxes are in image coordinates",
                         all(len(b) == 4 and 0 <= b[0] < b[2] and 0 <= b[1] < b[3]
                             for b in boxes), json.dumps(boxes[:2]))

        print("[6] the decider is not sure")
        before = clicks()
        rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                         env={"ATHAND_DECIDER": cfg(ask=stub_argv("--mode", "undecided"))})
        checks.check("a hand-off, not a click (exit 2)", rc == 2, f"rc={rc}; {out[:90]}")
        checks.check("it says UNDECIDED", out.startswith("UNDECIDED:"), out[:90])
        checks.check("it hands over the picture it looked at", ".marked" in out or ".png" in out,
                     out[:140])
        checks.check("nothing was injected", clicks() == before, f"{clicks()} clicks")

        print("[7] an id that is not in the option list")
        rc, out = athand("click", "--hwnd", str(hwnd), "--intent", "点击按下按钮",
                         env={"ATHAND_DECIDER": cfg(ask=stub_argv("--mode", "bogus"))})
        checks.check("not clicked either (exit 2)", rc == 2, f"rc={rc}; {out[:90]}")
        checks.check("nothing was injected", clicks() == before, f"{clicks()} clicks")

        print("[8] an ambiguous --name, decided by the decider")
        listing = json.loads((data / f"{hwnd}.json").read_text(encoding="utf-8"))
        matches = [t for t in listing["targets"] if (t.get("name") or "") == "系统"]
        if len(matches) < 2:
            checks.skip("an ambiguous name is resolved, not guessed",
                        f"only {len(matches)} candidate named 系统 in this window")
        else:
            rc, out = athand("click", "--hwnd", str(hwnd), "--name", "系统",
                             "--intent", "打开这个窗口左上角的系统菜单",
                             env={"ATHAND_DECIDER": cfg(ask=stub_argv("--label", "系统"))})
            checks.check("the gesture ran (exit 0)", rc == 0, f"rc={rc}; {out[:100]}")
            checks.check("it picked one of the matches", "'系统'" in out, out[:100])
            checks.check("the provenance is in the result", "via decider" in out, out[:120])

        failures = checks.failed()
        total = len([row for row in checks.rows if row[1] != "SKIP"])
        print(f"\ndecider-check: {total - len(failures)}/{total} passed, exit "
              f"{1 if failures else 0}")
        return 1 if failures else 0
    finally:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.4)
        if probe.poll() is None:
            probe.terminate()
        if args.keep:
            print(f"kept {work}")
        else:
            with contextlib.suppress(OSError):
                import shutil
                shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if "--stub" in sys.argv:
        sys.exit(stub())
    sys.exit(main())
