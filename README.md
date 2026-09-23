# Athand

**触手可及** —— 给 agent 一双直接操作本机桌面的手：一个脚本、几个子命令，没有 daemon、没有 MCP、
没有要装的服务，clone 下来就能调。

*Look at a Windows machine's desktop and act on it. One script, a few subcommands, no daemon, no MCP
server, no package to install.*

仅 Windows。**给 agent 读的说明书是 [`skills/athand/SKILL.md`](skills/athand/SKILL.md)**（流程、
两条规则、18 条实测坑），那份是权威；本文件是给人看的门面。

## 它解决的是哪件事

屏幕上该点哪个东西 —— 这件事对程序来说一直很难。

一个窗口里往往有几十个能点的东西：按钮、菜单项、列表行、图标。要知道"那个"在哪儿，得先知道它长什么样；
要让程序去点，还得给它一个坐标 —— 而模型给的坐标偏 15–68 px（实测，见下），在 18 px 的目标上等于抛硬币。

athand 把它变成**选择题**：程序把能点的东西**编号**，调用方只需要说"点 8 号"。

## 用起来长这样

三步：`windows`（有哪些窗口）→ `targets`（这个窗口里有哪些能点的）→ `click` / `type` / `drag`（动手）。

```bash
pip install -r requirements.txt                          # pillow / comtypes / numpy / rapidocr-onnxruntime
python skills/athand/athand.py windows                   # 这台机器上有哪些窗口
python skills/athand/athand.py targets --hwnd 4653616    # 这个窗口的控件编号 + 画了号码的 PNG
python skills/athand/athand.py click   --hwnd 4653616 --target 3
python skills/athand/athand.py type    --hwnd 4653616 --name "搜索" --text "你好"
python skills/athand/athand.py drag    --hwnd 4653616 --target 7 --dx 300
python skills/athand/athand.py restore --hwnd 4653616    # 从最小化/托盘里叫回来（走应用自己的门）
```

`4653616` 是**那台机器上**那个窗口的编号（`hwnd`，系统给的窗口句柄），在你机器上是别的数字 ——
先跑 `windows` 拿你自己的。认窗口靠这个编号，**不靠标题**（标题会变）。

`windows` 打出来是这样：

```
WINDOWS (9 application windows; hwnd is the identity, titles are not)
  hwnd=17828256 0x11009A0 780x690 python.exe 'athand target clicks=0' ← foreground
```

（编号同时给十进制和十六进制，两种写法后面都能用；`← foreground` 是说现在前台是它。）

`targets` 打出来是这样 —— **左边的 `#8` 就是编号，右边的字是它读到的名字**：

```
TARGETS in hwnd=0x11009A0 'athand target clicks=0' — pick one by number
#5 [text] '系统' cls=- rect=(462, 312, 495, 345) centre=(478, 328)
#6 [Invoke+ExpandCollapse] '系统' cls=- rect=(462, 312, 495, 345) centre=(478, 328)
#7 [Value] 'Edit' cls=Edit rect=(492, 377, 972, 416) centre=(732, 396)
#8 [Invoke] '按下' cls=Button rect=(1002, 377, 1182, 416) centre=(1092, 396)
#9 [text] 'clicks=0' cls=Static rect=(492, 431, 792, 467) centre=(642, 449)
#10 [text] 'clicks=0' cls=ListBox rect=(492, 482, 1182, 941) centre=(837, 711)
#11 [Invoke+Select+ScrollItem] '项目 01' cls=- rect=(494, 483, 1156, 507) centre=(825, 495)
…
[listing: %TEMP%\athand\17828256.json]
[numbered frame: %TEMP%\athand\targets-0x11009A0-20260923-131743-22e8.png]
```

（这段是把仓库里的探针窗口 `probes/target.py` 拉起来、对它跑一次 `targets` 的原样输出：`#5` 以前、
`#11` 以后的行略掉了，本机的临时目录写成了 `%TEMP%`。中间那列 `cls=Button` 是系统给它的类名。）

