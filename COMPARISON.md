# 对照实测：athand 与 cua-driver（2026-09-20）

同一台机器、同一个任务，两个工具各做一遍：桌面上建一个空白 `.txt` → 用 GUI 打开它 → **逐字**输入 50 字
以上 → 保存 → 打开微信，进 `Blinvo` 会话，把那个文件拖进输入框、点发送。

**两边都做成了。** 差别不在成不成，在三件事上：动作怎么发出去、拿什么验收、什么时候拒绝。

## 被测对象

|  | 版本 | 安装 | 进程模型 |
|---|---|---|---|
| athand | 本仓库 `main` @ `2105ed7`（2026-09-20） | clone 即可，`pip install -r requirements.txt` | 一次调用一个进程，无 daemon |
| cua-driver | 0.28.2（`trycua/cua` 的 `libs/cua-driver`，MIT） | 官方 `cua-driver-rs-0.28.2-windows-x86_64.zip`：29,086,255 B，sha256 `3c1fcf10ff9513b94e4af78ad6a216ab62aa95b2c9a3b70dfbdba9f04e021533`（与 release 的 `checksums.txt` 一致），解出 3 个 exe + 1 个 dll ≈ 80 MB（下载 125 s，本机代理） | **一次性 CLI 调用必须先 `cua-driver serve`** |

机器：Windows 11 26200，2240×1400 @1.5；`.txt` 关联到 **Notepad++**（两个工具面对同一个目标）；
微信 `Weixin.exe`（Qt 自绘）测试前在托盘里。cua 侧的第一步是它自己的：`cua-driver telemetry disable`。

对比的对象只有 **cua-driver 这一层**，不是 trycua/cua 平台（云桌面 Fleets、Lume 虚拟机、cua-bench、
CUA-S1 模型都没测）。

## 结果

|  | athand | cua-driver |
|---|---|---|
| 落盘 | `对照测试_athand.txt` 175 B，**67 字逐字相等** | `对照测试_cua.txt` 172 B，64 字逐字相等（**修掉 1 个静默塞进去的 `s` 之后**） |
| 微信 | 1 条，发出、输入框清空 | **2 条**（拖拽重复投递） |

## 账

### 第 1 轮：打开桌面的 txt → 逐字输入 → 保存

| 步骤 | athand | cua-driver |
|---|---|---|
| 看窗口 | `windows` 0.5 s；`targets` 桌面 1.8 s（32 个图标编号） | `list_windows` 0.2 s；桌面图标**不在 UIA 里** → 先 `get_desktop_state`（0.2 s 出 2240×1400 真像素）再自己 OCR 找图标（3.3 s） |
| 显示桌面 | `key win d` 3.7 s | `hotkey win d`（`effect:"unverifiable"`）；改点任务栏右下角 → **报 `foreground_unavailable`，桌面其实出来了**（假阴性） |
| 打开文件 | 先**被正确拒绝**一次（图标被 Edge 盖住：`is covered: (175,910) belongs to '首页 - 知乎…'`，exit 2，什么都没发）→ 再双击成功 | `double_click(pid=6208,x=176,y=1088)`：background 那次**打开了**（报 `unverifiable`）；按它自己文档再补一次 foreground → **`foreground_unavailable`**（窗口已开、前台已易主，又一处假阴性） |
| 读窗口 | `targets` **15.6 s**：自绘，OCR 13 个文本框 + 2 个形状，如实说编辑器本体不可寻址 | `get_window_state` 0.8 s，49 个元素（菜单项都在），但**编辑器本体不在树里** |
| 逐字输入 | `type --char-delay 0.15`：**12.7 s / 67 字（≈190 ms/字）**，`typed: 67/67`，`verify: unverified（画面变了 0.56%）` | `type_text(delay_ms=150)`：**0.45 s / 64 字（7 ms/字，delay 没生效）**，`effect:"unverifiable"` + `escalation:{reason:"delivery_failed"}` —— **假警报，字全落进去了** |
| 保存 | `key ctrl s` 2.2 s，`verify: control value changed → verified` | background `ctrl+s`：**把字面 `s` 打进文档**、**没保存**、只报 `unverifiable`；foreground `ctrl+s` 才保存 |
| 合计 | 8 次调用 ≈ 39 s，1 次拒绝（拒绝得对） | 14 次调用 ≈ 5 s 驱动时间：2 次假阴性 + 1 次假警报 + 1 次静默污染 |

