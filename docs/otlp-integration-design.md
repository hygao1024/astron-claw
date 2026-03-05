# Astron Claw — OpenTelemetry (OTLP) 集成设计文档

> **版本**: v1.0
> **日期**: 2026-03-05
> **目标**: 以最小侵入方式将 Traces、Metrics、Logs 接入现有 OTel Collector + Jaeger 观测体系，
> 并确保 `token` 和 `session_id` 成为一等公民查询维度

---

## 一、现状分析

### 1.1 现有观测能力

| 维度 | 现状 | 问题 |
|------|------|------|
| 日志 | Loguru → stderr + 文件轮转 | 纯文本格式，无 trace_id/span_id 关联 |
| 链路追踪 | 无 | 消息从 Chat→Bridge→Bot 全链路不可观测 |
| 指标 | 无 | 无 QPS、延迟、连接数等运行时指标 |
| 关联标识 | `worker_id`(12-char hex)、`request_id`(`req_*`) | 仅用于日志打印，非标准 W3C Trace Context |

### 1.2 现有基础设施

```
┌────────────────┬────────────────┬──────────────────────────┬───────────────────┐
│      服务      │     容器名     │           端口           │       说明        │
├────────────────┼────────────────┼──────────────────────────┼───────────────────┤
│ OTel Collector │ otel-collector │ 4317 (gRPC), 4318 (HTTP) │ 接收 OTLP 数据    │
├────────────────┼────────────────┼──────────────────────────┼───────────────────┤
│ Jaeger         │ jaeger         │ 16686                    │ Web UI 可视化界面 │
└────────────────┴────────────────┴──────────────────────────┴───────────────────┘
```

### 1.3 关键代码入口

| 文件 | 角色 | 集成意义 |
|------|------|----------|
| `run.py` | 进程入口 | OTel SDK 初始化点（`setup_logging()` 之后） |
| `app.py` | FastAPI 应用 + lifespan | 中间件注入点（当前零中间件） |
| `infra/log.py` | Loguru 日志配置 | 注入 trace_id/span_id 到日志格式 |
| `infra/config.py` | 环境变量配置 | 新增 OTLP 配置项 |
| `routers/websocket.py` | WebSocket 端点 | Bot/Chat 连接级 Span 起点 |
| `services/bridge.py` | 核心桥接逻辑 | 消息流 Span 传播的关键路径 |

---

## 二、设计原则

1. **最小侵入**: 业务代码零修改或极少修改，优先使用自动化 Instrumentor + 中间件
2. **开关可控**: 通过环境变量 `OTEL_ENABLED=true/false` 一键启用/禁用，禁用时零开销
3. **分层集成**: 先自动插桩 → 再手动增强核心链路，渐进式接入
4. **一等公民查询**: `token` / `session_id` / `request_id` 作为标准 Attribute 全链路透传，支持在 Jaeger 中直接检索
5. **复用现有标识**: `request_id` 和 `worker_id` 保留原有语义，同时提升为 Span Attribute

---

## 三、整体架构

