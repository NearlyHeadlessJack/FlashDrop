# 跨平台兼容性：Windows ↔ macOS

FlashDrop 的传输层与操作系统无关：控制消息、数据帧、CRC 校验和续传格式都是平台中立的。本文记录两端在文件名、权限、原生选择器、浏览器和网络上的实际差异。

> **验证状态**：Windows 主机上的完整双实例传输、WebUI 和续传已实测；POSIX 权限与 Unicode 路径也在 Linux/WSL2 原生文件系统上通过了自动化测试。macOS 支持代码已加入，但当前开发环境没有真实 Mac，尚未进行 macOS 实机验收。首次在 Mac 上使用时，建议先跑一个包含空文件、中文名、可执行脚本和嵌套目录的小任务。

## 1. 快速结论

| 场景 | 状态 |
|---|---|
| Windows → macOS 普通文件/文件夹 | ✅ 可用 |
| macOS → Windows 普通文件/文件夹 | ✅ 可用；Windows 非法文件名会在接收端自动适配 |
| macOS → macOS 可执行脚本 | ✅ 保留 rwx 权限位（需要 POSIX 发送/接收端） |
| macOS → Windows 可执行权限 | ⚠️ Windows 没有 POSIX 可执行位，文件仍可复制，但执行语义丢失 |
| 跨平台断点续传 | ✅ 续传状态在接收端，与发送 OS 无关 |
| macOS 无 Tkinter | ✅ 使用 osascript 原生选择器；再失败会提示手动路径 |
| Safari WebUI | ✅ 现代 Safari 可用；核心流程不依赖 Chromium 特性 |
| IPv6-only 局域网 | ❌ 当前仅 IPv4 |

## 2. macOS 运行要求

- Python **3.10+**。
- 无第三方 Python 依赖。
- 原生文件选择优先通过系统自带的 `/usr/bin/osascript` 调用 Finder 风格对话框，因此 **Homebrew Python 没有 `tkinter` 也可以使用**。
- 如果 `osascript` 被自动化权限拦截，才回退到 Tk；两种方式都不可用时仍可手动输入路径。
- 双击启动：

```bash
chmod +x start.command
open start.command
```

或直接在终端：

```bash
python3 -m flashdrop
```

- `start.command` 使用 zsh 和 `python3`，会打开默认浏览器。
- 第一次接收连接时，macOS 可能提示是否允许 Python 接受传入网络连接。请在「系统设置 → 网络/防火墙」允许。
- macOS 14/15 还可能在「隐私与安全 → 本地网络」请求授权；若其他设备连不上，检查终端/Python 是否有本地网络权限。
- 两台电脑需要在同一可互访的 IPv4 局域网。公司 Wi-Fi 的客户端隔离、访客网络会阻断。

## 3. 文件名与 Unicode

### 3.1 发送端统一为 NFC 和 POSIX 分隔符

- 线协议中的相对路径只使用 `/` 分隔。
- macOS 文件系统经常返回分解形式（NFD）的 Unicode；发送端在进入清单前统一为 **NFC**，文件 ID 也按 NFC + casefold 计算。
- 反斜杠在 macOS/Linux 是合法文件名字符，不会被当作分隔符；接收端是 Windows 时会进行下面所述的适配。
- 大小写折叠用于避免 macOS/Windows 大小写不敏感文件系统上的冲突。发送端遇到同名文件会自动生成 ` (2)` 等后缀。

### 3.2 Windows 接收端的自动名称适配

把 macOS 文件发到 Windows 时，接收端在**传输开始前**会为每个目标路径生成安全名称：

- 把 `\ : * ? " < > |` 和控制字符替换为 `_`；`/` 是路径分隔符，不会出现在文件名组件中。
- 去掉文件名末尾的空格和点。
- 避开 `CON`、`PRN`、`AUX`、`NUL`、`COM1`–`COM9`、`LPT1`–`LPT9` 等 Windows 保留名。
- 过长的单个名称会被截短，并加一个稳定短哈希。
- 发生变化的名字会追加 `~<8位十六进制>`，哈希由原名和父路径决定，因而在重复连接、重新选目录时保持稳定。
- 同一次清单若出现目标路径冲突，接收端会**拒绝开始**并说明冲突，而不是覆盖文件。

示例：

```text
macOS 源名        → Windows 保存名
报告:终稿?.pdf    → 报告_终稿_~a1b2c3d4.pdf
CON.txt           → _CON~e5f6a7b8.txt
项目/资料\备份    → 项目/资料_备份~1234abcd
```

接收端文件清单在 Windows 上显示的是**实际保存名称**，并在文件行上标记「名称已适配」；保存位置上方会显示适配数量和前三条示例。其他平台接收时不会改名。

> 接收端重命名的预览只在 Windows 上出现。发送端仍显示源端路径。

### 3.3 长路径