### 第 2 轮：微信 → Blinvo → 发文件

| 步骤 | athand | cua-driver |
|---|---|---|
| 把窗口叫回来 | `restore --hwnd 2097810` 2.6 s → `shell wake: clicked its 托盘图标 '微信' and it came up (the notification flyout is still open)`，hidden → normal | `list_windows(pid=17428)` **空**（隐藏窗口不枚举）→ `bring_to_front` → `refused: window_target_not_found, candidates: []` |
| 打开联系人 | `targets` 6.9 s（Qt 自绘：34 个 OCR 文本框 + 10 个形状）→ `#27 'Blinvo'` → click 1.7 s，`frame changed 18.19% → verified` | `get_window_state` → **`total_element_count: 0`**（只有 Window + 2 个 Pane）→ 只能像素：在它自己的截图上 OCR 出坐标 → click（`unverifiable`）。**像素没有过期保护**：我用稍旧的截图算出的坐标点到了**家庭群** |
| 拖文件 | `drag --to-hwnd 2097810 --dx 138 --dy 491 --route taskbar` 4.1 s：`74/74 points in 1.61s … via: its 任务栏按钮 '微信 - 1 个运行窗口' — hovered 0.60s until the drop point came within reach … verify: the destination came back on screen → verified` | `drag(scope:'desktop', from=(176,1088), to=(1600,1090))`：background 与 foreground 两次都回 `delivery.mode:"not_applicable"` + `effect:"unverifiable"`，**两次都真的执行了** → 输入框里两张同名卡片 |
| 发送 | `targets` 10.7 s + `click --name 发送` 10.1 s，`frame changed 14.26% → verified` | `click(px 1229,1078)`（`unverifiable`） |

## 四条差异

### 1. 验收哲学

cua 的回答是机读的：`effect` / `delivery.mode` / `escalation` / 具名拒绝码（`background_unavailable`、
`window_target_not_found`…），诚实承认不确定；但在自绘目标上它**几乎全程 `unverifiable`**，其中一条是**静默
改写用户文档**（见下），另一条**把调用方导向重复投递**。athand 用程序侧事实说话（画面差分、控件值、窗口
状态、`GetAsyncKeyState`），拒绝时 exit 2 + 一句点名原因；这一轮它唯一的 `unverified` 是诚实的那一种
（Scintilla 不暴露可读值），硬证据来自磁盘。

### 2. 门与前台

托盘隐藏窗口、被遮挡的拖动起点、最小化窗口的几何——athand 有能力（三条门、`--route taskbar`、窗口状态
判据）；cua 在这一层是空白（`window_target_not_found`），它把成本转给了"永不抢前台"的背景投递，而这套在
Qt 自绘目标上没有元素可用。

### 3. 动作怎么发出去

cua 的背景路径（UIA Invoke / PostMessage）在**可寻址控件**上确实快，并且不抢前台——这是它唯一的、athand
没有的能力。athand 一律 `SendInput` 真实注入：动真光标、抬目标窗口、每步一张实拍帧。这是取舍不是缺口，
理由写在 [`README.md`](README.md) 的「边界」那条。

### 4. 逐字 vs 快

cua 7 ms/字（`delay_ms` 形同虚设，整批 PostMessage）；athand 190 ms/字（0.15 s/字的节奏 + 进程开销）。
前者"看起来像粘贴"，正是 athand 的逐字输入要消灭的东西（spec §37）。

## 对方的账（量到的，不是推测）

