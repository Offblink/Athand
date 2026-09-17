# athand

Look at this Windows machine's desktop and act on it — one script, a few subcommands, no
daemon, no MCP server, no package to install. Clone it and call it.

```bash
pip install -r requirements.txt
python skills/athand/athand.py windows
python skills/athand/athand.py targets --hwnd 4653616
python skills/athand/athand.py click   --hwnd 4653616 --target 3
```

The instruction sheet an agent should read first is
[`skills/athand/SKILL.md`](skills/athand/SKILL.md): the flow, the two rules, and the traps that
cost real time.

## What it is

`athand.py` is [Fungi](https://github.com/CN-Fungi/Fungi)'s desktop-control tool, cut out of the
agent that used to host it. The mechanics are carried over verbatim — every `ctypes` declaration,
every threshold, every measured comment — so the design record for all of it lives in that
repo's `docs/spec.md` §35–§46, which the comments here cite as `spec §NN`.

What changed in the cut:

- **A script has no memory**, so the two things one call produces are written to
  `%TEMP%\athand` (override with `ATHAND_DIR`): the numbered listing of a window
  (`<hwnd>.json`, plus `<hwnd>.labels.json`) and the pictures (`*.png`). `targets` prints the
  paths; the next call reads the listing back, and **refuses numbers from a listing whose window
  rectangle no longer matches** — those numbers belong to the picture they were cut from.
- **A script has nobody to ask.** Where Fungi escalated to its user after three fruitless
  attempts, this prints `ESCALATED: …` (exit 2) and stops. There are no permission prompts:
  running it is the consent.
- **Coordinates are still never yours to give.** Every gesture names a target number or a name
  from `targets`; the program resolves it out of the accessibility tree, OCR, or the shapes it
  cuts out of the picture. Measured 2026-09-13: model-produced coordinates miss by 15–68px,
  picking a candidate number hit 3/3.

## Layout

```
skills/athand/
  SKILL.md       the instruction sheet (flow, rules, traps)
  athand.py      the whole tool: windows targets shot label click double-click drag
                 type key scroll restore release selftest
  probes/
    target.py           Win32 window (Edit, Button, Static, 60-item ListBox) that logs
                        every message it receives — the account the selftest judges by
    drag_probe.py       SetCapture path log + EM_GETSEL + WM_DROPFILES
    canvas_probe2.py    a Tk canvas: two controls drawn in pixels, no a11y for either
    drag_probe.sample.jsonl, drag_source.txt   a recorded drag path, and a file to drag
```

## Install

```bash
pip install -r requirements.txt      # pillow, comtypes, numpy, rapidocr-onnxruntime
```

`rapidocr-onnxruntime` is what reads text off a UI that draws itself. It is a default
dependency, but the code still treats it as optional (`find_spec`): a bare install without it
loses the OCR tier and says so, rather than failing to import.

Windows only. The architecture ports; the measurements do not. An elevated window (UAC secure
desktop) stays out of reach — Windows blocks input into it, and that is the hard boundary rather
than a gap.

## Verify it

```bash
python skills/athand/athand.py selftest [--keep]
```

It starts the probes and checks each gesture against what the probe itself recorded — the
`WM_COMMAND` a button sent, the `EM_GETSEL` an edit reported, the window rectangle the OS gives
back. A check that cannot run (say, the machine is locked) is reported `SKIP` with its reason,
never as a pass.

## License

MIT — see [LICENSE](LICENSE).
