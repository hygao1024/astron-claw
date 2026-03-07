# Astron Claw

[![Release](https://img.shields.io/github/v/release/hygao1024/astron-claw)](https://github.com/hygao1024/astron-claw/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)

AI Bot 实时对话桥接服务。服务器作为中转枢纽，Bot 端通过 WebSocket 连接，Chat 端通过 HTTP SSE 接入，根据 Token 配对并双向转发消息，支持流式回复。作为 OpenClaw Channel Plugin 运行，实现完整的消息和媒体双向传输。

```
                         ┌──────────────────────┐
Chat Client ──HTTP SSE───▶                      ◀──WebSocket── Bot Plugin (OpenClaw)
             /bridge/chat │   Bridge Server      │  /bridge/bot
                          │  (FastAPI + Redis)   │
                          │                      │
                          │  Token 配对 & 路由    │
                          │  Session 管理         │
                          │  媒体中转             │
                          └──────────────────────┘
                                   │
                          ┌────────┴────────┐
                          │  MySQL   Redis   │
                          │  持久化   缓存/路由│
                          └─────────────────┘
```

## 特性

- **OpenClaw Channel Plugin** — 原生 ChannelPlugin 接口，支持 message tool 主动发送消息
- **HTTP SSE Chat 接入** — 标准 HTTP POST + SSE 流式响应，对接成本最低
- **WebSocket Bot 桥接** — Bot 无需公网 IP，主动出站连接即可
- **多会话管理** — 支持创建/切换多个独立对话，前端 Session Drawer 可 pin 固定
- **媒体消息支持** — 图片、音频、视频、文件的上传/下载和双向传输
- **Token 管理** — 支持自定义名称、多种过期时间（1h/6h/1d/7d/30d/永不过期）
- **流式传输** — Token 级别真流式输出（onPartialReply 模式），支持思考过程、文本片段、工具调用/结果等多种消息类型
- **多 Worker 高可用** — Redis per-token/session inbox 跨 Worker 消息路由，连接状态全局可见，支持多进程 Uvicorn 部署
- **优雅关闭 & 自动重连** — 服务端滚动更新时通知客户端，Chat/Bot 自动重连并恢复会话，无需手动刷新
- **Admin 管理面板** — 密码认证、Token CRUD、在线状态监控
- **Web 聊天界面** — 内置 Chat 前端，支持文本和附件发送、会话切换
- **JSON-RPC 2.0 协议** — Bot 端和服务端之间使用标准 JSON-RPC 通信
- **高可用存储** — MySQL (SQLAlchemy ORM) 持久化 + Redis 会话缓存 + Redis inbox 跨 Worker 路由，支持 Redis 单机/集群双模式
- **链路追踪日志** — 全生命周期结构化日志，每条携带 token/session_id，支持 `grep` 端到端追踪
- **数据库版本控制** — Alembic 管理 Schema 迁移，支持升级/回滚
- **Docker 部署** — 多阶段构建镜像，启动时自动迁移数据库

## 项目结构

```
astron-claw/
├── server/                 # 服务端 (Python/FastAPI)
│   ├── app.py              # FastAPI 应用入口 & Lifespan
│   ├── run.py              # 启动入口 (Uvicorn)
│   ├── pyproject.toml      # 项目依赖 (uv)
│   ├── uv.lock             # 依赖锁文件
│   ├── pytest.ini          # 单元测试配置
│   ├── alembic.ini         # Alembic 配置
│   ├── .env.example        # 环境变量示例
│   ├── infra/              # 基础设施层
│   │   ├── config.py       # 配置加载 (.env)
│   │   ├── database.py     # MySQL 异步引擎 & 连接池
│   │   ├── cache.py        # Redis 连接管理 (单机/集群)
│   │   ├── models.py       # SQLAlchemy ORM 模型
│   │   └── log.py          # Loguru 日志配置
│   ├── services/           # 业务逻辑层
│   │   ├── bridge.py       # 连接桥接逻辑 (跨 Worker inbox 路由)
│   │   ├── session_store.py  # 会话持久化 (MySQL + Redis write-through)
│   │   ├── token_manager.py  # Token 管理 (MySQL)
│   │   ├── admin_auth.py   # Admin 认证 (MySQL + Redis session)
│   │   ├── media_manager.py  # 媒体文件管理（MySQL + 本地文件系统）
│   │   └── state.py        # 全局单例 (bridge, managers)
│   ├── routers/            # HTTP/WebSocket 路由层
│   │   ├── websocket.py    # /bridge/bot (WebSocket)
│   │   ├── sse.py          # /bridge/chat (HTTP SSE)
│   │   ├── tokens.py       # /api/token
│   │   ├── media.py        # /api/media
│   │   ├── admin.py        # /api/admin
│   │   ├── admin_auth.py   # /api/admin/auth
│   │   └── health.py       # /api/health
│   ├── migrations/         # Alembic 数据库迁移
│   │   └── versions/
│   └── tests/              # 测试
│       ├── conftest.py     # 共享 fixtures (mock session, mock redis)
│       ├── test_config.py
│       ├── test_bridge_translate.py
│       ├── test_token_manager.py
│       ├── test_media_manager.py
│       ├── test_admin_auth.py
│       ├── test_session_store.py
│       ├── test_bridge.py
│       └── e2e/            # 黑盒集成测试（需要真实服务器）
│           ├── README.md
│           └── test_integration.py
├── frontend/               # 前端
│   ├── index.html          # Chat 聊天界面（支持文本+附件）
│   ├── admin.html          # Admin 管理面板
│   └── astron_logo.png
├── plugin/                 # OpenClaw Channel Plugin (TypeScript)
│   ├── index.ts            # 入口（register + export default）
│   ├── src/
│   │   ├── constants.ts    # 插件常量
│   │   ├── types.ts        # 共享类型定义
│   │   ├── runtime.ts      # 全局状态管理
│   │   ├── config.ts       # 账号配置解析
│   │   ├── channel.ts      # ChannelPlugin 对象定义
│   │   ├── onboarding.ts   # 交互式配置流程
│   │   ├── hooks.ts        # tool_call/tool_result 钩子
│   │   ├── bridge/
│   │   │   ├── client.ts   # WebSocket 传输层（BridgeClient）
│   │   │   ├── media.ts    # 媒体上传/下载 REST API
│   │   │   └── monitor.ts  # 连接生命周期 & 健康探测
│   │   └── messaging/
│   │       ├── handlers.ts # 消息类型策略（text/image/audio/video/file）
│   │       ├── inbound.ts  # 入站消息处理 & JSON-RPC dispatch
│   │       ├── outbound.ts # 出站消息发送
│   │       └── target.ts   # 地址归一化
│   ├── openclaw.plugin.json
│   ├── tsconfig.json
│   └── package.json
├── docs/
│   └── api.md              # API 参考文档
├── scripts/
│   └── release.sh          # 插件打包脚本
├── Dockerfile              # Docker 镜像构建文件
├── .dockerignore           # Docker 构建忽略文件
├── install.sh              # 插件安装脚本（支持远程一行安装）
└── uninstall.sh            # 插件卸载脚本
```

## 快速开始

### 1. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 配置环境

```bash
cd server

# 复制环境变量模板并填写实际配置
cp .env.example .env
# 编辑 .env，填写 MySQL 和 Redis 连接信息
```

`.env` 配置项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MYSQL_HOST` | MySQL 地址 | `127.0.0.1` |
| `MYSQL_PORT` | MySQL 端口 | `3306` |
| `MYSQL_USER` | MySQL 用户名 | `root` |
| `MYSQL_PASSWORD` | MySQL 密码 | — |
| `MYSQL_DATABASE` | 数据库名 | `astron_claw` |
| `REDIS_HOST` | Redis 地址 | `127.0.0.1` |
| `REDIS_PORT` | Redis 端口 | `6379` |
| `REDIS_PASSWORD` | Redis 密码 | — |
| `REDIS_DB` | Redis DB 编号（集群模式忽略） | `0` |
| `REDIS_CLUSTER` | 是否使用 Redis 集群模式 | `false` |
| `SERVER_HOST` | 服务监听地址 | `0.0.0.0` |
| `SERVER_PORT` | 服务监听端口 | `8765` |
| `SERVER_WORKERS` | Worker 进程数 | `CPU 核数 + 1` |
| `SERVER_LOG_LEVEL` | 日志级别 | `info` |
| `SERVER_ACCESS_LOG` | 是否启用访问日志 | `true` |

### 3. 安装依赖 & 初始化数据库

```bash
# 安装依赖（自动创建 .venv）
uv sync

# 执行数据库迁移（自动创建数据库和表）
uv run alembic upgrade head
```

### 4. 启动服务端

```bash
uv run python3 run.py
```

服务启动后：
- 聊天界面：`http://localhost:8765/`
- 管理面板：`http://localhost:8765/admin`（首次访问需设置密码）
- 健康检查：`http://localhost:8765/api/health`

### 5. 安装 OpenClaw 插件

在 Bot 所在的机器上一行命令安装（从 GitHub Release 自动下载）：

```bash
curl -fsSL https://raw.githubusercontent.com/hygao1024/astron-claw/main/install.sh | bash -s -- \
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

### 6. 卸载插件

```bash
# 远程执行
curl -fsSL https://raw.githubusercontent.com/hygao1024/astron-claw/main/uninstall.sh | bash -s -- -y

# 或本地执行
./uninstall.sh       # 交互式确认
./uninstall.sh -y    # 静默卸载
```

## Docker 部署

```bash
# 构建镜像
docker build -t astron-claw .

# 运行（需提供 MySQL 和 Redis 连接信息）
docker run -d \
  --name astron-claw \
  -p 8765:8765 \
  -e MYSQL_HOST=<mysql-ip> \
  -e MYSQL_PASSWORD=<password> \
  -e REDIS_HOST=<redis-ip> \
  astron-claw
```

容器启动时自动执行 `alembic upgrade head` 迁移数据库，内置健康检查（`/api/health`，30s 间隔）。

## 数据库迁移

使用 Alembic 管理数据库 Schema 版本：

```bash
cd server

# 查看当前版本
uv run alembic current

# 修改 infra/models.py 后自动生成迁移
uv run alembic revision --autogenerate -m "描述变更内容"

# 升级到最新版本
uv run alembic upgrade head

# 回退一个版本
uv run alembic downgrade -1

# 查看迁移历史
uv run alembic history
```

## 单元测试

```bash
cd server
uv run pytest -v
```

E2E 黑盒集成测试（需要先启动服务）：

```bash
python3 server/tests/e2e/test_integration.py
```

## API 概览

详细文档见 [docs/api.md](docs/api.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查（MySQL + Redis 连通性） |
| `POST` | `/api/token` | 创建 Token |
| `POST` | `/api/token/validate` | 验证 Token |
| `GET` | `/api/admin/auth/status` | 查询 Admin 认证状态 |
| `POST` | `/api/admin/auth/setup` | 首次设置管理员密码 |
| `POST` | `/api/admin/auth/login` | 管理员登录 |
| `POST` | `/api/admin/auth/logout` | 管理员登出 |
| `GET` | `/api/admin/tokens` | 获取 Token 列表 |
| `POST` | `/api/admin/tokens` | 创建 Token（支持名称和过期时间） |
| `PATCH` | `/api/admin/tokens/{token}` | 更新 Token 名称/过期时间 |
| `DELETE` | `/api/admin/tokens/{token}` | 删除 Token |
| `POST` | `/api/admin/cleanup` | 清理过期 Token 和媒体文件 |
| `POST` | `/api/media/upload` | 上传媒体文件 |
| `GET` | `/api/media/download/{media_id}` | 下载媒体文件 |
| `POST` | `/bridge/chat` | Chat 对话（HTTP SSE 流式响应） |
| `GET` | `/bridge/chat/sessions` | 获取 Chat 会话列表（HTTP） |
| `POST` | `/bridge/chat/sessions` | 创建 Chat 会话（HTTP） |
| WebSocket | `/bridge/bot` | Bot 端连接 |

## 技术栈

- **服务端**：Python 3.11+ / FastAPI / Uvicorn (uvloop + httptools)
- **数据库**：MySQL (SQLAlchemy ORM + Alembic) / Redis (单机 + 集群)
- **日志**：Loguru 结构化日志 + 链路追踪
- **前端**：原生 HTML / CSS / JavaScript（highlight.js 代码高亮）
- **插件**：TypeScript / WebSocket (ws) / OpenClaw ChannelPlugin SDK
- **协议**：HTTP SSE + WebSocket (Bot) + JSON-RPC 2.0
- **部署**：Docker 多阶段构建 / uv 包管理

## License

MIT
