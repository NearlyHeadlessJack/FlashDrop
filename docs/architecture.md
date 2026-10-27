# 架构与状态机

本文描述 FlashDrop 的进程内组件、线程模型、收发状态机、续传落盘机制和修改代码时必须保持的不变量。协议报文细节见 [protocol.md](protocol.md)，运行与排查见 [operations.md](operations.md)。

## 1. 总体结构

```text
┌──────────────────────────── 本机：运行 python -m flashdrop ────────────────────────────┐
│                                                                                         │
│  Browser (127.0.0.1)                 HTTP/JSON + 静态资源                                │
│       │                                   │                                             │
│       ▼                                   ▼                                             │
│  web/index.html + app.js  ◄────  web.py (ThreadingHTTPServer)                            │
│                                           │                                             │
│                                     app.py (FlashDropApp)                               │
│                                      ├─ mode: home/send/receive                         │
│                                      ├─ catalog.py (待发送文件)                          │
│                                      ├─ sender.py (SendService)                          │
│                                      └─ receiver.py (ReceiveService)                     │
│                                                                                         │
│  发送端（一个会话）                          TCP                接收端（一个会话）         │
│  0.0.0.0:控制端口 ◄──── JSON 控制消息 ─────────────────────► 连接发送端                   │
│  0.0.0.0:数据端口 1..N ◄──── 二进制分块 ───────────────────► 接收并写 .part               │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

一台电脑既可以发送也可以接收，但程序当前只允许一个活动会话。WebUI 是纯本机控制台；文件字节永远不会经过 WebUI 的 HTTP 端口。

## 2. 主要组件

| 组件 | 职责 | 关键点 |
|---|---|---|
| `FlashDropApp` | 模式、发送/接收服务门面 | 不允许同时开始发送和接收 |
| `SenderCatalog` | 文件/文件夹展开、NFC 归一、相对路径去重、指纹输入 | 文件 ID 由 NFC 相对路径决定 |
| `SendService` | 监听控制端口、接受一个接收方、会话生命周期 | 支持在确认前断开后重新等待 |
| `SendTransfer` | 分块调度、N 条数据连接、发送进度 | 有界在途窗口 |
| `ReceiveService` | 连接、清单、保存位置、Windows 目标名适配、续传扫描、完成保存 | 只用本机状态验证续传 |
| `ReceiveTransfer` | 数据读线程、文件写入器、完成栅栏 | 每个文件只提交连续前缀 |
| `naming.py` | NFC、Windows 文件名适配、目标冲突检测 | 清单源路径保持不变 |
| `ControlChannel` | 长度前缀 JSON、心跳、线程安全发送 | 关闭回调只触发一次 |
| `picker.py` | macOS osascript 选择器，其他平台 Tk | 独立进程，不阻塞服务线程 |
| `web/` | 无构建前端 | 轮询 `/api/state`，分页取文件列表 |

## 3. 线程模型

### 3.1 发送端

```text
HTTP 线程
  └─ SendService.start() 只做校验与派发

send-service 线程
  ├─ 计算清单指纹
  ├─ bind 控制端口 + N 个数据端口
  ├─ accept 控制连接
  └─ 等待 ControlChannel 关闭

ControlChannel reader 线程（一个接收方一个）
  ├─ prepare / start / data_ready / completed / cancel
  ├─ ping → pong
  └─ 心跳由独立 heartbeat 线程发送

send-data-accept-N（每个数据端口一个）
  └─ 验证 data_hello token，注册 socket

send-data-N（每个已连接数据通道一个）
  ├─ 从全局游标领取下一个块
  ├─ 读源文件、算 CRC32、发送 36B 头 + payload
  └─ 达到窗口上限时等待

send-data-supervisor
  ├─ join 所有 worker
  ├─ 复查源文件 size/mtime
  └─ 成功 → all_sent；失败 → 通知控制线程

