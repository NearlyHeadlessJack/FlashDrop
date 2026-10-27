# FlashDrop 传输协议 v1

本文是收发两端必须共同遵守的线协议。实现入口：

- 帧与路径校验：`flashdrop/protocol.py`
- 控制通道与心跳：`flashdrop/control.py`
- 发送端：`flashdrop/sender.py`
- 接收端：`flashdrop/receiver.py`

修改任何字段、时序或语义时，**升级协议版本**，并同步本文件、发送端、接收端和测试。

## 1. 传输层次

```text
TCP 控制连接（1 条）        → 长度前缀 JSON 消息
TCP 数据连接（1–8 条）      → JSON 握手后切换为二进制帧
```

- 发送端监听一个控制端口和 N 个数据端口。
- 接收端连接控制端口，拿到清单和数据端口列表后，等双方都同意才开始传输。
- 数据端口由发送端动态分配，通过 `welcome` 消息告诉接收端；不需要预先固定连续区间。
- 文件字节只在数据连接上传输。控制连接不承载文件 payload。
- `data_token` 只用于避免连接串线，**不是安全认证**。

## 2. 控制帧

```text
+----------------+---------------------------------+
| 4 字节大端长度  | UTF-8 JSON 对象（长度 = N）      |
+----------------+---------------------------------+
```

- 长度字段不包含自身 4 字节。
- 最大长度：`MAX_CONTROL_FRAME = 128 MiB`。
- JSON 必须是一个对象，且 `type` 是字符串。
- 不合法帧长度、非 JSON、缺少 `type` → 立即关闭本连接。
- `ping`/`pong` 在控制层自动处理，不交给业务状态机。

连接建立后设置 TCP_NODELAY 和 keepalive。应用层心跳：

| 项 | 值 |
|---|---|
| 心跳发送间隔 | 5 秒 |
| 心跳判定超时 | 45 秒无任何入站消息 |
| 控制握手超时 | 12 秒 |

## 3. 数据帧

```text
偏移  长度  字段
0     4    magic = b"FDX1"
4     16   file id（16 字节原始值）
20    8    offset（uint64 大端，文件绝对偏移）
28    4    length（uint32 大端，payload 字节数）
32    4    crc32（uint32 大端，仅对 payload）
36    N    payload（原始文件字节）
```

Python 格式：`struct.Struct("!4s16sQII")`，头长 36 字节。

- `length > 0` 且不能超过双方的 `chunk_size`；当前 chunk_size 默认 4 MiB，协议上限 32 MiB。
- `offset + length <= manifest.size`，否则接收端报错。
- `crc32 = zlib.crc32(payload) & 0xFFFFFFFF`。
- 数据帧没有 session 字段；会话绑定发生在数据 socket 的 `data_hello`。
- 没有显式的文件结束帧。接收端依据 manifest 的文件大小和连续写入进度判断完整性。
- 文件顺序、块到达顺序可以不同；接收端只提交连续前缀，并缓存有限数量的乱序块。

## 4. 完整时序

```text
接收方                                               发送方
   |                                                    |
   | TCP connect (control port)                          |
   |---------------- hello ----------------------------->|
   |<--------------- welcome ----------------------------|
   |  [UI：展示清单、选择保存目录、扫描续传]                 |
   |---------------- prepare {session_id, resume} ------>|
   |<--------------- ready {accepted_resume,...} --------|
   |---------------- start ----------------------------->|
   |<--------------- started {data_ports,...} -----------|
   |                                                    |
   |  对每个数据端口并行：                                 |
   |---- TCP connect (data port) ----------------------->|
   |---------------- data_hello {token,index} ---------->|
   |<--------------- data_ok {index} --------------------|
   |                                                    |
   |---------------- data_ready {connected,failed} ---->|
   |                                                    |
   |<===== data frames（多条连接并行，双向无应用 ACK）======|
   |                                                    |
   |<--------------- progress (每秒) --------------------|
   |  等数据 EOF + all_sent                              |
   |<--------------- all_sent ---------------------------|
   |  [校验、fsync、os.replace]                          |
   |---------------- completed {ok:true,...} ----------->|
   |<--------------- done -------------------------------|
   |  close control                                     |
```

发送端只有在收到 `data_ready` 后才启动数据 worker。接收端在发 `data_ready` 之前必须已经启动数据读线程，避免握手完成后数据堆在 socket 缓冲区里。

## 5. 控制消息

所有消息都至少包含 `type`。未列出的字段视为扩展；未知的 `type` 目前被忽略并记录 debug 日志。