- **`ctrl+s` 静默污染**：background `hotkey(["ctrl","s"])` 在 legacy Win32 编辑器上把**字面 `s`** 打进了文档
  （状态栏 length 173 → 174，光标列 66 → 67），**文件没保存**，报告只有 `effect:"unverifiable"`。又跑一次
  复现（174 → 175，文件仍是 173 B）。foreground 那次才真的保存。
- **`drag` 的 `not_applicable` 不能当失败**：它的文档说 `unverifiable` 就升级重试——而这条拖拽两次都真的
  执行了，于是往同一个会话里投递了两份。
- **两处假阴性**：点任务栏"显示桌面"（桌面确实出来了）与第二次 `double_click`（文件确实已打开）都报
  `foreground_unavailable`，因为它的验收假设"点到的那个窗口应当变成前台"。
- **退出码**：参数错误（`Missing required integer field: pid`）与工具级拒绝**都是 `rc=0`**，只有 daemon
  没起时 `rc=1`。调用方只能读 payload 判断成败。

## athand 这边的代价（同一轮量到的）

- `targets` 在自绘/大窗口上一次 **6.9–15.6 s**（OCR + 画编号 PNG）；cua 的 UIA 走一遍 0.3–0.8 s，代价是
  自绘窗口下它返回 0 个元素。
- 微信这种 Qt 自绘目标 a11y **0 元素**（两边一样）——athand 的图就是工具面，cua 要靠调用方在外面算像素。
- `type` 在 Scintilla 上的验收只能是"画面变了 0.56%"，硬证据在磁盘。
- 桌面图标被别的窗口盖住时**直接拒绝**（本轮第一次双击）：这是设计（点错一行是数据事故），但也确实是能力
  边界——见 README 边界那条。

## 方法备注与局限

- **cua 侧的像素是调用方算的**：它没有 `targets` 那样的候选编号，桌面图标与微信联系人的坐标是我在它自己的
  截图上 OCR 出来的（3.3 s）。这一条对 cua 不算公平，记在这里。
- 两个工具的 `.txt` 由同一个 shell 建（athand 的规则 2 要求文件操作走 shell；cua 没有这条规则，本轮也走
  同一条件）。
- Blinvo 会话里最终有 3 条消息：athand 1 条、cua 2 条（重复的那条是 cua 的拖拽行为）。
- n=1、单机、单次；不构成对 cua 平台（macOS/Linux/云桌面/bench/模型）的任何判断。
- cua 侧结束后已收尾：daemon 停掉、`~/.cua-driver` 与临时目录删除。

## 复跑命令

```bash
# athand（本仓库）
python skills/athand/athand.py windows
python skills/athand/athand.py targets --hwnd <桌面>                     # 桌面图标编号
python skills/athand/athand.py double-click --hwnd <桌面> --target <文件图标>
python skills/athand/athand.py type --hwnd <编辑器> --text "…" --char-delay 0.15
python skills/athand/athand.py key --hwnd <编辑器> --keys ctrl s
python skills/athand/athand.py restore --hwnd <微信>                      # 走应用自己的门
python skills/athand/athand.py drag --hwnd <桌面> --target <文件图标> \
       --to-hwnd <微信> --dx 138 --dy 491 --route taskbar
python skills/athand/athand.py click --hwnd <微信> --name 发送

# cua-driver 0.28.2（Windows x86_64）
cua-driver serve                                   # 不做这步，每个调用都拒绝
cua-driver get_desktop_state '{"screenshot_out_file":"desktop.png"}'
cua-driver double_click '{"pid":<explorer>,"x":176,"y":1088}'
cua-driver get_window_state '{"pid":<notepad++>,"window_id":<id>,"screenshot_out_file":"w.png"}'
cua-driver type_text    '{"pid":<notepad++>,"text":"…","delay_ms":150}'
cua-driver hotkey       '{"pid":<notepad++>,"keys":["ctrl","s"],"delivery_mode":"foreground"}'
cua-driver click        '{"pid":<weixin>,"window_id":<id>,"x":208,"y":153}'
cua-driver drag         '{"scope":"desktop","from_x":176,"from_y":1088,"to_x":1600,"to_y":1090}'
```