Windows 默认可能只支持 260 字符的完整路径。接收端会统计保存根目录加目标相对路径的长度，超过 240 个字符时在审核页给出警告。该警告不会阻断传输（启用了 LongPathsEnabled 的系统可能成功），但如果最终保存失败，建议：

- 换一个更短的保存目录；
- 将深目录打包成 zip；
- 在 Windows 上启用长路径支持。

## 4. 权限与元数据

- POSIX 发送端（macOS/Linux）会附带每个文件的 `rwx` 权限位。
- POSIX 接收端在原子落盘后恢复这三位权限；因此 macOS → macOS 的 `.sh`、可执行文件和部分工具链文件可以保留可执行位。
- 只保留 `rwx`，不保留 setuid/setgid/sticky 位。
- Windows 发送端没有 POSIX 权限位，清单中省略 `mode`；macOS 接收后得到系统默认权限。
- Windows 接收端忽略 POSIX 权限位。

**不会传输/恢复**：

- 符号链接（目录链接会被跳过，文件链接会复制目标内容）；
- macOS 扩展属性、Finder 标签、资源叉、quarantine；
- ACL、文件所有者；
- Windows 的备用数据流和创建时间；
- 空目录。

`.app` 应用包、Framework、包含符号链接或资源叉的工程/工具，建议先打包成 `.zip` 再传。这样可以保留包结构、符号链接和更多元数据。

## 5. 原生选择器

| 平台 | 首选 | 后备 |
|---|---|---|
| macOS | `osascript` + Standard Additions（多文件、文件夹、保存目录） | Tk；再失败则手动路径 |
| Windows / Linux | Tkinter | 手动路径 |

- osascript 只读取系统原生选择结果，不依赖第三方库。
- 如果 macOS 阻止自动化/Apple Events，会在错误里显示原因，并尝试 Tk。
- 无桌面/SSH 环境请使用「手动输入路径」。

## 6. WebUI 与 Safari

- 使用系统字体栈：macOS 上优先 `-apple-system` / SF Pro / PingFang SC / Hiragino Sans GB。
- 已为 Safari 加入 `-webkit-backdrop-filter`、`-webkit-mask-image`，并移除了对 `color-mix()` 的依赖。
- 对话框使用原生 `<dialog>`；现代 Safari 支持。
- WebUI 通过本机回环地址 `127.0.0.1` 访问，不依赖外网字体或 CDN。
- Chromium/Edge 已实测；Safari 未在当前环境实机测试。若遇到旧 Safari 的样式降级，可使用 Chrome/Edge 打开同一地址，传输逻辑不变。

## 7. 断点续传与系统差异

- 续传记录在接收端保存目录的 `.flashdrop` 中，与发送端 OS 无关。
- 重新连接时必须选择同一个保存目录。
- 路径匹配包含 NFC 形式、大小写、大小、mtime 和快速指纹。
- 由于本版本将路径统一为 NFC，旧版本在分解 Unicode 名称上生成的部分续传记录可能不会再匹配；程序会安全地从头开始对应文件，不会覆盖不完整内容。
- Windows 名称适配是稳定函数；同源文件重新连接会得到同一个安全目标名。

## 8. 网络兼容性

- 仅支持 IPv4：发送端监听 `0.0.0.0`，接收端通过控制连接的对端 IPv4 连数据端口。
- macOS 通常同时有 Wi-Fi 和 Thunderbolt/虚拟网卡；发送端会列出多个地址，优先选与对方同网段的那个。
- Windows 防火墙按 Python 程序放行；macOS 允许 Python 的传入连接，并检查本地网络隐私权限。
- 同一 Wi-Fi 下如出现连接超时，可先 `ping` 对方 IPv4，或暂时用手机热点交叉验证。

## 9. 已知不支持

- 空目录、符号链接语义、ACL/所有者、扩展属性/资源叉。
- IPv6-only 或需要 NAT 穿透的公网场景。
- 加密、认证和公网安全暴露。
- Windows 未启用长路径时的超长路径。
- 把 `.app`/Framework 当成“普通目录”传输后的完整可执行性（请用 zip）。
- 在 macOS 上真正实机验证：当前版本已经写好支持路径，但需要一次真实 Mac 测试作为最终验收。

## 10. 建议的验收清单

1. Windows → macOS：中文名、空格、嵌套目录、空文件、一个 >1 GB 文件。
2. macOS → Windows：带重音的文件名、带 `:`/`?` 的文件名、`CON.txt`、深层目录。
3. macOS 上没有 Tkinter 时，文件选择是否仍弹出 Finder 风格窗口。
4. 执行 `chmod 755 run.sh` 的脚本从 macOS 发到 macOS 后是否仍是可执行。
5. 中断后重新连接同一目录，确认续传 offset 和 `.flashdrop` 行为。
6. Safari 与 Edge 各打开一次 WebUI。
