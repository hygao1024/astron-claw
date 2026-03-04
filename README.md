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
- **多 Worker 高可用** — Redis Pub/Sub 跨 Worker 消息路由，连接状态全局可见，支持多进程 Uvicorn 部署
- **优雅关闭 & 自动重连** — 服务端滚动更新时通知客户端，Chat/Bot 自动重连并恢复会话，无需手动刷新
- **Admin 管理面板** — 密码认证、Token CRUD、在线状态监控
- **Web 聊天界面** — 内置 Chat 前端，支持文本和附件发送、会话切换
- **JSON-RPC 2.0 协议** — Bot 端和服务端之间使用标准 JSON-RPC 通信
- **高可用存储** — MySQL (SQLAlchemy ORM) 持久化 + Redis 会话缓存 + Redis Pub/Sub 跨 Worker 通信，支持 Redis 单机/集群双模式
- **数据库版本控制** — Alembic 管理 Schema 迁移，支持升级/回滚

## 项目结构

```
astron-claw/
├── server/                 # 服务端 (Python/FastAPI)
│   ├── app.py              # 路由 & WebSocket 端点 & Lifespan
│   ├── bridge.py           # 连接桥接逻辑 (session 状态存储于 Redis)
│   ├── token_manager.py    # Token 管理 (MySQL)
│   ├── admin_auth.py       # Admin 认证 (MySQL + Redis session)
│   ├── media_manager.py    # 媒体文件管理（MySQL + 本地文件系统）
│   ├── models.py           # SQLAlchemy ORM 模型 (Token, AdminConfig, Media)
│   ├── database.py         # MySQL 异步引擎 & 连接池
│   ├── cache.py            # Redis 连接管理 (单机/集群)
│   ├── config.py           # 配置加载 (.env)
│   ├── run.py              # 启动入口
│   ├── requirements.txt
│   ├── alembic.ini         # Alembic 配置
│   ├── .env.example        # 环境变量示例
│   └── migrations/         # Alembic 数据库迁移
│       └── versions/       # 迁移版本文件
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

### 1. 配置环境

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

### 2. 初始化数据库

```bash
# 安装依赖
pip install -r requirements.txt

# 执行数据库迁移（自动创建数据库和表）
alembic upgrade head
```

### 3. 启动服务端

```bash
python3 run.py
```

服务启动后：
- 聊天界面：`http://localhost:8765/`
- 管理面板：`http://localhost:8765/admin`（首次访问需设置密码）
- 健康检查：`http://localhost:8765/api/health`

### 4. 安装 OpenClaw 插件

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

### 5. 卸载插件

```bash
# 远程执行
curl -fsSL https://raw.githubusercontent.com/hygao1024/astron-claw/master/uninstall.sh | bash -s -- -y

# 或本地执行
./uninstall.sh       # 交互式确认
./uninstall.sh -y    # 静默卸载
```

## 数据库迁移

使用 Alembic 管理数据库 Schema 版本：

```bash
cd server

# 查看当前版本
alembic current

# 修改 models.py 后自动生成迁移
alembic revision --autogenerate -m "描述变更内容"

# 升级到最新版本
alembic upgrade head

# 回退一个版本
alembic downgrade -1

# 查看迁移历史
alembic history
```

## API 概览

详细文档见 [docs/api.md](docs/api.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查（MySQL + Redis 连通性） |
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
| WebSocket | `/bridge/chat` | Chat 端连接 |

## 技术栈

- **服务端**：Python 3 / FastAPI / Uvicorn
- **数据库**：MySQL (SQLAlchemy ORM + Alembic) / Redis (单机 + 集群)
- **前端**：原生 HTML / CSS / JavaScript
- **插件**：Node.js / WebSocket (ws) / OpenClaw ChannelPlugin SDK
- **协议**：WebSocket + JSON-RPC 2.0

## License

MIT