最后两行是这次调用落在盘上的两样东西：一份**清单**（编号、矩形、它的名字），和一张
**把号码画在截图上的图** —— 图是给人（和视觉模型）看的，清单是给程序用的。
于是 `click --target 8` 的意思就是"点清单里的 8 号"：**位置由程序算，不由调用方给**。

还是那个窗口，`click --target 8` 打出来是这样（同一个会话，逐字）：

```
CLICK #8 [Invoke] '按下' cls=Button rect=(1002, 377, 1182, 416) centre=(1092, 396) at (1092, 396) in hwnd=0x11009A0 (injected)
  verify: focus matched → verified
  control: athand script — no per-action prompt (running it is the consent)
[frame: %TEMP%\athand\frame-20260923-131744-9558.png (780x690 PNG) — read that path to look at it]
```

那个 `(injected)` 是说它**真的动了光标**（不是悄悄调用控件的接口）；`verify:` 是它对自己的判决 ——
`verified`、`unverified`（没验到就不说成功），或者干脆拒绝（正文以 `ERROR:` / `UNDECIDED:` /
`ESCALATED:` 开头、exit 2，什么都没发出去）。

**去看那张 frame，再决定信不信它。** 这是这个工具的基本立场：每个动作都在画面上真实发生过，
看得见、可核对。

## 全部命令

| 命令 | 干什么 |
|---|---|
| `windows [--include visible\|all]` | 窗口表（`all` 加上最小化/隐藏窗口、通知区、桌面） |
| `shot [--hwnd N]` | 整屏或单窗口的 PNG，打印路径 |
| `decider [--start \| --stop] [--hwnd N --intent 意图]` | 这台机器可能被指到的决策服务：看状态、起停、或只问不点 |
| `targets --hwnd N` | 给这个窗口里的控件编号，并把号码画在图上 |
| `label --hwnd N --target K --label 文字` | 给没有文字的形状起个名，之后 `--name 文字` 找得到 |
| `click` / `double-click --hwnd N (--target K \| --name 文字 \| --intent 意图) [--button left\|right]` | `double-click` 就是"打开"手势 |
| `drag --hwnd N … [--to-hwnd M] [--to-target K \| --dx/--dy PX] [--route taskbar]` | 按住左键搬运 |
| `type --hwnd N [--target K \| --name 文字 \| --intent 意图] --text 文字 [--char-delay S]` | 逐字输入，**永不碰剪贴板** |
| `key --hwnd N --keys ctrl s` | 一个键，或一个组合键 |
| `scroll --hwnd N (--target K \| --name 文字 \| --intent 意图)` | 把控件滚进视野 |
| `restore --hwnd N [--via auto\|window\|shell]` | 把最小化/托盘里的窗口叫回来 |
| `release` | 松开这台机器上还按着的一切按键与按钮（被 kill 之后用） |
| `selftest [--keep]` | 拉起探针，逐项对账（`--keep` 留下工作目录） |

退出码：`0` 执行了；`2` 被拒绝（正文以 `ERROR:`、`UNDECIDED:` 或 `ESCALATED:` 开头，什么都没发出去）。

## 装

```bash
pip install -r requirements.txt      # pillow, comtypes, numpy, rapidocr-onnxruntime
```

`rapidocr-onnxruntime` 那一层是"读自绘界面上的字"（下面会讲什么叫自绘）。它默认装，但代码仍当它可选：
一个没装它的环境会**少掉 OCR 这一档并说出来**，而不是 import 就崩。

## 给不出号码的时候：`--intent`

号码得有人来给。正常情况是你自己看那张编号图、挑一个 —— 这就是设计。有两种情况给不出来：
调用方读不了图，或者好几个控件叫同一个名字。`--intent "点击发送按钮"` 是给它们的：说清这个手势
**是为了干什么**，号码交给**这台机器上配好的 decider**（另一个程序提供的模型服务）去挑 ——
没配、或者它说的权重不在这台机器上，`--intent` 就被拒绝，`--target`/`--name` 照旧。
athand 自己不跑模型、不认识任何模型，也不带任何模型（口径见
[`skills/athand/SKILL.md`](skills/athand/SKILL.md) 的「`--intent`」一节）。

