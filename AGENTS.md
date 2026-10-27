# AGENTS.md — FlashDrop / 闪传

> 面向在本仓库继续开发的人或代理。**先读完本文件，再按任务路由只打开需要的文档**。本文件是入口和约束清单，不重复协议细节。

## 项目是什么

局域网点对点文件快传桌面工具，Python 后端 + 浏览器 WebUI。

- 收发二合一；发送端监听控制端口 + 多条数据端口，接收端主动连接。
- 文件选择走本机系统窗口（macOS 优先 osascript，其他平台用 Tk 子进程），不经过浏览器上传。
- 默认 4 条数据连接，可配置 1–8 条；控制通道负责握手、清单、续传协商、进度与完成确认。
- 断点续传基于保存目录下的 `.flashdrop/{state,parts}`；数据块 CRC32 校验；完成后原子 `os.replace`。
- 仅依赖 Python 标准库；WebUI 是无构建步骤的 HTML/CSS/JS。
- **不做加密/认证**，定位是可信局域网。不要把它当作公网传输工具。

## 快速命令

```powershell
# 运行（从仓库根目录）
python -m flashdrop
python -m flashdrop --no-browser --debug

# 全量测试（unittest；测试临时目录在 tests/_tmp/）
python -m unittest discover -s tests -t . -v

# 轻量静态检查
Get-ChildItem flashdrop -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }
node --check flashdrop/web/app.js

# 仅起一个不自动开浏览器的实例
python -m flashdrop --no-browser --port 8765
```

macOS：

```bash
python3 -m flashdrop
chmod +x start.command && open start.command   # 或 zsh start.command
python3 -m unittest discover -s tests -t . -v
```

- Python 要求：**3.10+**，本机验证用 3.14。
- WebUI 地址每次启动会打印；端口被占用会自动退到其他端口。
- WebUI 只绑定 `127.0.0.1`；P2P 控制端口绑定 `0.0.0.0`。
- 端到端测试会启动两个应用实例和真实 TCP 连接，避免只用 mock。

## 代码地图

| 路径 | 职责 | 改动时先读 |
|---|---|---|
| `flashdrop/protocol.py` | 控制帧、数据帧、文件 ID、相对路径安全校验 | `docs/protocol.md` |
| `flashdrop/control.py` | JSON 控制通道、读写锁、心跳 | `docs/protocol.md` |
| `flashdrop/sender.py` | 发送监听、状态机、清单、多通道调度、完成确认 | `docs/architecture.md` |
| `flashdrop/receiver.py` | 接收连接、续传扫描、乱序缓冲、文件落盘 | `docs/architecture.md` |
| `flashdrop/catalog.py` | 文件/文件夹选择、Unicode 归一、去重、快速指纹 | `docs/development.md` |
| `flashdrop/naming.py` | NFC、Windows 目标文件名适配与冲突检测 | `docs/compatibility.md` |
| `flashdrop/app.py` / `web.py` | 应用门面、JSON API、静态资源 | `docs/development.md` |
| `flashdrop/web/` | WebUI（无框架、轮询 `/api/state`） | `docs/development.md` |
| `flashdrop/picker.py` | macOS osascript / 独立 Tk 选择器 | `docs/compatibility.md`、`docs/decisions.md` |
| `tests/` | 单元 + 本机双实例端到端 + HTTP/UI 静态检查 | `docs/development.md` |

## 不可破坏的约束

