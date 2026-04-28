# Chat SSE OTel 日志埋点方案

## 目标

在 Chat SSE 请求生命周期的关键节点发射结构化 OTel Log Record，上报到日志服务后由下游清洗出成功率、延时分布、错误分布等观测指标。

## 日志 Body 与触发时机

每条日志的 `Body` 为固定字符串 `chat.request.completed`，在 **SSE 请求结束时** 统一发射一条日志（无论成功或失败）。不在中间阶段发射，避免日志量膨胀。

触发点位于 `chatSSE()` 函数的所有 return 路径，包括：

| 退出场景 | close_reason | code |
|----------|-------------|------|
| 正常完成（收到 done 事件） | `done` | `0` |
| 客户端断开 | `client_disconnect` | `0` |
| Bot 断开 | `bot_disconnect` | `10201` |
| 流超时 | `timeout` | `10204` |
| 消费错误 | `error` | `10205` |
| 写入错误 | `write_error` | `10205` |
| Bot 返回 error 事件 | `error` | `10700` |
| 请求校验失败（JSON 解析、空消息等） | `""` | 对应错误码 |
| Bot 不在线 | `""` | `10201` |
| Session 不存在/创建失败 | `""` | `10400`/`10401` |
| 发送到 Bot 失败 | `""` | `10203` |

> `client_disconnect` 与 `done` 均为 `code=0`，通过 `close_reason` 区分。两者在成功率统计中都算成功（与现有 metrics 行为一致）。

## Log Record 属性

遵循 `docs/otel-logs-reference.md` 规范：

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `SeverityText` | string | 根据 code 判断 | 见下方 Severity 判定规则 |
| `Body` | string | 固定 | `chat.request.completed` |
| `token_id` | string | `c.GetString("token")` | Token 完整值 |
| `session_id` | string | 变量 `sessionID` | 会话 ID，校验阶段失败时为空字符串 |
| `duration_ms` | float64 | `time.Since(start)` | 请求总耗时（从 middleware 注入的 `metrics_start` 计算） |
| `ttfb_ms` | float64 | 首字节时间差 | 从请求开始到写入第一个 SSE 事件（session 事件）的耗时；未进入流阶段时为 `0` |
| `code` | int | 业务错误码 | `0` 表示成功，其余见错误码表 |
| `close_reason` | string | 变量 `closeReason` | 流关闭原因，未进入流阶段时为空字符串 |
| `pod_ip` | string | `c.GetString("metrics_ip")` | Pod IP（middleware 注入） |
| `trace_id` | string | `span.SpanContext().TraceID()` | 当前 turn span 的 Trace ID |

### Severity 判定规则

```
code == 0                                    → INFO   （成功，含 client_disconnect）
code ∈ {10200, 10201, 10202, 10204, 10206,   → WARN   （客户端错误，不影响服务健康）
        10300, 10301, 10302, 10303, 10304,
        10400}
code ∈ {10203, 10205, 10401, 10700}          → ERROR  （服务端/Bot 错误，需关注）
```

> 注意 `10205`（CodeChatInternalError）是服务端错误，归 ERROR 而非 WARN。

## 实现方案

### 1. 在 provider.go 增加 Log Provider 初始化

在现有 `telemetry.Init()` 中增加 `initLogs()`，复用 `OTLP_*` 配置链路，受 `OTLP_LOGS_ENABLED` 开关控制：

