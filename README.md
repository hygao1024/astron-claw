# Astron Claw

AI Bot 实时对话桥接服务。服务器作为中转枢纽，Bot 端和 Chat 端分别通过 WebSocket 连接，根据 Token 配对并双向转发消息，支持流式回复。作为 OpenClaw Channel Plugin 运行，实现完整的消息和媒体双向传输。

```
Chat Client ──WebSocket──▶ Bridge Server ◀──WebSocket── Bot Plugin (OpenClaw)
              /bridge/chat    (Token 配对)     /bridge/bot
```

## 特性

- **OpenClaw Channel Plugin** — 原生 ChannelPlugin 接口，支持 message tool 主动发送消息
- **WebSocket 双向桥接** — Bot 无需公网 IP，主动出站连接即可
- **多会话管理** — 支持创建/切换多个独立对话，前端 Session Drawer 可 pin 固定
- **媒体消息支持** — 图片、音频、视频、文件的上传/下载和双向传输
- **Token 管理** — 支持自定义名称、多种过期时间（1h/6h/1d/7d/30d/永不过期）
- **流式传输** — Token 级别真流式输出（onPartialReply 模式），支持思考过程、文本片段、工具调用/结果等多种消息类型
- **Admin 管理面板** — 密码认证、Token CRUD、在线状态监控
- **Web 聊天界面** — 内置 Chat 前端，支持文本和附件发送、会话切换
- **JSON-RPC 2.0 协议** — Bot 端和服务端之间使用标准 JSON-RPC 通信

## 项目结构

```
astron-claw/
├── server/                 # 服务端 (Python/FastAPI)
│   ├── app.py              # 路由 & WebSocket 端点
│   ├── bridge.py           # 连接桥接逻辑
│   ├── token_manager.py    # Token 管理 (SQLite)
│   ├── admin_auth.py       # Admin 认证
│   ├── media_manager.py    # 媒体文件管理（上传/下载/过期清理）
│   ├── run.py              # 启动入口
│   └── requirements.txt
├── frontend/               # 前端
│   ├── index.html          # Chat 聊天界面（支持文本+附件）
│   ├── admin.html          # Admin 管理面板
│   └── astron_logo.png
├── plugin/                 # OpenClaw Channel Plugin (Node.js)
│   ├── dist/index.js       # ChannelPlugin 实现
│   ├── openclaw.plugin.json
│   └── package.json
├── docs/
│   └── api.md              # API 参考文档
├── scripts/
│   └── release.sh          # 插件打包脚本
├── install.sh              # 插件安装脚本（支持远程一行安装）
├── uninstall.sh            # 插件卸载脚本
├── test_streaming.py       # 流式传输测试（Bridge 层）
├── test_e2e_streaming.py   # 端到端流式测试（真实插件）
└── test_integration.py     # 集成测试
```

## 快速开始

### 1. 启动服务端

```bash
# 安装依赖
cd server
pip install -r requirements.txt

# 启动服务 (默认端口 8765)
python3 run.py
```

服务启动后：
- 聊天界面：`http://localhost:8765/`
- 管理面板：`http://localhost:8765/admin`（首次访问需设置密码）

### 2. 安装 OpenClaw 插件

在 Bot 所在的机器上一行命令安装（从 GitHub Release 自动下载）：

```bash
curl -fsSL https://raw.githubusercontent.com/hygao1024/astron-claw/master/install.sh | bash -s -- \
  --bot-token <token> --server-url ws://<server-ip>:8765/bridge/bot
```

如果已克隆仓库，也可以直接运行本地脚本：

```bash
./install.sh --bot-token <token> --server-url ws://<server-ip>:8765/bridge/bot
```

| 参数 | 说明 |
|------|------|
| `--bot-token` | 在 Admin 面板生成的 Token（必填） |
| `--server-url` | Bridge 服务 WebSocket 地址（默认 `ws://localhost:8765/bridge/bot`） |
| `--target-dir` | 插件安装目录（默认 `~/.openclaw/extensions/astron-claw`） |
| `--version` | Release 版本标签（默认 `latest`，仅远程模式） |

### 3. 卸载插件

```bash
# 远程执行
curl -fsSL https://raw.githubusercontent.com/hygao1024/astron-claw/master/uninstall.sh | bash -s -- -y

# 或本地执行
./uninstall.sh       # 交互式确认
./uninstall.sh -y    # 静默卸载
```

## API 概览

详细文档见 [docs/api.md](docs/api.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/token` | 创建 Token |
| `POST` | `/api/token/validate` | 验证 Token |
| `GET` | `/api/admin/tokens` | 获取 Token 列表 |
| `POST` | `/api/admin/tokens` | 创建 Token（支持名称和过期时间） |
| `PATCH` | `/api/admin/tokens/{token}` | 更新 Token 名称/过期时间 |
| `DELETE` | `/api/admin/tokens/{token}` | 删除 Token |
| `POST` | `/api/admin/cleanup` | 清理过期 Token 和媒体文件 |
| `POST` | `/api/media/upload` | 上传媒体文件 |
| `GET` | `/api/media/download/{media_id}` | 下载媒体文件 |
| WebSocket | `/bridge/bot` | Bot 端连接 |
| WebSocket | `/bridge/chat` | Chat 端连接（支持多会话切换） |

## 技术栈

- **服务端**：Python 3 / FastAPI / Uvicorn / SQLite
- **前端**：原生 HTML / CSS / JavaScript
- **插件**：Node.js / WebSocket (ws) / OpenClaw ChannelPlugin SDK
- **协议**：WebSocket + JSON-RPC 2.0

## License

MIT