send-progress
  └─ 每秒把聚合进度发送给接收方并更新本地 state
```

### 3.2 接收端

```text
HTTP 线程
  └─ ReceiveService.connect/start() 只做校验与派发

receive-connect
  ├─ TCP connect + hello/welcome
  └─ 进入 review

receive-control reader + heartbeat
  ├─ ready → 发送 start
  ├─ started → 派发数据连接建立
  ├─ all_sent → 标记完成候选
  ├─ completed/error/abort 处理
  └─ 控制失联 → 中止

receive-prepare
  └─ 再次扫描续传状态、检查空间、发送 prepare

data setup 线程/线程池
  ├─ 并行连接各数据端口
  ├─ data_hello / data_ok 握手
  └─ 启动 receive-data-N，然后发送 data_ready

receive-data-N
  ├─ 收 36B 头、读 payload、校验 CRC32
  ├─ 写入 ReceiveFileWriter（按连续顺序提交）
  └─ EOF/错误 → 记录并触发完成条件

receive-finalize
  ├─ 等 all_sent + 全部 reader 结束 + 无错误
  ├─ 对每个文件 fsync + os.replace
  ├─ 清理该清单的 .flashdrop 状态
  └─ 回 completed，关闭控制连接
```

WebUI 不维持 WebSocket；每 650–2600 ms 轮询 `/api/state`，文件列表只在 revision 变化时重新加载。

## 4. 发送状态机

| 状态 | 含义 | 可进入 | 退出 |
|---|---|---|---|
| `idle` | 编辑文件清单 | 重置 | 开始 |
| `preparing` | 指纹计算、绑定端口 | 开始 | 成功→listening；异常→error |
| `listening` | 等待接收方控制连接 | 准备完成、接收方提前断开 | 有连接→negotiating；停止→stopped |
| `negotiating` | 已交换清单，等待 prepare/start | 控制握手 | start→starting；断开→listening |
| `starting` | 建数据通道，等待 data_ready | start | data_ready→transferring；失败→error |
| `transferring` | 多通道发送中 | data_ready | 全部 worker 结束→finishing |
| `finishing` | 等待接收方保存/校验确认 | all_sent | completed 消息→completed；断线→error |
| `completed` | 接收方确认完成 | completed | 重置后才能再次发送 |
| `error` / `stopped` / `cancelled` | 终态 | 异常/用户/对方取消 | 重置 |

注意：`listening → negotiating` 之后如果接收方只是关闭页面/取消确认，发送端会回到 `listening` 继续等待，而不是把服务杀掉。

## 5. 接收状态机

| 状态 | 含义 | 说明 |
|---|---|---|
| `idle` | 填写地址 | 不保留活动连接 |
| `connecting` | TCP + welcome | 失败→error |
| `review` | 已收到清单，等待保存位置 | 尚未发送 prepare；可取消 |
| `preparing` | 扫描续传、检查空间、等待 ready | 失败可回到 review（如空间不足） |
| `starting` | 已发 start，建立数据通道 | 等待 sender 的 started/data_ready |
| `transferring` | 接收字节并写入 `.part` | 取消会保留分片 |
| `verifying` | 校验并原子保存 | 不可取消；完成→completed |
| `completed` | 全部文件保存成功 | 可重置 |
| `error` / `cancelled` | 终态 | 保留状态文件以便手动/下次续传 |

空间不足时接收端会回到 `review` 并把错误显示在保存位置区域，保持与发送方的控制连接，允许用户改目录后重试。其他准备失败通常会关闭连接进入 `error`。

## 6. 续传与落盘

### 6.1 目录结构

```text
<save root>/
├── .flashdrop/
│   ├── state/<32-hex-file-id>.json
│   └── parts/<32-hex-file-id>.part
└── <manifest 相对路径>
```

状态示例：

```json
{
  "version": 1,
  "id": "80bcd1d8ca596b5842c8bdbb4f8e6e2d",
  "path": "项目归档/素材.zip",
  "size": 19327352832,
  "mtime_ns": 1760000000000000000,
  "quick_hash": "5f...",
  "status": "partial"
}
```

`status` 可为 `partial` 或 `done`。但 `scan_resume` 仍然会核对目标文件是否存在、大小是否一致；任何字段不匹配就忽略该记录并从头开始，不删除未知文件。

### 6.2 文件 ID、NFC 与指纹

```text
relative_path = NFC(original_relative_path)      # 线协议只使用 / 分隔
id            = BLAKE2b(relative_path.casefold(), digest_size=16).hex()
quick_hash    = SHA-256("flashdrop-qh1" + size + mtime_ns + head(64KiB) + tail(64KiB))
```

- macOS 文件系统常用 NFD，发送端统一转成 NFC，确保 Windows 和 macOS 的“同一个视觉名称”产生相同身份。
- 文件 ID 使用 casefold 是为了让大小写不敏感的 Windows/macOS 目标不会出现两个路径碰撞。
- 发送端在创建清单时还会对路径 casefold 去重，必要时加 ` (2)` 后缀。
- 线协议中反斜杠是普通 POSIX 文件名字符，不是分隔符；Windows 接收端会将其适配为安全名称。
- 快速指纹不是全文件哈希，设计理由是避免开始传 100 GB 文件前完整读一遍。详见 [decisions.md](decisions.md)。

### 6.3 Windows 目标名称适配

清单中的 `rel_path` 保留发送端源名，用于续传匹配和展示；`RemoteFile.destination_rel` 是实际落盘路径。接收端在 `os.name == "nt"` 时通过 `naming.plan_destinations` 生成目标路径：

- 非法字符、控制字符、尾部空格/点替换或清理；
- Windows 保留设备名避让；
- 过长组件截短，并附带由源路径派生的稳定短哈希；
- 同一次清单生成结果必须无大小写/目录前缀冲突，否则拒绝开始；
- 示例与前缀完整说明见 [compatibility.md](compatibility.md)。

状态文件中的 `path` 始终保存源清单路径，因此 Windows 适配不会破坏续传身份；`done` 文件存在性检查使用当前目标路径。

### 6.4 权限与 mtime

- POSIX 发送端在清单中附带 `mode`（仅 `0o777` rwx 位）。
- POSIX 接收端在 `os.replace` 后尽力 `os.chmod`；Windows 接收端忽略。
- 不传输 setuid/setgid/sticky、ACL、扩展属性、资源叉。
- 文件 mtime 在最终落盘后通过 `os.utime` 尽力恢复；目录权限不处理。

### 6.5 接收写入

`ReceiveFileWriter` 对每个文件维护：

- `initial_offset`：续传协商接受的起点，来自 `.part` 长度。
- `expected`：下一个应连续写入的偏移。
- `pending`：早到的、乱序的块（最多 `MAX_PENDING_CHUNKS`）。
- 单一文件句柄；文件完整时立即 close，避免同时打开十万个文件。

收到偏移等于 `expected` 的块时写入并推进，然后排空可连续写出的 pending。偏移更大的块只缓存；小于 `expected` 或重叠则报错。因此 `.part` 始终是连续前缀，崩溃后可以直接用文件长度作为续传 offset。

发送端为了限制这种乱序缓存，使用“在途窗口”：

```text
window = max(连接数, min(64, 连接数 * 4))
```

每个 worker 领取块会占用窗口，发送完释放。窗口满时从条件变量上等待。这样即使一条连接暂时变慢，也不会让接收端无限内存增长。

### 6.6 原子完成

每个文件的最终化步骤：

1. 校验 `expected == size` 且 pending 为空；
2. 打开 `.part`，核对实际长度；
3. `os.fsync`；
4. `destination.parent.mkdir(parents=True, exist_ok=True)`；
5. `os.replace(part, destination)`；
6. `os.utime` 恢复 mtime（失败只忽略时间戳）；
7. 尽力写 `status=done` 状态记录。

只有全部文件成功才发送 `completed ok=true`，并清理本清单对应的状态。若某文件失败，已成功文件保留最终路径和 done 记录；失败文件的 part/状态保留。下次连接同一发送方并选择同一目录时，扫描会跳过已完成文件并继续失败文件。

### 6.7 为什么以文件为单位保存

不是整次传输成功后才保存：一个 100 GB 的多文件任务如果“全有或全无”，任何末尾错误都会让已接收的大文件全部重来。当前策略尽量保留每个完整文件，同时用连续分片保证半文件也可续传。

## 7. 可靠性与失败模型

| 失败 | 行为 |
|---|---|
| 控制心跳丢失 | 双方关闭数据 socket；接收端保留分片并进入 error |
| 数据连接断 | worker 设置错误、关闭其他通道；发送失败；接收端保留可续传前缀 |
| 数据块 CRC 错 | 接收端立即失败，不写入该块 |
| 源文件在发送中改变 | worker 在打开时检查，supervisor 在结束时复查 size/mtime；发送端报错，不发送 all_sent |
| 接收端磁盘满 / 写失败 | 数据线程错误 → 中止；已完成文件保留 |
| macOS → Windows 文件名非法 | 接收端自动生成稳定安全名，并在审核页显示；无法消除的冲突会拒绝开始 |
| 目标路径过长 | Windows 审核页给长路径警告；保存失败时保留分片 |
| 目标文件被占用/只读 | 最终化失败；整轮报错；分片保留 |
| 进程崩溃 | `.part` 是连续前缀；状态 JSON 可能缺少最后一次 done 标记，最坏重传该文件 |
| 接收端在确认前断开 | 发送端回到 listening，继续等待下一个接收方 |

没有采用全文件哈希或加密；这两项是有意的范围取舍，见 [decisions.md](decisions.md)。

## 8. 并发与锁

- `SendService.lock`：会话/显示状态。
- `SenderCatalog._lock`：文件列表；`_mutation_lock` 串行化选择/清除。
- `ControlChannel._send_lock`：socket 写入；JSON 帧不会交错。
- `SendTransfer._lock`：游标、在途窗口、已发送字节、数据通道列表。
- `ReceiveService.lock`：接收状态机。
- `ReceiveService._dir_lock`：串行化磁盘上的续传扫描。
- `ReceiveFileWriter.lock`：单文件写入/pending/句柄。

总体原则：

1. 不在持锁时做 `accept()`、文件系统扫描或网络发送等长操作。
2. 控制消息回调只做短暂状态转换；最终化另起线程。
3. 关闭数据 socket 可以打断阻塞的 send/recv，是主动中止的主要手段。
4. 任何新的跨服务调用都要先确认锁的顺序，避免 A 持有 service.lock 等 transfer.lock，同时 B 反向等待。

## 9. 扩展建议

- **跨平台元数据**：符号链接、扩展属性/资源叉、ACL 需要新的 manifest 字段与专门的恢复策略；不要把 `mode` 当作完整元数据。
- **全文件哈希**：在 `ready` 协商中增加可选 hash 字段，完成后接收端校验；需要处理续传时的前缀哈希或改为可恢复的 Merkle/chunk 哈希。
- **多接收方/广播**：当前单会话模型需要拆分 SendService 的 listener 生命周期和每个接收方的独立 token，属于较大改动。
- **压缩/加密**：应插在数据帧 payload 层，需同步升级协议版本和数据帧头。
- **断点状态清理命令**：目前只有正常完成后自动清理；可作为显式运维工具加入。
- **全局限速/带宽分配**：可在 SendTransfer 的 worker 写 socket 前加入令牌桶，需保持窗口限制。

更细的协议字段见 [protocol.md](protocol.md)，API 与前端见 [development.md](development.md)。