```go
// internal/infra/telemetry/provider.go

import (
    "go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
    sdklog "go.opentelemetry.io/otel/sdk/log"
    otellog "go.opentelemetry.io/otel/log/global"
)

var loggerProvider *sdklog.LoggerProvider

func Init(ctx context.Context, otlpCfg config.OtlpConfig) error {
    // ... 现有 metrics/traces 初始化 ...

    if otlpCfg.LogsEnabled {
        if err := initLogs(ctx, otlpCfg, res); err != nil {
            return err
        }
    }

    return nil
}

func initLogs(ctx context.Context, otlpCfg config.OtlpConfig, res *resource.Resource) error {
    opts := []otlploggrpc.Option{
        otlploggrpc.WithEndpoint(otlpCfg.Endpoint),
    }
    if otlpCfg.Insecure {
        opts = append(opts, otlploggrpc.WithInsecure())
    }

    exporter, err := otlploggrpc.New(ctx, opts...)
    if err != nil {
        return err
    }

    loggerProvider = sdklog.NewLoggerProvider(
        sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)),
        sdklog.WithResource(res),
    )
    otellog.SetLoggerProvider(loggerProvider)

    log.Info().
        Str("service", otlpCfg.ServiceName).
        Str("endpoint", otlpCfg.Endpoint).
        Msg("OTLP logs exporter initialised (gRPC)")

    return nil
}

func Shutdown() {
    // ... 现有 metrics/traces shutdown ...

    if loggerProvider != nil {
        if err := loggerProvider.Shutdown(ctx); err != nil {
            log.Error().Err(err).Msg("OTLP logs shutdown error")
        }
        loggerProvider = nil
    }
}
```

### 2. Logger 与日志发射函数

```go
// internal/infra/telemetry/chat_log.go
package telemetry

import (
    "context"
    "time"

    "go.opentelemetry.io/otel/log"
    otellog "go.opentelemetry.io/otel/log/global"
)

var ChatLogger log.Logger

func EnsureLogger() {
    ChatLogger = otellog.GetLoggerProvider().Logger("astron-claw/chat")
}

type ChatLogRecord struct {
    TokenID     string
    SessionID   string
    DurationMs  float64
    TTFBMs      float64
    Code        int
    CloseReason string
    PodIP       string
    TraceID     string
}

func EmitChatLog(ctx context.Context, rec ChatLogRecord) {
    severity, severityText := severityFromCode(rec.Code)

    var r log.Record
    r.SetTimestamp(time.Now())
    r.SetSeverity(severity)
    r.SetSeverityText(severityText)
    r.SetBody(log.StringValue("chat.request.completed"))
    r.AddAttributes(
        log.String("token_id", rec.TokenID),
        log.String("session_id", rec.SessionID),
        log.Float64("duration_ms", rec.DurationMs),
        log.Float64("ttfb_ms", rec.TTFBMs),
        log.Int("code", rec.Code),
        log.String("close_reason", rec.CloseReason),
        log.String("pod_ip", rec.PodIP),
        log.String("trace_id", rec.TraceID),
    )

    ChatLogger.Emit(ctx, r)
}

// severityFromCode 同时返回 severity enum 和 SeverityText 字符串。
func severityFromCode(code int) (log.Severity, string) {
    if code == 0 {
        return log.SeverityInfo, "INFO"
    }
    if isClientError(code) {
        return log.SeverityWarn, "WARN"
    }
    return log.SeverityError, "ERROR"
}

var clientErrorCodes = map[int]bool{
    10200: true, // CodeChatEmptyMessage
    10201: true, // CodeChatNoBot
    10202: true, // CodeChatInvalidReq
    10204: true, // CodeChatStreamTimeout
    10206: true, // CodeChatStreamUnsupported
    10300: true, // CodeMediaFileTooLarge
    10301: true, // CodeMediaInvalidFile
    10302: true, // CodeMediaBadURLScheme
    10303: true, // CodeMediaUnsupportedType
    10304: true, // CodeMediaTooMany
    10400: true, // CodeSessionNotFound
}

func isClientError(code int) bool {
    return clientErrorCodes[code]
}
```

### 3. 在 chatSSE 中集成

在 `sse.go` 的 `chatSSE()` 函数中，通过一个 `defer` 统一发射日志。**不维护独立的 `logCode` 变量**，defer 中直接读取 `metrics_code`（各 return 路径已设置），避免遗漏：

