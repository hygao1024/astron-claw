# Trace 埋点实施方案

基于 [traces-reference.md](traces-reference.md) 和 [traces-reference-notes.md](traces-reference-notes.md) 对当前工程的评估与实施计划。

> 评估日期：2026-04-14  
> 最后更新：2026-04-14（根据代码评审修订）

---

## 现状评估

**当前状态：Trace 覆盖为零**

| 类别 | 状态 |
|------|------|
| TracerProvider 初始化 | 不存在 |
| otelgin HTTP span | 不存在 |
| 业务 span | 不存在 |
| `otel/trace` 依赖 | indirect（未直接使用） |
| Metrics 覆盖 | 已有（MeterProvider + 4 个指标） |

与 `traces-reference.md` 定义的 17 个 span 相比，**实现率 0%**。

**关键约束：**
- `config.go:191-193` 硬编码 `TracesEnabled: false`，不读环境变量
- `provider.go:26-29` 只要 `MetricsEnabled=false` 就直接返回，无 TracerProvider 初始化路径
- `provider.go:86` Shutdown 只关闭 MeterProvider
- `websocket.go:84` HandleBotMessage 使用 `context.Background()`，丢弃连接级 context
- `bridge_worker_inbox.go:14` workerInboxMessage 无 `traceparent` 字段
- `bridge.go:545` session/prompt payload 无 trace 字段
- Bot 插件回传的 session/update 不带 requestId/traceparent

---

## 差距分析

| 参考文档 Span | 对应代码位置 | 当前状态 | 阶段 |
|---|---|---|---|
| `HTTP {method}` | otelgin 中间件 | 缺失 | 第一阶段 |
| `chat.turn` | `router/sse.go:41 chatSSE` | 缺失 | 第一阶段 |
| `chat.session.resolve` | `router/sse.go:103-123` | 缺失 | 第一阶段 |
| `chat.bot.availability_check` | `router/sse.go:93` | 缺失 | 第一阶段 |
| `chat.bot.dispatch` | `service/bridge.go:523 SendToBot` | 缺失 | 第一阶段 |
| `chat.response.stream` | `router/sse.go:183-376` 主循环 | 缺失 | 第一阶段 |
| `chat.cancel` | `router/sse.go:246` client disconnect 分支 | 缺失 | 第一阶段 |
| `session.create` | `service/session_store.go:34` | 缺失 | 第一阶段 |
| `session.get` | `service/session_store.go:65` | 缺失 | 第一阶段 |
| `session.list` | `service/session_store.go:83` | 缺失 | 第一阶段 |
| `session.remove` | `service/session_store.go:54` | 缺失 | 第一阶段 |
| `bot.connection.register` | `router/websocket.go:56 RegisterBot` | 缺失 | 第一阶段 |
| `bot.connection.unregister` | `router/websocket.go:65 UnregisterBot` | 缺失 | 第一阶段 |
| `bot.message.receive` | `service/bridge.go:573 HandleBotMessage` | 缺失 | **已实现（服务端映射）** |
| `bot.event.translate` | `service/bridge.go:748 TranslateBotEvent` | 缺失 | 不实现（纯函数，无 I/O） |
| `chat.message.deliver` | `service/bridge.go:694 sendToSession` | 缺失 | **已实现（服务端映射）** |
| `bot.connection.heartbeat_check` | `service/bot_status_monitor.go:122` | 缺失 | 可选 |

**反向链路实现方式：**
- 采用服务端映射（`sessionId → trace context` 存 Redis），无需修改 Bot 插件协议
- 限制：best-effort，只关联最新 turn，并发场景可能错乱

---

## 实施方案

### 分阶段策略

**第一阶段（当前后端可落地）：**
- TracerProvider 基础设施
- HTTP 入口 + Chat 主链路（正向）
- Session 域 + Bot 连接生命周期

**第二阶段（需协议扩展）：**
- 反向链路 context propagation
- 跨 Worker trace 传播
- Bot 消息处理链路

### 第一步：基础设施（第一阶段）

**1.1 添加依赖**

```bash
cd backend
go get go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc
go get go.opentelemetry.io/contrib/instrumentation/github.com/gin-gonic/gin/otelgin
```

**1.2 配置扩展（`internal/config/config.go`）**

添加环境变量读取，替换硬编码：