### 5.1 `hello`（接收方 → 发送方）

```json
{
  "type": "hello",
  "protocol": 1,
  "app": "flashdrop",
  "app_version": "0.1.0"
}
```

发送方接受 `protocol` 为 `1` 或缺失的 hello，随后发送 `welcome`。

### 5.2 `welcome`（发送方 → 接收方）

```json
{
  "type": "welcome",
  "protocol": 1,
  "app": "flashdrop",
  "app_version": "0.1.0",
  "session_id": "32-hex",
  "data_token": "32-hex",
  "data_ports": [45871, 45872, 45873, 45874],
  "connections": 4,
  "chunk_size": 4194304,
  "sender_name": "DESKTOP-ABC",
  "peer": "192.168.1.23:52344",
  "manifest": {
    "count": 2,
    "total_size": 1234567,
    "files": [
      {
        "id": "32-hex",
        "path": "项目归档/a.bin",
        "size": 1234567,
        "mtime_ns": 1760000000000000000,
        "quick_hash": "40-hex"
      }
    ]
  }
}
```

`path` 必须是相对路径，使用 `/` 分隔。接收端使用 `safe_transfer_path` 再验证：

- 拒绝绝对路径、Windows 盘符、空路径、NUL、`..`；
- 顶层 `.flashdrop` 是保留目录；
- 不允许重复 file id / 重复路径（路径比较 casefold）。

`manifest.total_size` 必须等于所有 `size` 之和。`data_ports` 非空、去重、范围 1–65535。

### 5.3 `prepare`（接收方 → 发送方）

```json
{
  "type": "prepare",
  "session_id": "32-hex",
  "resume": {
    "32-hex-file-id": 104857600
  }
}
```

- `resume` 只包含需要续传且 offset > 0 的文件；缺省即从 0 开始。
- offset 必须满足 `0 <= offset <= size`。
- 文件已经全部存在时 offset 可以等于 size，发送方不会发送该文件的任何数据。
- 接收方必须使用 `session_id` 匹配当前控制会话。

### 5.4 `ready`（发送方 → 接收方）

```json
{
  "type": "ready",
  "accepted_resume": { "32-hex-file-id": 104857600 },
  "base_bytes": 104857600,
  "new_bytes": 90000000,
  "files": 2,
  "total_size": 194857600
}
```

- `accepted_resume` 是发送方实际接受的非零 offset。
- 接收方会严格比较它和本地提出的续传 map；不一致即拒绝开始。这是为了防止两端对“哪些字节已存在”的认知分叉。
- `new_bytes = sum(size - offset)`。

### 5.5 `start`（接收方 → 发送方）

```json
{ "type": "start" }
```

前提：已收到 `ready`。

### 5.6 `started`（发送方 → 接收方）

```json
{
  "type": "started",
  "data_ports": [45871, 45872, 45873, 45874],
  "connections": 4,
  "chunk_size": 4194304
}
```

接收方收到此消息后才开始连数据端口。

### 5.7 数据 socket 握手

接收方在每条数据连接上发送同一个长度前缀 JSON 格式：

```json
{ "type": "data_hello", "token": "32-hex", "index": 0 }
```

发送方验证 token，注册该 socket，回复：

```json
{ "type": "data_ok", "index": 0 }
```

之后该连接只发送二进制数据帧。发送方不会在 `data_ready` 之前发送数据。

### 5.8 `data_ready`（接收方 → 发送方）

```json
{ "type": "data_ready", "connected": 4, "failed": 0 }
```

- 接收方已经为成功的连接启动读线程。
- 发送方据此启动 worker。少于请求数的连接是允许的；`connected == 0` 会失败。
- 数据端口建立可能有部分失败；UI 会显示实际连接数。

### 5.9 `progress`（发送方 → 接收方，信息性）

```json
{
  "type": "progress",
  "done": 52428800,
  "total": 194857600,
  "base": 104857600,
  "new_bytes": 52428800,
  "new_total": 90000000,
  "percent": 26.91,
  "speed": 73400320.0,
  "eta": 8,
  "elapsed": 1.4,
  "files_total": 2,
  "connections": 4
}
```

每秒最多一条。接收端以本地落盘进度为准；该消息用于未来扩展和信息展示，不影响完成判定。

### 5.10 `all_sent`（发送方 → 接收方）

```json
{
  "type": "all_sent",
  "sent": 90000000,
  "total": 194857600,
  "elapsed": 12.6
}
```

