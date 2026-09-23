# Athand

**触手可及** —— 给 agent 一双直接操作本机桌面的手：一个脚本、几个子命令，没有 daemon、没有 MCP、
没有要装的服务，clone 下来就能调。

*Look at a Windows machine's desktop and act on it. One script, a few subcommands, no daemon, no MCP
server, no package to install.*

仅 Windows。**给 agent 读的说明书是 [`skills/athand/SKILL.md`](skills/athand/SKILL.md)** —— 把那份
交给你的 agent，它就知道怎么用；本文件是给人看的。

## 它解决的是哪件事

屏幕上该点哪个东西，这件事对程序来说一直很难。

一个窗口里往往有几十个能点的东西：按钮、菜单项、列表行、图标。要找到"那个"，模型给的坐标偏 15–68 px
（实测），在 18 px 的目标上等于抛硬币。

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

（这是仓库自带的一个测试窗口上的原样输出：`#5` 以前、`#11` 以后的行略掉了，本机临时目录写成了
`%TEMP%`。`cls=Button` 那一列是系统给它的类名。）

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

**去看那张 frame，再决定信不信它** —— 每个动作都在画面上真实发生过，看得见、可核对。

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
| `selftest` | 装完先跑一次：它拉起自带窗口，把每个手势验一遍，告诉你这台机器上哪些能用 |

退出码：`0` 执行了；`2` 被拒绝（正文以 `ERROR:`、`UNDECIDED:` 或 `ESCALATED:` 开头，什么都没发出去）。

## 装

```bash
pip install -r requirements.txt      # pillow, comtypes, numpy, rapidocr-onnxruntime
```

`rapidocr-onnxruntime` 那一层负责"读自绘界面上的字"（什么叫自绘见下）。它默认装，但代码当它可选：
没装它的环境会**少掉 OCR 这一档并说出来**，而不是 import 就崩。

装完跑一次 `python skills/athand/athand.py selftest`：18 项全过就说明这台机器上行；跑不了的项会报
`SKIP` 并说明原因（比如"机器锁着"），不会瞒成通过。

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

## 两条规则（用它的时候照这两条走）

1. **触手可及**：屏幕上够得着的东西优先。文件/程序的图标或行在屏幕上，就 `double-click` 它，别去写
   shell 命令。没在运行的窗口用 `restore` 走**应用自己的门**：任务栏按钮 → 托盘图标（含通知区溢出浮窗）
   → 桌面图标。三条门都真机验过，`ShowWindow` 只是"没有门"时的兜底——它给的是看着正常、却不吃输入的窗口，
   结果里会明说。
2. **文件的增删改移不走像素**：那是 shell 的活。像素路径要先解析成矩形，落到错误的一行就是数据事故。
   但把文件**交给某个应用**（输入框、拖放区）不是文件操作，那是 `drag`。

## 编号的规矩

每个手势只能报一个**编号**或一个**名字**，位置由程序自己解析 —— 它从无障碍树（Windows 给程序看的那份
控件清单，也叫 UIA）、OCR、或它从画面里切出来的形状里拿到矩形，再把编号交给你。同一批测试里，
模型直接给坐标偏 15–68 px，从候选里挑编号 3/3 命中。

- **编号属于那一次 listing。** `%TEMP%\athand\<hwnd>.json` 连窗口矩形和时间一起写下；窗口动过，后来的调用
  **拒绝**这个编号（"run `targets` again"）—— 重跑一次 `targets` 就好。
- **`--name` 不受窗口移动影响**：它是对活着的控件清单重新匹配，所以同一块界面动过之后，名字还能用。
  同一个名字有多个控件时，用 `--intent` 让 decider 挑（见上）。
- **自绘的窗口**（Chromium/Electron、Qt 这类，比如微信）在系统那份控件清单里可能什么都没有。这时图就是
  工具面：文字是从实拍帧上读出来的，按编号挑切出来的形状。**OCR 出来的名字是近似的**
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
- **动作一律是真实注入，不走无障碍触发（UIA Invoke）**：不是没做，是不做。每个手势都动真光标、把目标
  抬到前台，并留下一张实拍帧——动作在画面上存在，看得见、可核对；代价的另一半是**被遮挡 / 最小化 /
  托盘里的目标只能走应用自己的门，或者拒绝**。
- **它不替你监视任何东西**：无常驻进程、无屏幕差分轮询、无"变了再叫我"的回调。要等到某件事发生才动手，那件
  事得有个**接口**（应用自己的 IPC/事件、系统通知、或调用方自己的唤醒通道）；只能靠盯着屏幕看它变才能感知
  的状态，这个工具给不了。

## 想更深 / 想改它

- **[`skills/athand/SKILL.md`](skills/athand/SKILL.md)** —— 给 agent 读的说明书：流程、两条规则、
  18 条实测坑。想让它替你的 agent 干活，看这份。
- **[`skills/athand/NOTES.md`](skills/athand/NOTES.md)** —— 要改这个工具时看：自检的三条命令与基线、
  SKILL.md 这份契约的由来、以及只在改它时才会咬人的坑。
- **[`COMPARISON.md`](COMPARISON.md)** —— 与另一个同类工具 [cua-driver](https://github.com/trycua/cua)
  （`trycua/cua` 的桌面驱动层，MIT）的对照实测：同一台机器、同一个任务，两边都做成了，差别在动作怎么发出去、
  拿什么验收、什么时候拒绝。
- `skills/athand/probes/` 是自检用的探针（测试窗口），`athand.py` 的注释里会引用 `spec §NN` ——
  那指的是 [Fungi](https://github.com/CN-Fungi/Fungi) 仓库 `docs/spec.md` 的对应小节。

## 背景

`athand.py` 是 [Fungi](https://github.com/CN-Fungi/Fungi) 里的桌控工具，单独切出来发布（代码原样搬过来，
注释都是当初实测留下的）。它的立场是**不接管系统** —— 不装驱动、不接管输入，只用系统已有的控件清单、
OCR 和画面去操作**已经在跑的应用**；也**不做被动轮询** —— 没有 daemon、没有"每 N 秒看一眼屏幕"的循环，
只在你叫它的时候动。

来龙去脉见作者的知乎：[我们用的是同一个 Astra 吗？](https://zhuanlan.zhihu.com/p/2084012946414483140)、
[从 Psi 到 Fungi：Agent 菌丝网络与 AIOS](https://zhuanlan.zhihu.com/p/2079625942096528933)。

## License

MIT —— 见 [LICENSE](LICENSE)。