```
                          Astron Claw Server
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  run.py                                                          │
│  ┌─────────────────────────────────────┐                         │
│  │ setup_logging()                     │                         │
│  │ setup_telemetry()  ← 新增           │                         │
│  │   ├─ TracerProvider + OTLP Exporter │                         │
│  │   ├─ MeterProvider + OTLP Exporter  │                         │
│  │   └─ Resource(service.name)         │                         │
│  └─────────────────────────────────────┘                         │
│                                                                  │
│  app.py                                                          │
│  ┌─────────────────────────────────────┐                         │
│  │ FastAPIInstrumentor.instrument()    │ ← 自动插桩 HTTP 路由    │
│  │ SQLAlchemyInstrumentor.instrument() │ ← 自动插桩 DB 查询      │
│  │ RedisInstrumentor.instrument()      │ ← 自动插桩 Redis 操作   │
│  └─────────────────────────────────────┘                         │
│                                                                  │
│  infra/log.py                                                    │
│  ┌─────────────────────────────────────┐                         │
│  │ LOG_FORMAT += trace_id / span_id    │ ← 日志关联链路          │
│  └─────────────────────────────────────┘                         │
│                                                                  │
│  infra/telemetry.py  ← 新文件                                    │
│  ┌─────────────────────────────────────┐                         │
│  │ setup_telemetry()                   │                         │
│  │ shutdown_telemetry()                │                         │
│  │ get_tracer() / get_meter()          │                         │
│  └─────────────────────────────────────┘                         │
│                                                                  │
│  routers/websocket.py + services/bridge.py                       │
│  ┌─────────────────────────────────────┐                         │
│  │ 手动 Span + Span Link              │ ← WebSocket 链路追踪    │
│  │ 统一 Attribute 注入                 │ ← token/session 可查询  │
│  └─────────────────────────────────────┘                         │
│                                                                  │
└──────────────────────────────────────────────────────┬───────────┘
                                                       │ OTLP gRPC
                                                       ▼
                                              ┌────────────────┐
                                              │ OTel Collector  │
                                              │  :4317 (gRPC)   │
                                              └───────┬────────┘
                                                      │
                                                      ▼
                                              ┌────────────────┐
                                              │    Jaeger       │
                                              │   :16686 (UI)   │
                                              └────────────────┘
```

---

## 四、Attribute 规范

### 4.1 统一 Attribute 命名

所有手动 Span 必须遵循以下命名规范，确保 Jaeger 中查询一致性：

| Attribute Key | 值示例 | 说明 | 必填范围 |
|---------------|--------|------|----------|
| `astron.token` | `sk-a1b2c3d4e5f6...` | **完整 token**，Jaeger 主查询维度 | 所有 WS Span + Bridge Span |
| `astron.session_id` | `550e8400-e29b-...` | 完整 session UUID | 所有 Chat Span + Bridge 消息 Span |
| `astron.request_id` | `req_a1b2c3d4e5f6` | JSON-RPC 请求 ID，跨 Chat/Bot 关联 | `send_to_bot` / `handle_bot_message` |
| `astron.worker_id` | `a1b2c3d4e5f6` | Bridge Worker ID | 所有 Bridge Span |
| `astron.msg_type` | `text` / `image` / `file` | 消息类型 | 消息级 Span |
| `astron.ws.role` | `bot` / `chat` | WebSocket 角色 | 连接级 Span |
| `astron.ws.disconnect_reason` | `client` / `error` / `server_restart` | 断连原因 | 连接级 Span (结束时设置) |

### 4.2 Jaeger 查询示例

```
# 查某 token 的所有活动
Service: astron-claw  |  Tags: astron.token = "sk-a1b2c3d4e5f6..."

# 查某 session 的所有消息流
Service: astron-claw  |  Tags: astron.session_id = "550e8400-e29b-..."

# 查某条消息的完整链路 (Chat → Bot → Chat)
Service: astron-claw  |  Tags: astron.request_id = "req_a1b2c3d4e5f6"

# 查所有 Bot 连接/断连事件
Service: astron-claw  |  Operation: ws.bot.session

# 查所有消息发送
Service: astron-claw  |  Operation: bridge.send_to_bot
```

### 4.3 查询能力矩阵

| 查询需求 | Jaeger 操作方式 | 支持 |
|----------|----------------|------|
| 查某 token 所有活动 | Tags: `astron.token = sk-xxx` | ✅ |
| 查某 session 的所有消息 | Tags: `astron.session_id = uuid` | ✅ |
| 查某条消息完整链路 (Chat→Bot→Chat) | Tags: `astron.request_id = req_xxx` | ✅ |
| 从 Chat Span 跳转到 Bot 响应 Span | Span Link (FOLLOWS_FROM) | ✅ |
| 查某 token 的 Bot 连接/断连 | Tags: `astron.token` + Operation: `ws.bot.session` | ✅ |
| 查所有消息发送耗时分布 | Operation: `bridge.send_to_bot` → 延迟分布图 | ✅ |
| 查某 Worker 的所有 Span | Tags: `astron.worker_id = xxx` | ✅ |