表示所有计划中的新字节已经写入数据 socket，且发送端已完成源文件的大小/mtime 复查。接收端不能立刻认为成功：还需要等所有数据连接 EOF、无 reader 错误、所有文件连续完成。

### 5.11 `saving`（接收方 → 发送方，信息性）

```json
{ "type": "saving", "message": "接收方正在校验并保存文件" }
```

发送端当前忽略此消息，状态仍为 `finishing`。

### 5.12 `completed`（接收方 → 发送方）

成功：

```json
{
  "type": "completed",
  "ok": true,
  "files": 2,
  "bytes": 194857600,
  "elapsed": 15.2
}
```

失败：

```json
{
  "type": "completed",
  "ok": false,
  "error": "有 1 个文件保存失败：...",
  "files": 1
}
```

成功意味着接收方已经完成每个文件的 `fsync + os.replace`。失败时已成功的文件保留，失败文件的续传记录保留。发送方收到成功后才标记 `completed`。

### 5.13 `cancel`（接收方 → 发送方）

```json
{ "type": "cancel" }
```

- 在 review 阶段：发送方关闭控制连接，回到 `listening`。
- 在已开始传输后：发送方中止本次传输，状态 `cancelled`。
- 消息是尽力发送；接收方随后会关闭控制连接。

### 5.14 `aborted` / `error`

发送方中止传输：

```json
{ "type": "aborted", "message": "发送方已停止" }
```

任一方发现错误：

```json
{ "type": "error", "message": "数据块校验失败" }
```

收到后应关闭数据连接、保留接收端分片，并把会话置为错误。`error` 可能用于发送方拒绝非法握手。

### 5.15 `done`（发送方 → 接收方）

```json
{ "type": "done" }
```

发送方在收到成功的 `completed` 后尽力发送，然后关闭控制连接。接收方可以忽略它（已经完成了本地状态）。

### 5.16 `ping` / `pong`

```json
{ "type": "ping", "t": 1760000000.123 }
{ "type": "pong", "t": 1760000000.123 }
```

由 `ControlChannel` 自动处理，不进入业务回调。超过 45 秒没有收到任何消息即关闭连接。

## 6. 续传协商细节

1. 接收端扫描 `<save>/.flashdrop/state/<id>.json`。
2. 仅当 `id/path/size/mtime_ns/quick_hash` 全部与本次 manifest 匹配时才考虑复用。
3. `partial` 且 part 文件存在、长度 `0 < n <= size` → offset = n。
4. `done` 且目标文件存在、大小匹配 → offset = size（整文件跳过）。
5. 发送端接受 offset，返回 `accepted_resume`。
6. 接收端必须得到完全一致的 accepted map，否则拒绝开始。
7. 发送端从 `resume_offset` 开始调度；接收端只接受从该偏移连续的写入。
8. 当前版本没有全文件哈希协商字段；offset 的可信基础是快速指纹和本地 sidecar 校验。

## 7. 错误与超时

| 条件 | 处理 |
|---|---|
| 控制长度非法 / JSON 非法 | 关闭控制连接 |
| 控制握手超过 12s | 关闭 |
| 心跳 45s 无入站 | 关闭 |
| 数据连接/握手超过 10s | 该连接失败，继续尝试其他端口 |
| 数据读 120s 无数据 | 该数据线程失败 |
| 接收方 30s 没等到 `started` | 中止 |
| 数据帧 magic/length/CRC/file id 非法 | 接收端中止 |
| 数据块重复/重叠 | 接收端中止（不做隐式重传） |
| 发送端数据 worker 异常 | 关闭数据连接，通知接收端错误 |
| 发送端源文件变化 | 发送端在打开时/结束后检查，失败不确认 |
| 接收端无法保存 | 最终化失败，`completed ok=false` |

## 8. 版本与兼容

- 当前 `PROTOCOL_VERSION = 1`。
- 接收端在 `welcome` 中拒绝非 `1` 的协议。
- 发送端接受缺失或 `1` 的 hello。
- 新增可选消息或可选字段可以保持 v1，但涉及完成语义、续传匹配、数据帧布局的改动必须升 v2。
- 不得依赖“先到块一定先完成”：并行数据连接意味着接收端必须能处理乱序。

## 9. 安全说明

协议没有 TLS、认证、MAC 或签名。`data_token` 只防止错误串线。控制端口绑定 `0.0.0.0` 并接受第一个合法控制连接；任何能连接它的客户端都可以看到文件清单、自行选择保存目录并开始接收。请在可信局域网使用，或在外层使用 VPN/防火墙。
