# Astron Claw

AI Bot 实时对话桥接服务。服务器作为中转枢纽，Bot 端和 Chat 端分别通过 WebSocket 连接，根据 Token 配对并双向转发消息，支持流式回复。

```
Chat Client ──WebSocket──▶ Bridge Server ◀──WebSocket── Bot Plugin (OpenClaw)
              /bridge/chat    (Token 配对)     /bridge/bot
```

## 特性

- **WebSocket 双向桥接** — Bot 无需公网 IP，主动出站连接即可
- **Token 管理** — 支持自定义名称、多种过期时间（1h/6h/1d/7d/30d/永不过期）
- **流式传输** — 支持思考过程、文本片段、工具调用等多种消息类型
- **多会话管理** — 支持创建多个独立会话并自由切换，每个会话上下文隔离
- **Admin 管理面板** — 密码认证、Token CRUD、在线状态监控
- **Web 聊天界面** — 内置 Chat 前端，输入 Token 即可与 Bot 对话，左侧抽屉管理会话（支持固定/浮动模式）
- **OpenClaw 插件** — 一键安装，桥接本地 OpenClaw Gateway

## 项目结构

```
astron-claw/
├── server/                 # 服务端 (Python/FastAPI)
│   ├── app.py              # 路由 & WebSocket 端点
│   ├── bridge.py           # 连接桥接逻辑
│   ├── token_manager.py    # Token 管理 (SQLite)
│   ├── admin_auth.py       # Admin 认证
│   ├── run.py              # 启动入口
│   └── requirements.txt
├── frontend/               # 前端
│   ├── index.html          # Chat 聊天界面
│   ├── admin.html          # Admin 管理面板
│   └── astron_logo.png
├── plugin/                 # OpenClaw 插件 (Node.js)
│   ├── dist/index.js       # 插件主逻辑
│   ├── openclaw.plugin.json
│   └── package.json
├── docs/
│   └── api.md              # API 参考文档
├── scripts/
│   └── release.sh          # 插件打包脚本
├── install.sh              # 插件安装脚本（支持远程一行安装）
├── uninstall.sh            # 插件卸载脚本
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
| `POST` | `/api/admin/cleanup` | 清理过期 Token |
| WebSocket | `/bridge/bot` | Bot 端连接 |
| WebSocket | `/bridge/chat` | Chat 端连接（支持多会话切换） |

## 技术栈

- **服务端**：Python 3 / FastAPI / Uvicorn / SQLite
- **前端**：原生 HTML / CSS / JavaScript
- **插件**：Node.js / WebSocket (ws)
- **协议**：WebSocket + JSON-RPC 2.0

## License

MIT