**参考实现**：[BiXian](https://github.com/Offblink/BiXian) —— 同一个作者的本地决策服务（4-bit 的小视觉模型，
从编号清单里挑一个号），它就是为这个接缝写的。下面只是 README 的推荐：**代码里没有任何模型的名字**，
接缝认的只是 `decider.json` 里那几个字段，换谁来实现都行。两者之间只有 HTTP 和一条启动命令，
没有 import，也没有共享环境——模型那一侧的运行时和显存完全留在对面。

在 `athand.py` 旁边放一个 `decider.json`（或者把同一份 JSON 给 `$ATHAND_DECIDER`，那份可以只作用于一次调用）：

```json
{
  "url": "http://127.0.0.1:8111",
  "serve": ["<BiXian 的 venv>/python.exe", "<BiXian>/vlm-probe/decider.py", "serve", "--port", "8111"],
  "weights": "<BiXian>/models/qwen3vl-4b",
  "k": 9
}
```

字段就这些：`url`（常驻服务的地址）、`serve`（没人应答时怎么把它起起来）、`ask`（一次性命令，每次现开）、
`weights`（**开关**：这个路径不在盘上，整个接缝就关掉，并说清它找的是哪个路径）、`k`（问几个候选）、
`timeout`、`wait`、`autostart`。阈值/策略不在这里给：decider 自己的策略随它的 `/health` 回来，
athand 每次请求都带上它。`decider.json` 里有本机路径，所以它**不在仓库里**
（`.gitignore` 着），别人 clone 下来得到的是一个没有 decider 的 athand —— 行为跟以前一模一样。
想知道这台机器现在被指到了什么：

```bash
python skills/athand/athand.py decider
```

## 两条规则

1. **触手可及**：屏幕上够得着的东西优先。文件/程序的图标或行在屏幕上，就 `double-click` 它，别去写
   shell 命令。没在运行的窗口用 `restore` 走**应用自己的门**：任务栏按钮 → 托盘图标（含通知区溢出浮窗）
   → 桌面图标。三条门都真机验过，`ShowWindow` 只是"没有门"时的兜底——它给的是看着正常、却不吃输入的窗口，
   结果里会明说。
2. **文件的增删改移不走像素**：那是 shell 的活。像素路径要先解析成矩形，落到错误的一行就是数据事故。
   但把文件**交给某个应用**（输入框、拖放区）不是文件操作，那是 `drag`。

## 为什么是编号，不是坐标

实测 2026-09-13：模型给的坐标偏 15–68px（18px 的目标上等于抛硬币）；从候选里挑一个编号，3/3 命中。

所以每个手势只能报一个**编号**或一个**名字**，由程序自己从无障碍树（Windows 给程序看的那份控件清单，
也叫 UIA）、OCR、或它从画面里切出来的形状里解析出矩形，再把编号交给你。

编号属于**那一次 listing**：`%TEMP%\athand\<hwnd>.json` 连窗口矩形和时间一起写下；窗口动过，后来的调用
**拒绝**这个编号（"run `targets` again"）。`--name` 不受窗口移动影响（它是对活的无障碍树重新匹配）。

窗口是**自绘**的（Chromium/Electron、Qt 这类，比如微信）时，系统那份控件清单里可能什么都没有，
图就是工具面：把文字从实拍帧上读出来，按编号挑切出来的形状。OCR 出来的名字是近似的
（`drag_source.txt` 会读成 `drag_source. txt`），编号不是。要往这种窗口里打字，先点一下输入框，
再 `type` 不给 `--target` —— 字是跟着焦点走的。

## 它记得什么、不记得什么

- **每次调用都是一个新进程，它没有记忆。** 一次调用产出的两样东西落盘到 `%TEMP%\athand`
  （`ATHAND_DIR` 可覆盖）：窗口的编号清单（`<hwnd>.json`，外加 `<hwnd>.labels.json`）和图片（`*.png`）。
  下一次调用把清单读回来 —— 这也是为什么窗口动过之后编号会失效。
- **它没有人可问**：做不下去的时候它是打印一行说明就停手（`ESCALATED: …`，exit 2），
  不会弹权限框、也不会等你回答。**运行它就是同意**。
- **坐标轮不到调用方给**（见上）。

## 它做不到的

- **只有 Windows**。架构能移植，实测数据不能——那些阈值都是这台机器上量出来的。
- **提权窗口（UAC 安全桌面）够不着**：UIPI 是硬边界，不是缺口。工具会如实报，别硬试。
- **锁屏时什么都注入不了**：输入桌面是 `Screen-saver` 时 `SendInput` 全部失败；`windows`/`targets`/
  窗口级 `shot` 照常，工具报的是机器状态而不是"工具坏了"。
- **读它的输出走管道时设 `PYTHONIOENCODING=utf-8`**：脚本自己不选编码，Windows 子进程 stdout 不是控制台时
  按系统代码页写（本机 `gbk`），UTF-8 的读者会看到乱码。
- **`type` / `key` 要求目标窗口真在前台**，否则拒绝：字和键只跟焦点走，不跟窗口走。
- **屏幕上的动作一律是真实注入，不走无障碍触发（UIA Invoke）**：不是没做，是不做。每个手势都动真光标、
  把目标抬到前台，并留下一张实拍帧——动作在画面上存在，看得见、可核对（`type` / `key` 因此要求目标
  真在前台）。无障碍通道能不留痕迹地"触发"控件，而动作在画面上不存在：原型里有这条路（`pcbridge.py`
  的 `a11y_invoke`），切出来时故意没带。代价的另一半：被遮挡 / 最小化 / 托盘里的目标只能走应用自己的门，
  或者拒绝。
- **它不替你监视任何东西**：无常驻进程、无屏幕差分轮询、无"变了再叫我"的回调。要等到某件事发生才动手，那件
  事得有个**接口**（应用自己的 IPC/事件、系统通知、或调用方自己的唤醒通道）；只能靠盯着屏幕看它变才能感知
  的状态，这个工具给不了 —— 那是接管系统那一层的题目。

## 怎么自证（改了它之后跑什么）

这一节说的是**怎么自证**，不是使用的前提：不跑它也能用（`windows → targets → act` 照走，每个动作自己会打印
`verify:` 和实拍帧）。它是**改动这个工具时的回归账**——仓库里没有 CI、没有单元测试，探针就是唯一的判据，
改了 `athand.py` 却不跑这几条，等于没验。

```bash
python skills/athand/athand.py selftest [--keep]
```

它拉起探针（仓库自带的一个测试窗口，`probes/target.py`），逐项对着探针**自己记的账**检查每个手势：
按钮发出的 `WM_COMMAND`、编辑框报回的 `EM_GETSEL`、OS 给回的窗口矩形、应用自己被点了托盘图标时打的
`trayclick`。跑不了的检查报 `SKIP` 并打印原因（"机器锁着"、"探针拿不到前台"，就是那两类），绝不瞒成通过。

第三条门——桌面图标——单独一个检查，因为它要动你的桌面（建一个快捷方式、必要时 `win d`、然后把窗口还回来）：

```bash
python skills/athand/probes/desktop_door_check.py     # 约 25 秒，退出时无残留
```

决策接缝（`--intent`）也有自己的一条，而且**不需要任何模型**：它自带一个每次都答同样的假 decider，
所以检查的是接缝本身——问到了什么、athand 拿哪个编号去动作、以及每一种"不行"（没配 / 权重不在 /
没人应答 / 配置坏 / `UNDECIDED` / 回来一个不在选项里的 id）是不是都发生在**注入之前**。真实点击只落在
它自己拉起的探针窗口上：

```bash
python skills/athand/probes/decider_check.py          # 约 5 分钟（每个会点击的行都是一次真实扫描+注入）
```

基线（2026-09-17，Windows 11 26200）：`selftest` **18/18、exit 0、无 SKIP**；
`desktop_door_check` **6/6、无残留**；锁屏时跑，只读项 6/6、注入项 `SKIP` 并打印原因。
`decider_check` **28/28、exit 0**（2026-09-23），`selftest` 同日 18/18（改完接缝复跑）。
真机用例：驱动一个 **Qt 自绘**的聊天客户端（a11y 里什么都没有）——托盘图标把窗口叫回来 → 按 OCR 编号
点开一个联系人 → 把桌面上的一个文件拖进输入框 → 逐字打 140 个字（0.15 s/字）→ 点发送；每一步都读回
实拍帧确认。

要改 `athand.py` 的人请先读 [`skills/athand/NOTES.md`](skills/athand/NOTES.md)：18 行判据的逐项名字、
基线怎么刷新、两条守卫与三条门的实现细节，以及只在改工具时才会咬人的那些坑（`Win+D` 会把探针一起
最小化、跨进程读 `Edit` 是假的、清理只能按窗口类名……）。

## 布局

```
skills/athand/
  SKILL.md      给 agent 的说明书（流程、规则、坑）
  NOTES.md      改这个工具时的开发/验证笔记（探针、18 行判据、基线、两条守卫、三条门、踩过的坑）
  athand.py     整个工具：windows targets shot label click double-click drag
                type key scroll restore decider release selftest
  probes/
    target.py               Win32 窗口（Edit / Button / Static / 60 项 ListBox），收到的每条消息都记进
                            JSONL —— selftest 拿它自己的记录对账 —— 并且能穿上"门"所依赖的形态：
                            --tool-window（无任务栏按钮）、--tray（通知区图标）、--hidden（不显示窗口）
    drag_probe.py           SetCapture 路径日志 + EM_GETSEL + WM_DROPFILES
    canvas_probe2.py        一个 Tk canvas：两个控件画在像素里，a11y 里都没有
    desktop_door_check.py   第三条门，单跑：桌面上建一个快捷方式 + `win d`
    decider_check.py        `--intent` 决策接缝，单跑：自带假 decider，不需要模型
    drag_probe.sample.jsonl, drag_source.txt   一条录下来的拖动路径，和一个用来拖的文件
```

## 它从哪来

`athand.py` 是 [Fungi](https://github.com/CN-Fungi/Fungi) 的桌控工具，从原来承载它的 agent 里切出来
（代码是原样搬过来的，注释也是当初一次次实测留下的；设计记录留在 Fungi 的 `docs/spec.md` §35–§46，
这里注释里的 `spec §NN` 指的就是那份）。

`Screen` 原本是 Fungi 里的一个工具。同一时期另一条路线是把整台电脑接管下来（在容器里起一个 AIOS，从系统层
控制一切）；这条路走的是反方向 —— **不接管系统，只用既有的无障碍树、OCR 和画面去操作已经在跑的应用**，
开销小得多。于是它被切出来，成为现在这个样子：一份 skill + 一个脚本。

设计立场是一路带过来的，一句话：**绝不做被动轮询，而是做主动唤醒**。所以这里没有 daemon、没有后台监视、
没有"每隔 N 秒看一眼屏幕"的循环 —— `athand.py` 只在你叫它的时候动作，且每个动作自带 `verify:` 与实拍帧。
要监视什么、什么时候叫醒 agent，是调用方（agent 自己，或者将来的系统级接口）的事，不是这个工具的活。

来龙去脉见作者的知乎：[我们用的是同一个 Astra 吗？](https://zhuanlan.zhihu.com/p/2084012946414483140)，
它在 Fungi 里的样子见 [从 Psi 到 Fungi：Agent 菌丝网络与 AIOS](https://zhuanlan.zhihu.com/p/2079625942096528933)。

和另一个同类工具 **[cua-driver](https://github.com/trycua/cua)（`trycua/cua` 的桌面驱动层，MIT）** 的对照实测
（同一台机器、同一个任务：逐字输入 / 打开桌面文件 / 托盘唤醒 / 拖文件进微信）记在
[`COMPARISON.md`](COMPARISON.md) —— 两边都做成了，差别在动作怎么发出去、拿什么验收、什么时候拒绝。

## License

MIT —— 见 [LICENSE](LICENSE)。