```go
// config.go:186-194
OTLP: OtlpConfig{
    Enabled:        getEnvBool("OTLP_ENABLED", false),
    ServiceName:    getEnv("OTLP_SERVICE_NAME", "astron-claw"),
    Endpoint:       getEnv("OTLP_ENDPOINT", "localhost:4317"),
    Insecure:       getEnvBool("OTLP_INSECURE", false),
    MetricsEnabled: getEnvBool("OTLP_METRICS_ENABLED", true),
    TracesEnabled:  getEnvBool("OTLP_TRACES_ENABLED", false),  // 改为读环境变量
    LogsEnabled:    getEnvBool("OTLP_LOGS_ENABLED", false),
},
```

在 `backend/.env.example` 添加：

```bash
# OpenTelemetry
OTLP_ENABLED=false
OTLP_TRACES_ENABLED=false
OTLP_METRICS_ENABLED=true
OTLP_LOGS_ENABLED=false
```

**1.3 重构 `internal/infra/telemetry/provider.go`**

当前 `Init` 只处理 metrics，需要重构为同时支持 traces：

```go
var (
    meterProvider  *sdkmetric.MeterProvider
    tracerProvider *sdktrace.TracerProvider
)

func Init(ctx context.Context, otlpCfg config.OtlpConfig) error {
    if !otlpCfg.Enabled {
        log.Info().Msg("OTLP telemetry disabled")
        return nil
    }

    // 共用 gRPC 连接
    opts := []otlpmetricgrpc.Option{
        otlpmetricgrpc.WithEndpoint(otlpCfg.Endpoint),
    }
    if otlpCfg.Insecure {
        opts = append(opts, otlpmetricgrpc.WithInsecure())
    }

    conn, err := grpc.DialContext(ctx, otlpCfg.Endpoint, grpcOpts...)
    if err != nil {
        return err
    }

    res := resource.NewWithAttributes(
        semconv.SchemaURL,
        semconv.ServiceNameKey.String(otlpCfg.ServiceName),
    )

    // 初始化 MeterProvider（如果启用）
    if otlpCfg.MetricsEnabled {
        metricExporter, err := otlpmetricgrpc.New(ctx, otlpmetricgrpc.WithGRPCConn(conn))
        if err != nil {
            return err
        }
        // ... 现有 metrics 初始化逻辑
        log.Info().Msg("OTLP metrics exporter initialized")
    }

    // 初始化 TracerProvider（如果启用）
    if otlpCfg.TracesEnabled {
        traceExporter, err := otlptracegrpc.New(ctx, otlptracegrpc.WithGRPCConn(conn))
        if err != nil {
            return err
        }
        tracerProvider = sdktrace.NewTracerProvider(
            sdktrace.WithBatcher(traceExporter),
            sdktrace.WithResource(res),
        )
        otel.SetTracerProvider(tracerProvider)
        otel.SetTextMapPropagator(propagation.TraceContext{})
        log.Info().Msg("OTLP traces exporter initialized")
    }

    return nil
}

func Shutdown() {
    ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()

    if meterProvider != nil {
        if err := meterProvider.Shutdown(ctx); err != nil {
            log.Error().Err(err).Msg("MeterProvider shutdown error")
        }
        meterProvider = nil
    }

    if tracerProvider != nil {
        if err := tracerProvider.Shutdown(ctx); err != nil {
            log.Error().Err(err).Msg("TracerProvider shutdown error")
        }
        tracerProvider = nil
    }

    log.Info().Msg("OTLP telemetry shut down")
}
```

**1.4 注册 otelgin 中间件（`internal/router/router.go`）**

在现有中间件栈中明确位置，**必须在 Recovery 之后、Metrics 之前**：

```go
// router.go:31-40
func SetupRouter(app *App, podIP string) *gin.Engine {
    r := gin.New()
    r.Use(gin.Recovery())                          // 第一位：panic 恢复
    r.Use(otelgin.Middleware("astron_claw"))       // 第二位：trace 入口
    r.Use(middleware.MetricsMiddleware(podIP))     // 第三位：metrics
    r.Use(middleware.CORSMiddleware(app.CORSCfg))
    // ...
}
```

**顺序说明：**
- Recovery 必须最先，否则 panic 会导致 span 未关闭
- otelgin 在 Metrics 前，确保 span context 可用于 metrics 关联
- 在 Auth 前，确保 401/403 也能被追踪

---

### 第二步：Chat 主链路（`router/sse.go`，第一阶段）

`chatSSE` 函数按以下层次创建 span，外层 context 传递给内层：