1. **先协商，后传输**：发送端收到接收方 `prepare` → 回 `ready` → 收 `start` → 回 `started` → 收 `data_ready`，才允许发数据帧。
2. **完成语义**：接收方只有在 `all_sent`、所有数据读线程结束、且无错误时才启动 `_finalize_worker`。不要把“收到字节”当作“已保存”。
3. **按文件连续落盘**：`ReceiveFileWriter` 只提交连续前缀。续传 offset = `.part` 文件长度。不要改成散写而不更新恢复协议。
4. **原子替换**：最终路径只能由 `.part` 经 `os.replace` 产生；失败时保留续传记录。
5. **续传身份**：file id 由 NFC 相对路径 casefold 后的 BLAKE2b 前 16 字节得到；状态匹配还要求 path/size/mtime/quick_hash。修改任一项都会破坏旧续传。
6. **数据帧**：`!4s16sQII` = magic + file id + offset + payload length + CRC32(payload)，长度为 36 字节。改字段就要升级协议版本并同步 `docs/protocol.md`。
7. **路径安全**：接收端必须经过 `safe_transfer_path`，拒绝绝对路径、盘符、`..` 和顶层 `.flashdrop`。线协议只用 `/` 分隔；反斜杠是合法 POSIX 文件名字符，由 Windows 目标适配器转换。
8. **源文件一致性**：发送结束后 `_supervise` 会复查源文件 size/mtime；不要移除这一步。
9. **锁顺序**：尽量保持“应用锁 → 服务锁 → 传输锁”的粗粒度顺序；控制回调里不要做长时间磁盘操作。
10. **界面/API 语言**：用户可见文案为中文，代码标识和协议字段为英文。不要引入构建工具或第三方运行时依赖，除非同步更新文档与打包配置。
11. **跨平台命名**：发送端把路径规范为 NFC；Windows 接收端必须通过 `naming.plan_destinations` 生成目标路径，不要直接拿清单路径拼盘。冲突时拒绝，不覆盖。
12. **权限位**：清单中的 `mode` 只允许 `0o777` 的 rwx 位；只在 POSIX 接收端应用。不要加入 setuid/setgid/sticky。
13. **低开销前端**：不要重新加入持续动画、`backdrop-filter`、大范围模糊/光晕或每次轮询重建 DOM。保持 1 秒/2.5 秒/隐藏 5 秒的轮询节奏和状态签名缓存。

## 按任务查文档（渐进式披露）

| 你要做的事 | 只读这些 |
|---|---|
| 改控制消息、数据帧、握手、超时 | `docs/protocol.md`，再看 `docs/architecture.md` 的状态机 |
| 改发送/接收调度、续传、并发、线程 | `docs/architecture.md`，再看 `docs/decisions.md` |
| 改 WebUI、加 API、改交互 | `docs/development.md`，必要时 `docs/protocol.md` 只读 API 部分 |
| 改 Windows/macOS 文件命名、AppleScript、Safari | `docs/compatibility.md` |
| 部署、防火墙、排查、恢复数据 | `docs/operations.md` |
| 理解为什么不用框架/加密/全文件哈希 | `docs/decisions.md` |
| 打包、发布、验收 | `docs/development.md` 的 Release checklist |

## 状态与限制（当前版本）

- 单会话：同一进程同一时间只允许一个发送会话或一个接收会话。
- 空目录、符号链接语义、扩展属性/资源叉、ACL 不传输；POSIX 两端保留 rwx 权限。
- 快速指纹采样头尾各最多 64 KiB，不是完整内容哈希；传输可靠性依赖每块 CRC32 + 精确大小 + 原子写入。
- 不加密、不认证；控制监听 `0.0.0.0`，任何能连上控制端口的客户端都可能成为接收方。
- 数据端口在启动时动态选择；防火墙按程序放行最可靠。
- 大量文件清单会整体放进欢迎控制帧（上限 128 MiB），WebUI 文件列表分页加载。
- macOS 端已实现 osascript 选择器、NFC、POSIX 权限和 Windows 名称适配，但尚未在真实 Mac 上验收。
- 浏览器关闭不影响后端；关闭启动终端或进程会中止，接收端保留分片用于续传。

## 改动后的最低验证

1. `python -m unittest discover -s tests -t . -v` 全绿。
2. 若改了协议/续传/写盘：额外用两个临时目录跑一次含空文件、大文件、嵌套目录的端到端传输，并比较字节。
3. 若改了 WebUI/API：`node --check flashdrop/web/app.js`，打开页面确认首屏、发送、接收、错误提示和进度条没有脚本异常。
4. 若改了打包：`pip install -e .` 或构建 wheel，确认 `web/` 静态资源和 `flashdrop` 入口都进入发行物。
5. 文档与实现同步：协议、限制、命令改了就更新对应文档和本文件的“状态与限制”。

## 文档地图

```text
AGENTS.md                  ← 你在这里；入口、约束、路由
README.md                  ← 用户视角：安装、双机流程、FAQ、限制
docs/architecture.md       ← 组件、线程、状态机、续传目录、关键不变量
docs/protocol.md           ← 控制消息表 + 数据帧布局 + 握手时序
docs/development.md        ← 仓库结构、API 表、前端结构、测试/发布
docs/operations.md         ← 部署、端口/防火墙、排查、数据恢复
docs/compatibility.md      ← Windows↔macOS 名称/权限/选择器/浏览器矩阵
docs/decisions.md          ← ADR：技术取舍与升级路径
```
