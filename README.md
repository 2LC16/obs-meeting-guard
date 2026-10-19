# OBS 会议守护 (OBS Meeting Guard)

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6)](#)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-none-success)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**简体中文** | [English](README.en.md)

开完会总是忘记停止 OBS 录屏？让这个小工具替你盯着——**腾讯会议**（含**企业微信内置的融合版腾讯会议**）一结束，自动停止录屏。

它是一个常驻后台的小守护程序，当出现以下任一情况时，自动调用 OBS 的 `StopRecord`，不让录屏空转几个小时、白白吃掉几十 GB 硬盘（也避免录下会后桌面上的各种隐私画面）：

| # | 停止条件 | 默认值 |
|---|----------|--------|
| ① | 腾讯会议结束（会议窗口消失超过宽限期） | 宽限 30 秒 |
| ② | 画面连续静止超过设定时长（**且桌面音频同时安静**） | 900 秒（15 分钟） |
| ③ | 桌面音频电平（系统输出混音，**不含麦克风**）持续低于阈值 | -45 dB，持续 720 秒（12 分钟） |

> **条件②的门控**：会议仍在发声（桌面音频 > -45 dB）时，**即使画面静止也不会停止录屏**——避免"画面停在共享页面但会议还在说话"被误停。可用 `static_require_quiet = false` 关闭此门控。

## 特点

- **同时覆盖两种会议形态**：独立版腾讯会议（`wemeetapp.exe`）和企业微信会议（`WXWork.exe`，已从其程序二进制中核实会议窗口类名为 `Meeting*` / `Qt*`）；
- **图形界面 + 控制台双模式**：默认弹出状态窗口（状态灯 + 进度条 + 实时日志），也可用 `--console` 回到纯日志模式；
- **纯 Python 标准库实现**，运行不需要安装任何第三方包（界面为 Win32 原生窗口，不依赖 tkinter）；
- 自带极简 OBS WebSocket 客户端（obs-websocket v5 协议，支持密码认证、自动重连、版本自适应）；
- WASAPI 环回采集桌面音频（不经过麦克风），ctypes 直接截屏做画面静止检测；
- 一键自检（`--selftest` / 界面上的"自检"按钮）、模拟运行（`--dry-run` / 界面勾选）、窗口列举（`--list-windows`）；
- **全部在本地运行**，不访问网络（OBS 本机连接除外）、不上传任何数据。

## 下载与安装

### 方式一：直接用 exe（推荐）

1. 到 [Releases 页面](../../releases)下载最新的 `obs_meeting_guard.exe`（图形界面版）和 `obs_meeting_guard_console.exe`（控制台版，可选），放进任意文件夹；
2. 双击 `obs_meeting_guard.exe`——弹出状态窗口，**不会**有黑框；
3. 点击【开始守护】即开始工作；窗口可最小化，守护在后台继续跑；
4. 关闭窗口会弹确认（守护中时），**关闭守护不会停止 OBS 录屏**；
5. 想用纯日志模式：双击 `obs_meeting_guard_console.exe`。

> 首次运行如提示 "Failed to load Python DLL"，是安全软件（如 360）对新程序做实时扫描与自解压加载产生竞争导致，添加信任后重试即可，之后不会再出现。

### 方式二：用 Python 运行

需要 Windows 10/11 + Python 3.8 及以上（**无需安装任何第三方包**）：

```bat
python obs_meeting_guard.py            :: 图形界面(默认)
python obs_meeting_guard.py --console  :: 控制台模式
```

### 从源码自己打包 exe

装好 Python 3.8+ 后执行（仅需临时安装 PyInstaller）：

```bat
python -m pip install pyinstaller
build_exe.bat
```

## OBS 前置设置

1. OBS 菜单 **工具 → obs-websocket 设置**（OBS Studio 28+ 内置）；
2. 勾选**启用 WebSocket 服务器**，端口保持默认 `4455`；
3. 如果勾选了**启用身份验证**：程序会自动从 OBS 的配置文件
   （`%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json`，仅本机读取）
   获取密码；便携版 OBS 不适用时，请在 `config.ini` 的 `[obs] password` 手动填写；
4. 建议先跑一次自检：`obs_meeting_guard_console.exe --selftest`（或界面上的【自检】按钮），四项全 OK 即可放心使用。

## 界面说明

状态窗口分四块：

- **三个状态灯**：OBS 录屏（绿=录制中/守护生效）、腾讯会议（绿=会议进行中）、桌面音频（绿=有声、黄=安静累积中、红=采集不可用）；
- **三条条件进度条**：实时显示三个停止条件的计时进度（仅录屏中计时），走满即触发停止；当桌面音频仍有声时，"画面静止"进度条会标注"（仍有音频, 不触发）"；
- **当前桌面音频电平**（dB）与 OBS 连接状态；
- **实时日志区**（保留最近 300 行，完整日志见 `guard.log`）；
- **按钮**：开始守护 / 停止守护 / 模拟运行（勾选后只记录不真正停止）/ 自检 / 配置 / 退出。

## 工作原理

守护程序的主循环每秒执行一次（GUI 与控制台模式逻辑完全一致）：

1. **会议检测**：枚举腾讯会议/企业微信进程的顶层窗口，按"类名含 `Meeting`/`Qt` 关键词 + 尺寸达标 + 标题排除主界面"判定会议窗口是否存在（规则可在 `config.ini` 调整）；
2. **音频采集**（独立线程）：WASAPI 环回持续采集默认播放设备的混音，计算 RMS 电平（dBFS），累计"连续低于 -45 dB"的时长；
3. **画面检测**：每 30 秒截取整个虚拟屏幕，与上一帧比较平均绝对差，低于阈值视为"没有变化"，累计静止时长；
4. **OBS 状态**：每 2 秒查询一次是否在录屏；
5. **条件评估**：仅在"OBS 正在录屏"时评估三条停止条件，满足即调用 `StopRecord`，并重置所有计时器；
6. 录屏被手动停止/开始时会自动识别并切换守护状态；OBS 断开连接会自动重连。

补充说明：

- 会议结束条件要求"本次录屏期间检测到过会议"才开始计时，避免录别的内容被误停；
- **画面静止条件默认以桌面音频安静为前提**：当前电平不低于 -45 dB（会议仍在发声）时，静止计时照常累计但不触发停止，进度条会提示"（仍有音频, 不触发）"；需要独立生效时设 `static_require_quiet = false`；
- 模拟运行只记录"将要停止"的日志，不真正操作 OBS，适合先观察阈值是否合适；
- 每条心跳（5 分钟）会在日志里报告当前状态，方便确认守护正常工作。

## 配置说明（config.ini）

所有阈值都在 `config.ini` 里调整，改完保存、重启程序生效。仓库自带的 [config.ini](config.ini) 即为全部默认值。

### [obs] — OBS 连接

| 项 | 默认 | 说明 |
|----|------|------|
| host / port | 127.0.0.1 / 4455 | OBS WebSocket 地址 |
| password | 空 | OBS WebSocket 密码（启用自动读取时可不填） |
| auto_read_obs_password | true | 密码留空时自动从 OBS 配置文件读取（仅本机读取） |

### [guard] — 停止条件

| 项 | 默认 | 说明 |
|----|------|------|
| meeting_gone_grace_sec | 30 | 会议窗口消失后等待多久才判定"会议结束"（防抖动） |
| static_check_interval_sec | 30 | 每隔多少秒截屏比较一次 |
| static_duration_sec | 900 | 画面连续静止多少秒停止录屏 |
| static_diff_threshold | 0.5 | 画面差异阈值（0-255 平均绝对差），越大越容易判定静止 |
| quiet_threshold_db | -45 | 桌面音频安静阈值（dBFS） |
| quiet_duration_sec | 720 | 桌面音频持续安静多少秒停止录屏 |
| static_require_quiet | true | true=画面静止条件仅在桌面音频也低于阈值时才触发（会议有声时静止不停）；false=静止条件独立生效 |
| static_quiet_require_meeting | false | true=仅在检测到会议时，静止/安静条件才生效 |
| auto_start_recording | false | true=检测到会议自动开始录屏（默认只负责停止） |

### [meeting] — 会议检测

| 项 | 默认 | 说明 |
|----|------|------|
| process_names | wemeetapp.exe,WXWork.exe,WXWorkWeb.exe | 监视的进程 |
| min_width / min_height | 500 / 350 | 会议窗口最小尺寸（过滤悬浮工具条） |
| meeting_class_keywords | Meeting,Qt | 类名含任一关键词 → 判定为会议窗口 |
| meeting_title_keywords | 空 | 标题含任一关键词 → 也算会议窗口（默认关闭，防群聊名误判） |
| class_exclude_keywords | PerryShadow,TitleBar,IME,... | 类名含任一关键词 → 直接排除（辅助/隐藏窗口） |
| home_titles | 企业微信,微信,腾讯会议,... | 标题精确等于 → 排除（主界面） |
| title_exclude_keywords | 加入会议,设置,邀请,... | 标题含任一关键词 → 排除（仅对标题关键词信号生效） |

### [gui] — 图形界面

| 项 | 默认 | 说明 |
|----|------|------|
| always_on_top | false | 窗口总在最前 |
| notify_on_stop | true | 自动停止录屏时弹窗提醒 |

### [log] — 日志

| 项 | 默认 | 说明 |
|----|------|------|
| level | INFO | 日志级别（DEBUG 会记录每次检测到的窗口明细，排查很有用） |
| file | guard.log | 日志文件（程序同目录，自动滚动，最多 3 个备份） |

## 会议检测校准（重要）

会议检测基于窗口类名/标题启发式规则，默认规则已覆盖绝大多数情况。如果发现"会议结束了但没停止"或"没开会却以为在开会"：

1. 开会时运行 `obs_meeting_guard_console.exe --list-windows wxwork`（或 `wemeet`），查看会议窗口的**标题和类名**；
2. 对照 `config.ini` 的 `[meeting]` 段调整：
   - 类名不含 `Meeting`/`Qt` → 把实际类名关键词加进 `meeting_class_keywords`；
   - 或填写 `meeting_title_keywords`（如 `会议`）启用标题匹配；
3. 也可把 `config.ini` 的 `log.level` 改成 `DEBUG`，日志会记录每次检测到的窗口明细，便于排查。

## 常见问题

**Q: 首次运行提示 "Failed to load Python DLL"？**
这是安全软件（如 360 安全卫士）对首次运行的新程序做实时扫描，与程序自解压加载产生竞争导致的。退出安全软件（或在其中添加信任/允许）后重试即可；之后不会再出现。

**Q: 界面上的进度条怎么不动？**
进度条只在"OBS 正在录屏"时计时；未开始录屏时显示 0 是正常的。另外"会议结束"进度条需要本次录屏期间检测到过会议才开始走。

**Q: 会议结束了但没有停止录屏？**
查看日志（guard.log）里"会议检测"相关记录：若一直显示"未检测到会议窗口"，说明会议窗口的类名/标题不在默认规则里，按上面[会议检测校准](#会议检测校准重要)调整；若显示"会议进行中"，可能有其他大窗口被误判为会议窗口。

**Q: 录屏被提前停止了？**
- 开会时长时间共享一张静态幻灯片、且没人说话（桌面音频也安静）→ 画面静止条件会触发，可把 `static_duration_sec` 调大；只要会议还有声音，画面静止不会触发停止；
- 会议中全员静音/静音播放 → 音频安静条件会触发，可把 `quiet_threshold_db` 调低（如 -60）或 `quiet_duration_sec` 调大；
- 只想用"会议结束"一个条件 → 把 `static_duration_sec` 和 `quiet_duration_sec` 改成 86400（24 小时）。

**Q: 桌面音频采集不可用？**
自检会提示原因。常见于没有播放设备的机器；此时"音频安静"条件不生效，其余条件正常。

**Q: 开会时习惯最小化会议窗口？**
最小化的会议窗口仍算"会议中"。此时若桌面音频也安静，15 分钟后仍会触发静止停止——如不希望这样，请把 `static_duration_sec` 调大（会议仍有声音时不会触发）。

**Q: 会议里的"已进行时长"计时器会影响静止判定吗？**
会有一定影响：计时器数字每秒变化属于"画面变化"，会重置静止计时。但静止判定比较的是**整屏平均绝对差**（默认阈值 0.5/255），计时器只占屏幕很小一块，多数情况下其变化量低于阈值、不会阻止判定。如果发现会议一直无法判定静止（日志里"画面恢复动态"频繁出现），可把 `static_diff_threshold` 适当调大（如 1.0~2.0）忽略这类微小变化。真正会议结束时以条件①（窗口消失）为准，不受计时器影响。

**Q: 如何开机自动运行？**
把 `obs_meeting_guard.exe` 的快捷方式放进 `shell:startup`（运行对话框输入 `shell:startup` 打开的文件夹）。

**Q: 守护退出后录屏会停吗？**
不会。退出守护只是不再监视，OBS 继续录屏。

## 运行测试（可选）

```bat
cd tests
run_all.bat
```

包含：画面静止检测（8 项）、音频采集、OBS 协议（13 项）、守护逻辑端到端（10 项，含"有音频时静止不停止"门控场景）、图形界面（23 项）、GUI 版 exe 端到端（6 项），共 61 项断言。

## 仓库结构

```
obs-meeting-guard/
├── obs_meeting_guard.py           # 主程序源码(中文注释, 纯标准库, 单文件)
├── config.ini                     # 配置文件(所有阈值都在这里调)
├── build_exe.bat                  # 一键重新打包两个 exe(需要 pip install pyinstaller)
├── start_guard.bat                # 双击启动 GUI 版
├── requirements.txt               # 依赖说明(运行零依赖)
├── README.md / README.en.md       # 中/英文说明
├── LICENSE                        # MIT 许可证
└── tests/                         # 测试脚本(可选)
    ├── list_windows.py            #   窗口枚举诊断工具
    ├── test_screen.py             #   画面静止检测测试
    ├── test_audio.py              #   桌面音频采集测试
    ├── test_obs_ws.py             #   OBS WebSocket 协议测试(mock 服务器)
    ├── test_guard_e2e.py          #   守护逻辑端到端测试
    ├── test_gui.py                #   图形界面测试
    ├── e2e_gui_exe.py             #   GUI 版 exe 端到端测试(模拟点击按钮)
    ├── mock_obs_server.py         #   模拟 OBS 服务器(测试用)
    ├── probe_obs.py               #   OBS 协议探针(直连真实 OBS 排查请求名)
    ├── debug_com.py               #   WASAPI COM 调用诊断
    └── run_all.bat                #   一键运行全部测试
```

## 隐私声明

本程序**完全在本地运行**：不访问互联网、不上传任何数据、不写注册表、不装服务。唯一的网络连接是连到本机 OBS 的 WebSocket（127.0.0.1:4455）；唯一的本地文件读取是可选地从 OBS 自己的配置文件里读取 WebSocket 密码。

## 卸载

直接删除程序所在文件夹即可（不写注册表、不装服务、不留后台进程）。

## 许可证

[MIT](LICENSE)