```
HTTP POST（otelgin 自动）
└── chat.turn                        ← chatSSE 入口，覆盖整个 handler
    ├── chat.session.resolve         ← sse.go:103-123 session 解析块
    │   └── session.create           ← 仅新建时
    ├── chat.bot.availability_check  ← sse.go:93
    ├── chat.bot.dispatch            ← Bridge.SendToBot 内部
    └── chat.response.stream         ← sse.go:183 主循环开始到结束
        └── chat.cancel              ← 仅 client disconnect 分支
```

**关键实现点：**

**2.1 `turn_id` 来源**

直接复用 `bridge.go:529` 生成的 `requestID`（`req_` + uuid 前12位），作为 `astron.turn_id` 属性。

**2.2 `chat.turn` SpanKind**

使用 `trace.SpanKindInternal`，因为 `chat.turn` 是业务主 Span，不是传输层入口（传输层入口由 otelgin 自动创建的 `HTTP POST` span 表示）。

**2.3 `chat.bot.dispatch` SpanKind**

使用 `trace.SpanKindProducer`，因为这是向异步队列（Worker Inbox）发布消息的操作。

**2.4 `chat.cancel` context 传递**

SSE 流中有两个 client_disconnect 路径，都需要正确传递 trace context 并创建 `chat.cancel` span：

```go
// 第一个路径 (sse.go:347-357)
case <-c.Request.Context().Done():
    closeReason = "client_disconnect"
    cancelCtx := trace.ContextWithSpan(context.Background(), trace.SpanFromContext(ctx))
    go func() {
        _, cancelSpan := sseTracer.Start(cancelCtx, "chat.cancel",
            trace.WithSpanKind(trace.SpanKindInternal))
        defer cancelSpan.End()
        app.Bridge.SendCancelToBot(cancelCtx, tokenStr, sessionID)
    }()
    return

// 第二个路径 (sse.go:403-411) - 同样处理
case <-c.Request.Context().Done():
    closeReason = "client_disconnect"
    cancelCtx := trace.ContextWithSpan(context.Background(), trace.SpanFromContext(ctx))
    go func() {
        _, cancelSpan := sseTracer.Start(cancelCtx, "chat.cancel",
            trace.WithSpanKind(trace.SpanKindInternal))
        defer cancelSpan.End()
        app.Bridge.SendCancelToBot(cancelCtx, tokenStr, sessionID)
    }()
    return
```

**2.5 关键属性（使用 `astron.` 前缀）**

```go
// chat.turn
span.SetAttributes(
    attribute.String("astron.token_prefix", tokenPrefix),
    attribute.String("astron.session_id", sessionID),
    attribute.String("astron.turn_id", turnID),  // 复用 requestID
    attribute.Bool("astron.is_new_session", isNew),
)

// chat.response.stream 结束时
span.SetAttributes(attribute.String("astron.close_reason", closeReason))
```

---

### 第三步：Bot 连接生命周期（`router/websocket.go`，第一阶段）

**3.1 register / unregister span**

在 `wsBot` handler 中创建 span：

```go
// websocket.go:56 RegisterBot 前
ctx, regSpan := tracer.Start(r.Context(), "bot.connection.register",
    trace.WithSpanKind(trace.SpanKindInternal))
defer regSpan.End()

regSpan.SetAttributes(
    attribute.String("astron.token_prefix", tokenPrefix),
    attribute.String("astron.worker_id", workerID),
)

// websocket.go:65 defer UnregisterBot 时
defer func() {
    unregCtx, unregSpan := tracer.Start(context.Background(), "bot.connection.unregister",
        trace.WithSpanKind(trace.SpanKindInternal))
    defer unregSpan.End()
    unregSpan.SetAttributes(
        attribute.String("astron.token_prefix", tokenPrefix),
        attribute.String("astron.worker_id", workerID),
        attribute.String("astron.reason", reason),
    )
    app.Bridge.UnregisterBot(unregCtx, botToken)
}()
```

**3.2 heartbeat_check（可选）**

`bot_status_monitor.go:122 checkAllBots` 每秒执行一次，全量 span 会产生高频噪音。

**建议：**
- 仅在检测到超时/异常时创建 span
- 或改为纯 metric（counter: `bot.heartbeat.timeout`）
- 不作为第一阶段必做项

---

### 第四步：Session 域（`service/session_store.go`，第一阶段）

在 `CreateSession`、`GetSession`、`GetSessions`、`RemoveSessions` 各自入口处创建对应 span，context 由调用方传入：