---

## 五、详细设计

### 5.1 新增依赖

```toml
# pyproject.toml — dependencies 新增
"opentelemetry-api",
"opentelemetry-sdk",
"opentelemetry-exporter-otlp-proto-grpc",
"opentelemetry-instrumentation-fastapi",
"opentelemetry-instrumentation-sqlalchemy",
"opentelemetry-instrumentation-redis",
```

### 5.2 新增环境变量

```bash
# .env 新增
OTEL_ENABLED=true
OTEL_SERVICE_NAME=astron-claw
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

### 5.3 配置扩展 — `infra/config.py`

新增 `OtelConfig` 数据类并挂载到 `AppConfig`:

```python
@dataclass(frozen=True)
class OtelConfig:
    enabled: bool
    service_name: str
    otlp_endpoint: str


@dataclass(frozen=True)
class AppConfig:
    mysql: MysqlConfig
    redis: RedisConfig
    server: ServerConfig
    otel: OtelConfig          # ← 新增


def load_config() -> AppConfig:
    return AppConfig(
        # ... 现有 mysql / redis / server 不变 ...
        otel=OtelConfig(
            enabled=os.getenv("OTEL_ENABLED", "false").lower() == "true",
            service_name=os.getenv("OTEL_SERVICE_NAME", "astron-claw"),
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
        ),
    )
```

**侵入程度**: 新增一个数据类 + `load_config()` 中增加 5 行。

### 5.4 核心模块 — `infra/telemetry.py` (新文件)

封装 OTel SDK 初始化/关闭，对外提供 `get_tracer()` 和 `get_meter()`。

```python
"""OpenTelemetry 初始化模块.

仅在 OTEL_ENABLED=true 时激活，否则全局保持 NoOp 实现（零开销）。
"""

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def setup_telemetry(
    enabled: bool,
    service_name: str,
    otlp_endpoint: str,
) -> None:
    """初始化 OTel SDK，注册全局 TracerProvider 和 MeterProvider。

    当 enabled=False 时直接返回，所有 get_tracer()/get_meter()
    调用将返回 OTel API 内置的 NoOp 实现，零性能开销。
    """
    global _tracer_provider, _meter_provider

    if not enabled:
        return

    resource = Resource.create({SERVICE_NAME: service_name})

    # ── Traces ──
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        )
    )
    trace.set_tracer_provider(_tracer_provider)

    # ── Metrics ──
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
        export_interval_millis=15000,
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_meter_provider)


def shutdown_telemetry() -> None:
    """优雅关闭，flush 所有未发送的 Span 和 Metric 数据。"""
    if _tracer_provider:
        _tracer_provider.shutdown()
    if _meter_provider:
        _meter_provider.shutdown()


def get_tracer(name: str = "astron-claw") -> trace.Tracer:
    """获取 Tracer 实例。OTel 未启用时返回 NoOp Tracer。"""
    return trace.get_tracer(name)


def get_meter(name: str = "astron-claw") -> metrics.Meter:
    """获取 Meter 实例。OTel 未启用时返回 NoOp Meter。"""
    return metrics.get_meter(name)
```

**侵入程度**: 纯新增文件，不修改任何现有代码。

### 5.5 入口集成 — `run.py`

在 `setup_logging()` 之后新增一行调用：

```python
# 现有
setup_logging(level=server.log_level.upper())

# 新增 (2 行)
from infra.telemetry import setup_telemetry
setup_telemetry(
    enabled=config.otel.enabled,
    service_name=config.otel.service_name,
    otlp_endpoint=config.otel.otlp_endpoint,
)
```

**侵入程度**: 新增 2 行 import + 调用。

### 5.6 自动插桩 — `app.py`

在 `lifespan` 函数中添加自动 Instrumentor，关闭时 flush:

```python
from infra.telemetry import shutdown_telemetry

