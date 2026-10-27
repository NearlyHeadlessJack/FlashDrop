# 开发、API 与 WebUI

本文面向要继续修改 FlashDrop 的开发者。先读 [AGENTS.md](../AGENTS.md) 的约束；协议细节见 [protocol.md](protocol.md)，系统结构见 [architecture.md](architecture.md)。

## 1. 环境与运行

- Python **3.10+**，无第三方运行时依赖。
- Windows 建议使用官方 Python，自带 `tkinter`。Linux 若需要原生文件窗口，安装 `python3-tk`；否则使用手动输入路径。
- 前端是原生 HTML/CSS/JS，没有 npm 构建步骤。
- 入口：

```powershell
python -m flashdrop
python -m flashdrop --no-browser --debug
python -m flashdrop --port 8765
```

CLI 参数：`--port`、`--no-browser`、`--debug`、`--version`。WebUI 端口被占用时会自动尝试临时端口，并由控制台打印实际 URL。

## 2. 仓库结构

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── start.bat
├── start.command            # macOS 双击启动
├── flashdrop/
│   ├── __init__.py          # 版本、应用名
│   ├── __main__.py          # python -m flashdrop
│   ├── cli.py               # argparse、启动、Ctrl+C
│   ├── app.py               # mode 与业务门面
│   ├── web.py               # HTTP server、API 路由、静态文件
│   ├── catalog.py           # 文件选择/展开/NFC/去重/快速指纹/权限位
│   ├── naming.py            # Windows 目标名称适配与冲突规划
│   ├── protocol.py          # 控制帧、数据帧、路径安全、ID
│   ├── control.py           # ControlChannel + 心跳
│   ├── sender.py            # 发送状态机与多通道调度
│   ├── receiver.py          # 接收状态机、续传扫描、落盘
│   ├── picker.py            # macOS osascript / Tk 选择器子进程
│   ├── metrics.py           # 进度、速度、ETA
│   ├── netutil.py           # 端口扫描、地址发现、socket 选项
│   ├── config.py            # 默认值和超时
│   └── web/
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       └── favicon.svg
├── tests/
│   ├── support.py
│   ├── test_protocol.py
│   ├── test_catalog.py
│   ├── test_naming.py
│   ├── test_picker.py
│   ├── test_writer.py
│   ├── test_integration.py
│   └── test_http_e2e.py
└── docs/
```

`tests/_tmp/` 是测试临时目录；测试会自行清理，且已在 `.gitignore` 中忽略。

## 3. JSON API

所有响应为 JSON。成功为 HTTP 200；用户错误为 400（`{"error": "...", "code": "bad_request"}`）；未知路径 404；未处理异常 500。API 只在本机回环地址提供。

### GET

| 路径 | 作用 |
|---|---|
| `/api/state` | 完整 UI 快照（模式、发送/接收状态、进度、清单统计、监听地址） |
| `/api/send/files?offset=0&limit=200` | 发送文件分页；limit 上限 500 |
| `/api/receive/files?offset=0&limit=200` | 远端 manifest 分页；Windows 上含 `dest_rel` 适配目标名 |
| `/api/health` | `{"ok": true}` |

`/api/state` 的关键结构：

```json
{
  "mode": "send",
  "busy": true,
  "defaults": {"control_port": 45870, "connections": 4},
  "send": {
    "status": "listening",
    "busy": true,
    "files": {"count": 3, "total_size": 1234, "revision": 8},
    "listen": {
      "port": 45870,
      "addresses": ["192.168.1.5", "127.0.0.1"],
      "data_ports": [45871, 45872, 45873, 45874],
      "connections": 4
    },
    "peer": null,
    "resume": {"files": 0, "bytes": 0},
    "progress": {"done": 0, "total": 1234, "percent": 0.0, "speed": 0.0, "eta": 0, "elapsed": 0.0},
    "error": null,
    "notice": "正在等待接收方连接",
    "completed": null
  },
  "receive": {
    "...": "...",
    "path_adaptation": {
      "renamed": 0,
      "examples": [],
      "warnings": []
    }
  }
}
```

实际字段以 `flashdrop/sender.py::SendService.state` 和 `receiver.py::ReceiveService.state` 为准。前端只消费这些快照。

### POST

| 路径 | Body | 作用 |
|---|---|---|
| `/api/mode` | `{"mode":"home\|send\|receive"}` | 切换模式；忙碌时拒绝 |
| `/api/send/pick-files` | `{}` | 打开本机多文件窗口并加入清单 |
| `/api/send/pick-folder` | `{}` | 打开文件夹窗口并递归加入 |
| `/api/send/add` | `{"paths":"D:\\a\\nD:\\b"}` 或 `{"paths":[...]}` | 手动添加文件/文件夹 |
| `/api/send/remove` | `{"id":"32-hex"}` | 移除一个文件 |
| `/api/send/clear` | `{}` | 清空清单 |
| `/api/send/start` | `{"port":45870,"connections":4}` | 开始监听；`port: 0` 自动分配 |
| `/api/send/stop` | `{}` | 停止监听/传输 |
| `/api/send/reset` | `{}` | 回到编辑状态（保留清单） |
| `/api/receive/connect` | `{"host":"192.168.1.23","port":45870}` | 异步连接并获取清单 |
| `/api/receive/set-dir` | `{"path":"D:\\receive"}` | 手动设置保存目录并扫描续传 |
| `/api/receive/choose-dir` | `{}` | 打开系统目录窗口并设置 |
| `/api/receive/start` | `{}` | 开始协商和接收 |
| `/api/receive/cancel` | `{}` | 取消当前接收 |
| `/api/receive/reset` | `{}` | 回到未连接状态 |

原生选择 API 会在服务端启动一个短命 Python 子进程运行 `flashdrop/picker.py`，读取其 stdout JSON。不要让 Tk 在 HTTP/服务线程中直接创建主窗口。

- macOS：`picker.py` 先调用 `/usr/bin/osascript -l JavaScript`，通过 Standard Additions 显示文件/文件夹窗口；无需 Tk。
- 其他平台或 osascript 失败：回退到独立进程中的 Tk。
- 选择器的 stdout 只有一个 JSON 对象；解析器兼容 osascript 可能的双重 JSON 编码。
- 取消选择返回空 `paths` 且退出码 0；实际错误才返回非零码和 `error`。

## 4. WebUI 结构

### 4.1 文件与职责

- `index.html`：三个视图（home/send/receive）、帮助对话框、静态 SVG 图标。
- `style.css`：暗色设计系统、响应式布局、轻量静态视觉；除短生命周期 spinner 外不使用持续动画或模糊滤镜。
- `app.js`：单 IIFE，无模块打包。

### 4.2 前端状态模型

```text
app.data             ← 最近一次 /api/state
app.refreshing       ← 防止轮询重叠
caches.send/receive  ← 文件列表分页缓存 {revision, items, total, loading, token}
lastSendStatus ...
```

轮询间隔为忙碌 1 秒、空闲 2.5 秒、页面隐藏 5 秒。文件列表按 `files.revision` / `manifest.revision` 变化重新加载，而不是每次轮询都拉取全量文件。

### 4.3 渲染原则

- 视图由后端的 `mode` 决定；不能用前端路由绕过忙碌检查。
- 所有来自后端/文件系统的文本用 `textContent` 插入。只有固定图标 HTML 使用 `innerHTML`。
- 状态映射集中在 `SEND_PHASES` / `RECEIVE_PHASES`；新增状态必须同时更新这里、`.busy` 判定、后端 state 和文档。
- 长操作按钮统一用 `withBusy()`，防止双击；错误用 toast，并在相关表单里保留可操作的 inline 错误。
- 文件列表最多先显示 120 项，剩余由「显示更多」分页。
- 接收端文件列表优先使用 `dest_rel`（Windows 实际保存名），并用「名称已适配」标识；`path_adaptation` 决定审核页提示。
- Mac 字体栈与 Safari 前缀集中在 `style.css` 根变量/相关规则；新增视觉特性前不要依赖只有 Chromium 支持的 CSS。
- **低渲染开销约束**：保留 `style.css` 末尾的 Lightweight rendering 规则。不要重新引入持续动画、`backdrop-filter`、大范围 `filter: blur`、轨道/粒子装饰或持续触发合成的阴影；交互反馈优先用颜色和边框。唯一允许的持续动画是短生命周期的 spinner。
- 新增轮询状态渲染时使用 DOM 差异更新/签名缓存，避免每次 poll 重建状态 badge、地址 chips、结果块、文件列表。现有缓存入口：`paintStatusBadge`、`renderShareAddresses`、`showResult`、`setSessionBlocks`。
- CSP 禁止内联脚本和内联事件处理；不要添加 `onclick` 属性。样式属性通过 JS 修改，CSP 的 `style-src` 已允许内联样式。

### 4.4 安全/交互注意

- 不要用浏览器 `<input type=file>` 作为主路径：它会把大文件读入浏览器，无法直接拿到本机路径，也不是本项目的“直传”语义。
- 保存路径是接收方本机隐私；发送方不会收到实际路径，只收到续传 offset。
- 确认类操作（清空、取消、停止）使用 `window.confirm`，保持简单。
- 关闭浏览器不会停止后端；页面重新打开会从 `/api/state` 恢复。

## 5. 典型改动流程

### 增加一个 API

1. 在对应 Service 增加线程安全的公开方法（必要时异步）。
2. 在 `FlashDropApp` 增加模式校验和门面方法。
3. 在 `flashdrop/web.py` 增加 `POST_ROUTES` 条目和处理器，或在 GET 分流中增加。
4. 在 `web/app.js` 调用 API、处理错误、刷新 state。
5. 在 `tests/test_http_e2e.py` 覆盖成功/失败路径。
6. 更新本文和 [protocol.md](protocol.md)（如果是传输协议而非纯 UI API）。

### 增加一个发送/接收状态

1. 更新 service 状态与 `BUSY_STATUSES` / `TERMINAL_STATUSES`。
2. 检查所有会等待/关闭的线程是否在该状态正确收尾。
3. 更新 `SEND_PHASES` / `RECEIVE_PHASES` 与相关渲染分支。
4. 更新 [architecture.md](architecture.md) 的状态表。
5. 增加状态转换测试，尤其要测试超时、断线和用户取消。

### 调整数据帧或续传格式

这是高危改动。必须：

- 升协议版本；
- 能识别旧 `.flashdrop` 状态并安全忽略/迁移；
- 更新协议文档中的字段、时序、offset 语义；
- 保留“接收端只提交连续前缀”的不变量；
- 用中断/重启场景测试。

## 6. 测试

```powershell
python -m unittest discover -s tests -t . -v
node --check flashdrop/web/app.js
Get-ChildItem flashdrop -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }
```

测试类别：

- `test_protocol.py`：ID、路径安全、控制帧、数据头布局。
- `test_catalog.py`：目录展开、`.flashdrop` 忽略、同名去重、NFC、增删。
- `test_naming.py`：Windows 非法字符、保留名、长组件、确定性、NFC 冲突。
- `test_picker.py`：JXA 脚本结构、osascript JSON 输出解析；不会在非 macOS 上启动真实选择器。
- `test_writer.py`：乱序块、重叠块、未完成文件、适配目标名、POSIX 权限（平台条件）。
- `test_integration.py`：真实 TCP 双实例、多通道、空文件、嵌套、续传、Windows 目标名规划。
- `test_http_e2e.py`：HTTP 完整传输、静态资源、`path_adaptation` 字段、模式/校验错误。

测试的设计偏好：

- 不 mock socket，用真实回环 TCP。
- 不依赖固定端口，发送端常用 `port=0`。
- 测试目录在 `tests/_tmp`，避免系统临时目录权限差异。
- 失败信息包含双方 state 快照，便于诊断。

### 手工 UI 冒烟清单

1. 打开首页 → 发送模式 → 手动输入文件夹 → 检查列表与删除。
2. 控制端口填 0 → 开启传输 → 检查地址/端口/数据端口。
3. 另一实例接收 → 填地址 → 连接 → 检查清单与文件大小。
4. 选择保存目录 → 开始接收 → 双方进度、速度、完成页。
5. 中途关闭接收端或停止发送 → 确认双方错误页可操作；重连后可续传。
6. 打开帮助、窄屏（≤680px）检查布局和按钮。
7. macOS：没有 Tk 时确认 osascript 文件窗口；允许本地网络权限后传输。
8. macOS → Windows：发一个含 `:`/`?` 或 `CON.txt` 的文件，确认接收端提示改名并使用安全路径。

## 7. 代码约定

- Python 3.10+；使用 `from __future__ import annotations`、`dataclass`、类型标注。
- 用户可见错误为中文；日志用英文/技术信息，带模块 logger。
- 不用第三方运行时库；如需增加依赖，需说明动机并更新 `pyproject.toml` 与文档。
- 文件操作优先 `pathlib.Path`；跨端路径在线上使用 `/`。
- 网络与磁盘操作失败必须转成用户可见错误，不能让线程静默死亡。
- 关闭 socket 使用 `shutdown`+`close`，以打断其他线程的阻塞读写。
- 不要在持 service 锁时做长时间 I/O 或 `join`。
- 新增 UI 文案时保持简洁、可扫描；不伪造“加密”“校验全部内容”等能力。

## 8. Release checklist

- [ ] 更新 `flashdrop/__init__.py` 与 `pyproject.toml` 的版本。
- [ ] 全量 unittest 通过。
- [ ] 双机/双实例含大文件、多文件、空文件、嵌套目录、续传各跑一次。
- [ ] `node --check flashdrop/web/app.js`。
- [ ] 页面无 console 异常；帮助/错误/进度/窄屏检查。
- [ ] 协议或限制变化同步到 `docs/` 与 `AGENTS.md`。
- [ ] 构建 wheel，确认包含 `flashdrop/web/*` 且 `flashdrop` 入口可用：
  `python -m build`（若安装了 build）或 `pip wheel .`。
- [ ] 在干净环境 `pip install dist/*.whl` 后运行 `flashdrop --no-browser`。
- [ ] 不把 `tests/_tmp/`、`.flashdrop/`、`artifacts/` 提交到版本库。
- [ ] 标签名必须与 `flashdrop/__init__.py` 的 `__version__` 一致（如 `v0.2.1`），CI 会校验，不一致直接失败。
- [ ] 推 `v*` 标签触发 `.github/workflows/release.yml`：构建 Windows amd64、macOS arm64、macOS x86_64 三个单文件可执行程序，并自动创建 release 挂到 assets。
- [ ] 单文件构建用 `packaging/flashdrop.spec`；新增非 Python 资源（如图片、模板）时同步更新其中的 `datas`，否则发行物里会缺文件。