```go
// session_store.go:34
func (s *SessionStore) CreateSession(ctx context.Context, token string) (*model.Session, error) {
    ctx, span := tracer.Start(ctx, "session.create", trace.WithSpanKind(trace.SpanKindInternal))
    defer span.End()
    
    span.SetAttributes(
        attribute.String("astron.token_prefix", pkg.SafePrefix(token, 10)),
        attribute.String("astron.session_id", sessionID),
    )
    // ... 现有逻辑
}
```

类似地为 `GetSession`、`GetSessions`、`RemoveSessions` 添加 span。

---

### 第五步：反向链路（服务端映射，已实现）

**方案：** 服务端 `sessionId → trace context` 映射（Redis），无需修改 Bot 插件协议。

**限制（best-effort）：** 只关联最新的 turn，并发场景可能错乱。

**实现：**

- `SendToBot`（`bridge.go`）：将当前 trace context 序列化存入 Redis，key=`bridge:trace_ctx:<sessionId>`，TTL=600s
- `HandleBotMessage`（`bridge.go`）：提取 sessionId 后从 Redis 恢复 trace context，作为 `bot.message.receive` span 的 parent
- `sendToSession`（`bridge.go`）：入口添加 `chat.message.deliver` span

**Span 链路：**

```
chat.turn
└── chat.bot.dispatch
bot.message.receive   ← parent = chat.turn（通过 Redis 映射恢复）
└── chat.message.deliver
```

**升级路径：** 若未来 Bot 插件支持回传 `traceparent`，可直接从消息体提取，移除 Redis 映射。

---

### 第六步：Error/Status 处理（所有阶段）

遵循 `traces-reference-notes.md` 的约定：

| 场景 | Span Status | 记录 exception |
|------|-------------|----------------|
| 业务拒绝（bad_request、no_bot、session_not_found） | `OK` | 否 |
| Session 创建失败 | `ERROR` | 是 |
| 消息发送失败 | `ERROR` | 是 |
| Bot 返回 JSON-RPC error | `ERROR` | 是 |
| 内部异常 | `ERROR` | 是 |
| 流超时、客户端断开 | `OK` | 否 |
| Bot 断开 | `ERROR` | 是 |

```go
// 出错时
span.SetStatus(codes.Error, "send to bot failed")
span.RecordError(err)
```

---

## 实施优先级

### 第一阶段（当前可落地）

| 优先级 | 内容 | 原因 |
|--------|------|------|
| P0 | 基础设施（config + provider 重构 + otelgin） | 所有 span 的前提 |
| P0 | `chat.turn` + `chat.response.stream` | 核心业务主轴，排障价值最高 |
| P1 | `chat.session.resolve` + `session.*` | 会话域完整性 |
| P1 | `chat.bot.availability_check` + `chat.bot.dispatch` | 正向链路完整性 |
| P1 | `chat.cancel` context 传递修复 | 避免断链 |
| P2 | `bot.connection.register` + `bot.connection.unregister` | Bot 连接生命周期 |

### 第二阶段（需协议扩展）

| 优先级 | 内容 | 前置条件 |
|--------|------|----------|
| P0 | 跨 Worker context propagation | `workerInboxMessage` 增加 `traceparent` 字段 |
| P0 | `bot.message.receive` + `bot.event.translate` + `chat.message.deliver` | Bot 插件回传 `requestId`/`traceparent` 或服务端 turn-context 映射 |

### 可选项

| 内容 | 说明 |
|------|------|
| `bot.connection.heartbeat_check` | 高频轮询（1秒/次），建议改为异常采样或纯 metric |

---

## 验收标准

### 第一阶段完成标志

- [ ] `OTLP_TRACES_ENABLED=true` 时，TracerProvider 正常初始化
- [ ] 所有 HTTP 请求自动生成 `HTTP {method}` span
- [ ] `POST /bridge/chat` 生成完整的 `chat.turn` → `chat.response.stream` 链路
- [ ] Session CRUD 操作生成对应 span
- [ ] Bot 连接/断开生成 `bot.connection.*` span
- [ ] 所有 span 包含 `astron.*` 命名空间的自定义属性
- [ ] Error 场景正确设置 `span.Status` 和 `span.RecordError`

### 第二阶段完成标志

- [ ] Bot 回复消息能关联到原始 `chat.turn` trace
- [ ] 跨 Worker 的消息路由保持 trace 连续性
- [ ] `bot.message.receive` → `chat.message.deliver` 链路完整