@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()

    # ... 现有初始化代码 (MySQL / Redis / Services) 全部不变 ...

    # ── 自动插桩 (仅 OTel 启用时) ──
    if config.otel.enabled:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from infra.database import get_engine

        FastAPIInstrumentor.instrument_app(app)
        SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)
        RedisInstrumentor().instrument()

    logger.info("Astron Claw Bridge Server started")
    yield

    # ── 关闭顺序: 业务 → OTel → 基础设施 ──
    await state.bridge.shutdown()
    shutdown_telemetry()          # ← 新增: flush spans/metrics
    await close_redis()
    await close_db()
    logger.info("Astron Claw Bridge Server stopped")
```

**自动获得的能力**:

| Instrumentor | 自动覆盖范围 |
|--------------|-------------|
| `FastAPIInstrumentor` | 所有 HTTP 路由 (`/api/*`) 自动生成 Span，含 http.method、http.status_code、http.url |
| `SQLAlchemyInstrumentor` | 所有 DB 查询自动生成子 Span，含 db.statement、db.system |
| `RedisInstrumentor` | 所有 Redis 命令自动生成子 Span，含 db.statement、net.peer.name |

**侵入程度**: `lifespan` 内新增约 10 行，`if` 守卫隔离，现有代码不变。

### 5.7 日志关联 — `infra/log.py`

在日志格式中注入当前 Span 的 `trace_id` 和 `span_id`，实现 **日志 ↔ 链路双向互查**：

```python
def _get_otel_context() -> str:
    """从当前 OTel Context 中提取 trace_id 和 span_id。"""
    try:
        from opentelemetry import trace as _trace
        span = _trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return (
                f"trace_id={format(ctx.trace_id, '032x')} "
                f"span_id={format(ctx.span_id, '016x')}"
            )
    except Exception:
        pass
    return "trace_id=- span_id=-"


def _otel_patcher(record):
    """Loguru patcher: 为每条日志动态注入 OTel 上下文。"""
    record["extra"]["otel_context"] = _get_otel_context()


# 修改日志格式，追加 OTel 上下文字段
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "{extra[otel_context]} | "
    "<level>{message}</level>"
)
```

在 `setup_logging()` 中应用 patcher:

```python
def setup_logging(level: str = "INFO") -> None:
    # ... 现有逻辑不变 ...

    logger.remove()
    patched = logger.patch(_otel_patcher)

    # Console sink
    patched.add(sys.stderr, format=LOG_FORMAT, level=level, colorize=True)

    # File sink
    patched.add(
        str(log_dir / "server.log"),
        format=LOG_FORMAT, level=level,
        rotation="50 MB", retention="30 days",
        compression="gz", encoding="utf-8",
    )

    # ... stdlib interception 不变 ...
```

**日志效果**:

```
2026-03-05 15:30:12.345 | INFO     | routers.tokens:create:25 | trace_id=4bf92f3577b34da6a3ce929d0e0e4736 span_id=00f067aa0ba902b7 | Token created: sk-xxx...
```

> 在 Jaeger 中搜索 `trace_id=4bf92f...` 即可定位对应链路；反之，在链路详情中也可通过 trace_id 反查日志。

**侵入程度**: 修改 `LOG_FORMAT` 字符串 + 新增 `_otel_patcher` / `_get_otel_context` 函数 + `setup_logging` 中改用 `logger.patch()`。

### 5.8 WebSocket 手动 Span — `routers/websocket.py`

FastAPIInstrumentor **无法自动追踪 WebSocket 生命周期**，需要手动创建 Span。

#### Bot 端

```python
from infra.telemetry import get_tracer
tracer = get_tracer()

@router.websocket("/bridge/bot")
async def ws_bot(ws: WebSocket, token: str = Query(default="")):
    bot_token = token or ws.headers.get("x-astron-bot-token", "")
    if not await state.token_manager.validate(bot_token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing bot token")
        return

    await ws.accept()

    if not await state.bridge.register_bot(bot_token, ws):
        await ws.send_json({"error": "Another bot is already connected"})
        await ws.close(code=4002, reason="Bot already connected")
        return

    # ── 连接级 Span ──
    with tracer.start_as_current_span(
        "ws.bot.session",
        attributes={
            "astron.token": bot_token,               # 完整 token
            "astron.ws.role": "bot",
            "astron.worker_id": state.bridge._worker_id,
        },
    ) as conn_span:
        await state.bridge.notify_bot_connected(bot_token)
        try:
            while True:
                raw = await ws.receive_text()
                # ── 消息级 Span ──
                with tracer.start_as_current_span(
                    "ws.bot.message",
                    attributes={"astron.token": bot_token},
                ):
                    await state.bridge.handle_bot_message(bot_token, raw)
        except WebSocketDisconnect:
            conn_span.set_attribute("astron.ws.disconnect_reason", "client")
        except Exception:
            conn_span.set_attribute("astron.ws.disconnect_reason", "error")
            conn_span.record_exception(Exception)
        finally:
            await state.bridge.unregister_bot(bot_token)
            await state.bridge.notify_bot_disconnected(bot_token)
```

#### Chat 端

```python
@router.websocket("/bridge/chat")
async def ws_chat(ws: WebSocket, token: str = Query(default="")):
    if not await state.token_manager.validate(token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing token")
        return

    await ws.accept()

    # ... 现有 session 恢复/创建逻辑不变 ...

    await state.bridge.register_chat(token, ws, session_id)

    # ── 连接级 Span ──
    with tracer.start_as_current_span(
        "ws.chat.session",
        attributes={
            "astron.token": token,                   # 完整 token
            "astron.session_id": session_id,
            "astron.ws.role": "chat",
            "astron.worker_id": state.bridge._worker_id,
        },
    ) as conn_span:
        try:
            while True:
                data = await ws.receive_json()
                msg_type = data.get("type", "")

                if msg_type == "new_session":
                    with tracer.start_as_current_span(
                        "ws.chat.new_session",
                        attributes={"astron.token": token},
                    ):
                        # ... 现有逻辑不变 ...

                elif msg_type == "switch_session":
                    with tracer.start_as_current_span(
                        "ws.chat.switch_session",
                        attributes={
                            "astron.token": token,
                            "astron.session_id": data.get("sessionId", ""),
                        },
                    ):
                        # ... 现有逻辑不变 ...

                elif msg_type == "message":
                    with tracer.start_as_current_span(
                        "ws.chat.message",
                        attributes={
                            "astron.token": token,
                            "astron.session_id": session_id,
                            "astron.msg_type": data.get("msgType", "text"),
                        },
                    ):
                        # ... 现有消息处理逻辑不变 ...

        except WebSocketDisconnect:
            conn_span.set_attribute("astron.ws.disconnect_reason", "client")
        except Exception:
            conn_span.set_attribute("astron.ws.disconnect_reason", "error")
        finally:
            await state.bridge.unregister_chat(token, ws)
```

**侵入程度**: 原有业务逻辑包裹在 `with` 块中，缩进一层，不改变语义。

### 5.9 核心链路 Span + Span Link — `services/bridge.py`

这是实现 **token / session_id / request_id 全链路可查**的关键。

#### 5.9.1 Trace Context 跨 Redis 传播

Chat 和 Bot 是两条独立 WebSocket 连接，消息经过 Redis Inbox 中转。为了关联它们，
将 Trace Context 序列化后注入到 Redis 消息中，Bot 侧收到后通过 **Span Link** 建立关联。

```
Chat WS                        Redis                        Bot WS
   │                             │                             │
   │  send_to_bot()              │                             │
   │  ┌──────────────────┐       │                             │
   │  │ span: bridge.     │       │                             │
   │  │   send_to_bot     │──RPUSH──►  bot_inbox:{token}       │
   │  │                   │       │   payload 含                │
   │  │ 记录 trace_id +   │       │   _trace_context:          │
   │  │   span_id         │       │     trace_id + span_id     │
   │  └──────────────────┘       │                             │
   │                             │                             │
   │                             │   LPOP ──►                  │
   │                             │         handle_bot_message()│
   │                             │         ┌────────────────┐  │
   │                             │         │ span: bridge.   │  │
   │                             │         │  handle_bot_msg │  │
   │                             │         │                 │  │
   │                             │         │ SpanLink ──────►│──── 指向 send_to_bot 的 SpanContext
   │                             │         └────────────────┘  │
```

#### 5.9.2 send_to_bot 修改

```python
from infra.telemetry import get_tracer
from opentelemetry import trace

tracer = get_tracer()

async def send_to_bot(self, token, user_message, msg_type="text", media=None):
    with tracer.start_as_current_span(
        "bridge.send_to_bot",
        attributes={
            "astron.token": token,
            "astron.msg_type": msg_type,
            "astron.worker_id": self._worker_id,
        },
    ) as span:
        session_id = await self.get_active_session(token)
        if not session_id:
            session_id, _ = await self.create_session(token)

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        self._pending_requests[request_id] = (token, session_id)

        span.set_attribute("astron.session_id", session_id)
        span.set_attribute("astron.request_id", request_id)

        # ... 现有 rpc_request 构建逻辑不变 ...

        # ── 注入 Trace Context 到消息中，用于跨 Redis 传播 ──
        current_ctx = trace.get_current_span().get_span_context()
        if current_ctx and current_ctx.trace_id:
            rpc_request["_trace_context"] = {
                "trace_id": format(current_ctx.trace_id, '032x'),
                "span_id": format(current_ctx.span_id, '016x'),
            }

        # ... 现有 Redis RPUSH 逻辑不变 ...
```

#### 5.9.3 handle_bot_message 修改

```python
async def handle_bot_message(self, token, raw):
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    if msg.get("type") == "ping":
        return

    method = msg.get("method", "")
    request_id = msg.get("id", "")

    # ── 构建 Span Link: 从 pending_request 中恢复原始 Trace Context ──
    links = []
    trace_ctx = None
    # JSON-RPC response 的情况：通过 request_id 查找原始 trace
    if request_id and request_id in self._pending_requests:
        # 从请求上下文恢复（如果存储了的话）
        pass
    # JSON-RPC notification 的情况：从消息体中提取 _trace_context
    if isinstance(msg.get("_trace_context"), dict):
        tc = msg["_trace_context"]
        try:
            from opentelemetry.trace import SpanContext, TraceFlags
            original_ctx = SpanContext(
                trace_id=int(tc["trace_id"], 16),
                span_id=int(tc["span_id"], 16),
                is_remote=True,
                trace_flags=TraceFlags(0x01),
            )
            links.append(trace.Link(original_ctx))
        except Exception:
            pass

    with tracer.start_as_current_span(
        "bridge.handle_bot_message",
        links=links,
        attributes={
            "astron.token": token,
            "astron.worker_id": self._worker_id,
            "astron.request_id": request_id if request_id else "",
            "rpc.method": method,
        },
    ) as span:
        # ... 现有消息路由逻辑完全不变 ...
        # 在找到 session_id 后补充设置
        if session_id:
            span.set_attribute("astron.session_id", session_id)
```

#### 5.9.4 _send_to_session 修改

```python
async def _send_to_session(self, token, session_id, event):
    with tracer.start_as_current_span(
        "bridge.send_to_session",
        attributes={
            "astron.token": token,
            "astron.session_id": session_id,
            "event.type": event.get("type", ""),
        },
    ):
        # ... 现有 Redis RPUSH 逻辑不变 ...
```

**侵入程度**: 核心函数新增 `with` 包裹 + 属性设置。**业务逻辑、函数签名、返回值全部不变**。

### 5.10 自定义 Metrics

在 `ConnectionBridge.__init__` 或 `start()` 中注册业务指标：

```python
from infra.telemetry import get_meter

meter = get_meter()

# ── 计数器 ──
self._msg_counter = meter.create_counter(
    "astron.bridge.messages_total",
    description="Total messages routed through bridge",
)

# ── 直方图 (延迟) ──
self._msg_duration = meter.create_histogram(
    "astron.bridge.message_duration_ms",
    description="Message routing latency in ms",
    unit="ms",
)

# ── 可观测量规 (连接数) ──
meter.create_observable_gauge(
    "astron.bridge.bots_connected",
    callbacks=[lambda options: [metrics.Observation(len(self._bots))]],
    description="Current bot connections on this worker",
)
meter.create_observable_gauge(
    "astron.bridge.chats_connected",
    callbacks=[lambda options: [metrics.Observation(
        sum(len(s) for s in self._chats.values())
    )]],
    description="Current chat connections on this worker",
)
```

在 `send_to_bot()` 中使用：

```python
self._msg_counter.add(1, {"astron.msg_type": msg_type, "astron.token": token})
```

| 指标名 | 类型 | 标签 | 说明 |
|--------|------|------|------|
| `astron.bridge.messages_total` | Counter | `msg_type`, `token` | 消息总量 |
| `astron.bridge.message_duration_ms` | Histogram | `msg_type` | 消息路由延迟分布 |
| `astron.bridge.bots_connected` | Gauge | — | 当前 Bot 连接数 |
| `astron.bridge.chats_connected` | Gauge | — | 当前 Chat 连接数 |

---

## 六、Span 拓扑总览

以一次完整的 **Chat 发消息 → Bot 回复 → Chat 收到** 流程为例：

```
━━━ Trace A (Chat 侧) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ws.chat.session]  astron.token=sk-xxx  astron.session_id=sess-001
 │
 └─[ws.chat.message]  astron.msg_type=text
    │
    └─[bridge.send_to_bot]  astron.request_id=req_abc  ──────┐
       │                                                      │
       ├─ Redis:RPUSH (自动)                                   │ trace_context
       └─ MySQL:SELECT (自动)                                  │ 注入到消息
                                                               │
━━━ Trace B (Bot 侧) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│━━━━
                                                               │
[ws.bot.session]  astron.token=sk-xxx                          │
 │                                                             │
 └─[ws.bot.message]                                            │
    │                                                          │
    └─[bridge.handle_bot_message]  ◄── SpanLink ──────────────┘
       │                               (FOLLOWS_FROM Trace A)
       │  astron.request_id=req_abc
       │  astron.session_id=sess-001
       │
       └─[bridge.send_to_session]
          │
          └─ Redis:RPUSH (自动)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

在 Jaeger 中:
  - 搜索 astron.token=sk-xxx       → 同时看到 Trace A 和 Trace B
  - 搜索 astron.request_id=req_abc → 同时看到 Trace A 和 Trace B
  - 点击 Trace B 中的 SpanLink     → 跳转到 Trace A 的 send_to_bot Span
```

---

## 七、OTel 关闭时的零开销保证

当 `OTEL_ENABLED=false` 时:

| 组件 | 行为 |
|------|------|
| `setup_telemetry()` | 直接 return，不创建任何 Provider |
| `get_tracer()` | 返回全局 NoOp Tracer (OTel API 标准行为) |
| `get_meter()` | 返回全局 NoOp Meter |
| `tracer.start_as_current_span()` | 返回 NoOp Span (几乎零开销 Context Manager) |
| 自动 Instrumentor | 不注册 (被 `if config.otel.enabled` 守卫) |
| 日志 patcher | `get_current_span()` 返回 INVALID_SPAN → 输出 `trace_id=- span_id=-` |
| Metrics | `counter.add()` / `histogram.record()` 为 NoOp 调用 |

> 不需要在每个业务调用点做 `if otel_enabled` 判断，OTel API 的 NoOp 机制天然保证了这一点。

---

## 八、修改清单与侵入度评估

| 文件 | 修改类型 | 改动行数(估) | 侵入程度 |
|------|----------|-------------|----------|
| `infra/telemetry.py` | **新增文件** | ~65 行 | 无 (新文件) |
| `infra/config.py` | 新增 `OtelConfig` + 读取 | ~12 行 | 极低 |
| `infra/log.py` | 修改 `LOG_FORMAT` + 新增 patcher | ~20 行 | 低 |
| `run.py` | 新增 `setup_telemetry()` 调用 | ~5 行 | 极低 |
| `app.py` | lifespan 内新增插桩 + shutdown | ~12 行 | 低 |
| `routers/websocket.py` | 手动 Span 包裹 + Attribute | ~30 行 | 低 |
| `services/bridge.py` | Span + Link + Metrics + Attribute | ~45 行 | 中低 |
| `pyproject.toml` | 新增 OTel 依赖 | ~6 行 | 无 |
| `.env` | 新增 3 个环境变量 | ~3 行 | 无 |
| **合计** | | **~198 行** | |

> **核心承诺**: 现有业务逻辑的函数签名、返回值、调用关系、错误处理全部不变。
> 所有修改都是"包裹式"(with 语句) 和"追加式"(属性设置)，而非"改写式"。

---

## 九、实施阶段

### Phase 1 — 基础接入 (自动插桩)

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1 | `pyproject.toml` | 添加 OTel 依赖 |
| 2 | `infra/telemetry.py` | 新建 SDK 初始化模块 |
| 3 | `infra/config.py` | 新增 `OtelConfig` |
| 4 | `run.py` | 调用 `setup_telemetry()` |
| 5 | `app.py` | 启用 FastAPI / SQLAlchemy / Redis 自动插桩 |
| 6 | `.env` | 配置 OTLP 环境变量 |

**验证**: Jaeger UI 可见所有 HTTP API 请求 Span (`/api/health`, `/api/token` 等) 及其子 Span (MySQL/Redis)

### Phase 2 — 日志关联

| 步骤 | 文件 | 内容 |
|------|------|------|
| 7 | `infra/log.py` | 注入 trace_id / span_id 到日志格式 |

**验证**: 日志中出现 `trace_id=xxx span_id=xxx`，可通过 trace_id 在 Jaeger 定位对应请求

### Phase 3 — WebSocket 链路

| 步骤 | 文件 | 内容 |
|------|------|------|
| 8 | `routers/websocket.py` | Bot/Chat 连接级 + 消息级 Span，含完整 Attribute |

**验证**: Jaeger 中可见 `ws.bot.session` / `ws.chat.session` / `ws.chat.message` Span，可按 `astron.token` 查询

### Phase 4 — 核心链路追踪 + Metrics

| 步骤 | 文件 | 内容 |
|------|------|------|
| 9 | `services/bridge.py` | `send_to_bot` / `handle_bot_message` Span + Span Link + trace_context 传播 |
| 10 | `services/bridge.py` | 业务 Metrics 注册 |

**验证**:
- 搜索 `astron.request_id=req_xxx` 可同时看到 Chat 侧和 Bot 侧 Span
- 搜索 `astron.token=sk-xxx` 可看到该 token 的所有链路
- 搜索 `astron.session_id=xxx` 可看到该会话的所有消息
- Span Link 可从 Bot 响应跳转到 Chat 发送

---

## 十、配置参考

### .env 完整新增

```bash
# ── OpenTelemetry ────────────────────────────────
OTEL_ENABLED=true
OTEL_SERVICE_NAME=astron-claw
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

### Docker Compose 网络互通

确保 `astron-claw` 容器与 `otel-collector` 在同一 Docker 网络中:

```yaml
services:
  astron-claw:
    environment:
      - OTEL_ENABLED=true
      - OTEL_SERVICE_NAME=astron-claw
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
    networks:
      - observability

  otel-collector:
    networks:
      - observability

  jaeger:
    networks:
      - observability
```
