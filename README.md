# 闪传 FlashDrop

局域网点对点文件快传工具。文件在两台电脑之间直接传输，不经过云盘。

## 下载

不想装 Python 的话，可以直接用 GitHub Releases 里的单文件版本（推送 `v*` 标签时由 CI 自动构建）：

| 平台 | 文件 |
|---|---|
| Windows x64 | `flashdrop-windows-amd64.exe` |
| macOS Apple 芯片 | `flashdrop-macos-arm64` |
| macOS Intel | `flashdrop-macos-x86_64` |

Windows 双击即可运行，地址会打印在控制台窗口里。

macOS 下载后需要补上执行权限、去掉隔离属性，否则会被 Gatekeeper 拦下：

```bash
chmod +x flashdrop-macos-arm64
xattr -d com.apple.quarantine flashdrop-macos-arm64
./flashdrop-macos-arm64
```

## 启动

要求 Python 3.10 以上，不需要安装第三方依赖。

### Windows

```powershell
cd C:\Users\jack\flash
python -m flashdrop
```

也可以直接双击 `start.bat`。

### macOS

```bash
cd /path/to/flash
chmod +x start.command
open start.command
```

或：

```bash
python3 -m flashdrop
```

启动后浏览器会自动打开本地控制台。首次传输时，请允许 Python 通过防火墙；macOS 还需允许 Python 接收传入连接。

## 使用

### 发送文件

1. 点击「我要发送」。
2. 选择文件或文件夹，可预览、继续添加或删除。
3. 点击「开启传输」。
4. 把页面显示的 **本机 IP** 和 **信息传输端口** 告诉接收方。
5. 等待对方连接并确认接收。

### 接收文件

1. 点击「我要接收」。
2. 填写发送方的 IP 和端口，点击「尝试连接」。
3. 查看对方准备发送的文件清单。
4. 选择本机保存文件夹。
5. 点击「开始接收」。

发送端和接收端需要在同一局域网内。若连接失败，先检查防火墙和 IP 地址。

## 断点续传

传输中断后，重新连接并在接收端选择**同一个保存文件夹**，程序会自动跳过已经接收的字节。

## 注意

- 当前版本不加密、不认证，只适合在可信局域网使用。
- macOS ↔ Windows 的文件名、权限等兼容说明见 [docs/compatibility.md](docs/compatibility.md)。
- 完整文档入口：[AGENTS.md](AGENTS.md)、[docs/architecture.md](docs/architecture.md)、[docs/protocol.md](docs/protocol.md)、[docs/operations.md](docs/operations.md)。
