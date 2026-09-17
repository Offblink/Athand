# Athand

**触手可及** —— 给 agent 一双直接操作本机桌面的手：一个脚本、几个子命令，没有 daemon、没有 MCP、
没有要装的服务，clone 下来就能调。

*Look at a Windows machine's desktop and act on it. One script, a few subcommands, no daemon, no MCP
server, no package to install.*

`athand.py` 是 [Fungi](https://github.com/CN-Fungi/Fungi) 的桌控工具，从原来承载它的 agent 里切出来：
机制逐字搬运（每一处 `ctypes` 声明、每一个阈值、每一条实测注释），所以设计记录留在那个仓库的
`docs/spec.md` §35–§46，这里的注释用 `spec §NN` 引用。**给 agent 读的说明书是
[`skills/athand/SKILL.md`](skills/athand/SKILL.md)**（流程、两条规则、16 条实测坑），那份是权威；
本文件是给人看的门面。仅 Windows。

```bash
pip install -r requirements.txt      # pillow / comtypes / numpy / rapidocr-onnxruntime
python skills/athand/athand.py windows
```

## 三步：`windows → targets → act`

```bash
python skills/athand/athand.py windows                   # 窗口表：hwnd(十进制+十六进制)、尺寸、进程、标题
python skills/athand/athand.py targets --hwnd 4653616    # 这个窗口的控件编号 + 画了号码的 PNG
python skills/athand/athand.py click   --hwnd 4653616 --target 3
python skills/athand/athand.py type    --hwnd 4653616 --name "搜索" --text "你好"
python skills/athand/athand.py drag    --hwnd 4653616 --target 7 --dx 300
python skills/athand/athand.py restore --hwnd 4653616    # 从最小化/托盘里叫回来（走应用自己的门）
```

每个动作都会打印它**验证到什么**，外加一张 `[frame: <path>]` —— 去看那个路径，再判断成没成。
`hwnd` 是身份，标题不是（标题会变）。

| 命令 | 干什么 |
|---|---|
| `windows [--include visible\|all]` | 窗口表（`all` 加上最小化/隐藏窗口、通知区、桌面） |
| `shot [--hwnd N]` | 整屏或单窗口的 PNG，打印路径 |
| `targets --hwnd N` | 给这个窗口里的控件编号，并把号码画在图上 |
| `label --hwnd N --target K --label 文字` | 给没有文字的形状起个名，之后 `--name 文字` 找得到 |
| `click` / `double-click --hwnd N (--target K \| --name 文字) [--button left\|right]` | `double-click` 就是"打开"手势 |
| `drag --hwnd N … [--to-hwnd M] [--to-target K \| --dx/--dy PX] [--route taskbar]` | 按住左键搬运 |
| `type --hwnd N [--target K \| --name 文字] --text 文字 [--char-delay S]` | 逐字输入，**永不碰剪贴板** |
| `key --hwnd N --keys ctrl s` | 一个键，或一个组合键 |
| `scroll --hwnd N (--target K \| --name 文字)` | 把控件滚进视野 |
| `restore --hwnd N [--via auto\|window\|shell]` | 把最小化/托盘里的窗口叫回来 |
| `release` | 松开这台机器上还按着的一切按键与按钮（被 kill 之后用） |
| `selftest [--keep]` | 拉起探针，逐项对账（`--keep` 留下工作目录） |

退出码：`0` 执行了；`2` 被拒绝（正文以 `ERROR:` 或 `ESCALATED:` 开头，什么都没发出去）。

## 两条规则

1. **触手可及**：屏幕上够得着的东西优先。文件/程序的图标或行在屏幕上，就 `double-click` 它，别去写
   shell 命令。没在运行的窗口用 `restore` 走**应用自己的门**：任务栏按钮 → 托盘图标（含通知区溢出浮窗）
   → 桌面图标。三条门都真机验过，`ShowWindow` 只是"没有门"时的兜底——它给的是看着正常、却不吃输入的窗口，
   结果里会明说。
2. **文件的增删改移不走像素**：那是 shell 的活。像素路径要先解析成矩形，落到错误的一行就是数据事故。
   但把文件**交给某个应用**（输入框、拖放区）不是文件操作，那是 `drag`。

## 为什么是编号，不是坐标

实测 2026-09-13：模型给的坐标偏 15–68px（18px 的目标上等于抛硬币）；从候选里挑一个编号，3/3 命中。
所以每个手势只能报一个**编号**或一个**名字**，由程序自己从无障碍树（exact）、OCR、或它从画面里切出来的
形状里解析出矩形，再把编号交给你。

编号属于**那一次 listing**：`%TEMP%\athand\<hwnd>.json` 连窗口矩形和时间一起写下；窗口动过，后来的调用
**拒绝**这个编号（"run `targets` again"）。`--name` 不受窗口移动影响（它是对活的无障碍树重新匹配）。
窗口是**自绘**的（Chromium/Electron、Qt 这类，比如微信）时，图就是工具面：把文字从实拍帧上读出来，
按编号挑切出来的形状。OCR 出来的名字是近似的（`drag_source.txt` 会读成 `drag_source. txt`），编号不是。

## 切出来时改了什么

- **脚本没有记忆**，所以一次调用产出的两样东西落盘到 `%TEMP%\athand`（`ATHAND_DIR` 可覆盖）：窗口的编号
  清单（`<hwnd>.json`，外加 `<hwnd>.labels.json`）和图片（`*.png`）。下一次调用把清单读回来。
- **脚本没有人可问**：Fungi 里三次徒劳之后会问用户，这里打印 `ESCALATED: …`（exit 2）就停手。没有权限
  提示——**运行它就是同意**。
- 坐标依旧轮不到你给（见上）。

## 布局

```
skills/athand/
  SKILL.md      给 agent 的说明书（流程、规则、坑）
  athand.py     整个工具：windows targets shot label click double-click drag
                type key scroll restore release selftest
  probes/
    target.py               Win32 窗口（Edit / Button / Static / 60 项 ListBox），收到的每条消息都记进
                            JSONL —— selftest 拿它自己的记录对账 —— 并且能穿上"门"所依赖的形态：
                            --tool-window（无任务栏按钮）、--tray（通知区图标）、--hidden（不显示窗口）
    drag_probe.py           SetCapture 路径日志 + EM_GETSEL + WM_DROPFILES
    canvas_probe2.py        一个 Tk canvas：两个控件画在像素里，a11y 里都没有
    desktop_door_check.py   第三条门，单跑：桌面上建一个快捷方式 + `win d`
    drag_probe.sample.jsonl, drag_source.txt   一条录下来的拖动路径，和一个用来拖的文件
```

## 安装

```bash
pip install -r requirements.txt      # pillow, comtypes, numpy, rapidocr-onnxruntime
```

`rapidocr-onnxruntime` 是"读自绘界面上的字"那一层。它是默认依赖，但代码仍当它可选（`find_spec`）：
一个没装它的环境会**少掉 OCR 那一档并说出来**，而不是 import 就崩。

## 验证

```bash
python skills/athand/athand.py selftest [--keep]
```

它拉起探针，逐项对着探针**自己记的账**检查每个手势：按钮发出的 `WM_COMMAND`、编辑框报回的 `EM_GETSEL`、
OS 给回的窗口矩形、应用自己被点了托盘图标时打的 `trayclick`。跑不了的检查报 `SKIP` 并打印原因
（"机器锁着"、"探针拿不到前台"，就是那两类），绝不瞒成通过。

第三条门——桌面图标——单独一个检查，因为它要动你的桌面（建一个快捷方式、必要时 `win d`、然后把窗口还回来）：

```bash
python skills/athand/probes/desktop_door_check.py     # 约 25 秒，退出时无残留
```

基线（2026-09-17，Windows 11 26200）：`selftest` **18/18、exit 0、无 SKIP**；
`desktop_door_check` **6/6、无残留**；锁屏时跑，只读项 6/6、注入项 `SKIP` 并打印原因。
真机用例：驱动一个 **Qt 自绘**的聊天客户端（a11y 里什么都没有）——托盘图标把窗口叫回来 → 按 OCR 编号
点开一个联系人 → 把桌面上的一个文件拖进输入框 → 逐字打 140 个字（0.15 s/字）→ 点发送；每一步都读回
实拍帧确认。

## 边界

- **只有 Windows**。架构能移植，实测数据不能——那些阈值都是这台机器上量出来的。
- **提权窗口（UAC 安全桌面）够不着**：UIPI 是硬边界，不是缺口。工具会如实报，别硬试。
- **锁屏时什么都注入不了**：输入桌面是 `Screen-saver` 时 `SendInput` 全部失败；`windows`/`targets`/
  窗口级 `shot` 照常，工具报的是机器状态而不是"工具坏了"。
- **读它的输出走管道时设 `PYTHONIOENCODING=utf-8`**：脚本自己不选编码，Windows 子进程 stdout 不是控制台时
  按系统代码页写（本机 `gbk`），UTF-8 的读者会看到乱码。
- **`type` / `key` 要求目标窗口真在前台**，否则拒绝：字和键只跟焦点走，不跟窗口走。

## License

MIT —— 见 [LICENSE](LICENSE)。