```go
func (app *App) chatSSE(c *gin.Context) {
    tokenStr := c.GetString("token")
    // ... 现有代码 ...

    ctx, turnSpan := sseTracer.Start(c.Request.Context(), "chat.turn", ...)
    defer turnSpan.End()

    // 日志状态变量
    var (
        logSession  string  // session 解析成功后赋值
        logTTFBMs   float64 // 写入首个 SSE 事件后赋值
        closeReason string  // 进入流阶段后各路径赋值（复用现有变量）
    )

    // 统一日志发射
    defer func() {
        // 从 metrics_code 读取 code（所有 return 路径已设置）
        logCode := 0
        if raw, exists := c.Get("metrics_code"); exists {
            if codeStr, ok := raw.(string); ok {
                logCode, _ = strconv.Atoi(codeStr)
            }
        }

        // 安全读取 metrics_start
        var durationMs float64
        if raw, exists := c.Get("metrics_start"); exists {
            if startTime, ok := raw.(time.Time); ok {
                durationMs = float64(time.Since(startTime).Milliseconds())
            }
        }

        traceID := turnSpan.SpanContext().TraceID().String()

        telemetry.EmitChatLog(ctx, telemetry.ChatLogRecord{
            TokenID:     tokenStr,
            SessionID:   logSession,
            DurationMs:  durationMs,
            TTFBMs:      logTTFBMs,
            Code:        logCode,
            CloseReason: closeReason,
            PodIP:       c.GetString("metrics_ip"),
            TraceID:     traceID,
        })
    }()

    // ... 现有校验、session 解析等代码 ...
    // session 解析成功后：logSession = sessionID
    // 写入 session 事件后：logTTFBMs = float64(time.Since(startTime).Milliseconds())
    // closeReason 复用现有变量，各流退出路径已赋值
}
```

关键改动点：
- `logCode` **不再手工维护**，defer 中从 `c.Get("metrics_code")` 读取，与现有 metrics 保持一致
- `metrics_start` 读取增加 exists/类型双重守卫
- `closeReason` 复用现有变量（`sse.go:220`），不引入额外状态
- session 解析成功后赋值 `logSession = sessionID`
- 写入 session 事件后记录 `logTTFBMs`

### 4. main.go 改动

无需改动 `main.go`。日志初始化已集成在 `telemetry.Init()` 中，受 `OTLP_LOGS_ENABLED` 开关控制。仅需在 `telemetry.Init()` 后调用 `telemetry.EnsureLogger()`（与现有 `EnsureInstruments()` 并列）：

```go
// cmd/server/main.go（现有位置 line 46 之后）
telemetry.EnsureLogger()
```

## 下游清洗参考

日志上报后，下游可基于这些属性清洗出以下观测数据：

| 观测指标 | 清洗逻辑 |
|---------|---------|
| 请求成功率 | `count(code=0) / count(*)` 按 `pod_ip` 分组 |
| 请求延时 P50/P99 | `duration_ms` 分位数，按 `pod_ip` 分组 |
| 首字节延时 P50/P99 | `ttfb_ms`（过滤 `ttfb_ms > 0`），按 `pod_ip` 分组 |
| 错误码分布 | `count` group by `code`，过滤 `code != 0` |
| 关闭原因分布 | `count` group by `close_reason`，过滤 `close_reason != ""` |
| 用户中断率 | `count(close_reason="client_disconnect") / count(close_reason != "")` |
| Token 维度成功率 | `count(code=0) / count(*)` 按 `token_id` 分组 |
| Pod 健康度 | 错误率按 `pod_ip` 分组，用于发现单 Pod 异常 |

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `internal/infra/telemetry/provider.go` | 修改，增加 `initLogs()` + shutdown |
| `internal/infra/telemetry/chat_log.go` | 新增，`EnsureLogger` + `EmitChatLog` + `ChatLogRecord` + severity 判定 |
| `internal/router/sse.go` | 修改，`chatSSE()` 增加 defer 日志发射 |
| `cmd/server/main.go` | 修改，增加 `telemetry.EnsureLogger()` 调用 |
| `go.mod` | 修改，增加 `go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc` 等依赖 |
