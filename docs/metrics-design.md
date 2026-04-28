# Astron-Claw 统一指标采集设计文档

## 1. 设计目标

将现有的 4 个 OTel 指标从仅覆盖 `/bridge/chat` 扩展到所有 HTTP 接口，通过 `func` 维度区分不同接口，实现全局统一的可观测性。

### 1.1 核心原则

- **指标名称不变**：保持现有 4 个指标名称，确保向后兼容
- **通用化设计**：4 个指标适用于所有 HTTP 接口，不再局限于 chat SSE
- **维度统一**：所有接口使用相同的 attribute 集合（`func`, `ip`, `code`）
- **中间件实现**：通过 Gin 中间件自动采集，避免在每个 handler 中重复代码
- **语义一致性**：`code` 统一使用业务错误码，不混用 HTTP status code

## 2. 指标定义

### 2.1 指标清单

| 指标名 | 类型 | 语义 | 维度 |
|--------|------|------|------|
| `bridge.chat.requests` | Counter | HTTP 请求总数（所有接口） | `func`, `ip`, `code` |
| `bridge.chat.request.duration` | Histogram | HTTP 请求处理耗时（从接收到首字节响应） | `func`, `ip`, `code` |
| `bridge.chat.stream.duration` | Histogram | SSE 流持续时间（仅 `/bridge/chat`） | `func`, `ip`, `code` |
| `bridge.chat.active_streams` | UpDownCounter | 当前活跃 SSE 流数量（仅 `/bridge/chat`） | `func`, `ip` |

**注意**：虽然指标名称保留 `bridge.chat.` 前缀（向后兼容），但语义已扩展为全局 HTTP 指标。

### 2.2 维度说明

#### `func` — 接口标识

取值为 Gin handler 函数名（通过 `c.HandlerName()` 提取末段并去除 `-fm` 后缀），例如：

| 路由 | `func` 取值 |
|------|-------------|
| `GET /api/health` | `healthCheck` |
| `POST /api/token` | `createToken` |
| `POST /api/token/validate` | `validateToken` |
| `POST /api/media/upload` | `uploadMedia` |
| `POST /bridge/chat` | `chatSSE` |
| `GET /bridge/chat/sessions` | `listSessions` |
| `POST /bridge/chat/sessions` | `createSession` |
| `GET /api/admin/tokens` | `listTokens` |
| `POST /api/admin/tokens` | `adminCreateToken` |
| `PATCH /api/admin/tokens/:token` | `adminUpdateToken` |
| `DELETE /api/admin/tokens/:token` | `adminDeleteToken` |
| `POST /api/admin/auth/setup` | `adminAuthSetup` |
| `POST /api/admin/auth/login` | `adminAuthLogin` |
| `POST /api/admin/auth/logout` | `adminAuthLogout` |
| `GET /api/admin/auth/status` | `adminAuthStatus` |
| `POST /api/admin/cleanup` | `adminCleanup` |

**实现方式**：通过 `extractFuncName(c.HandlerName())` 提取，从完整的 handler 名称（如 `astron-claw/backend/internal/router.(*App).chatSSE-fm`）中提取最后一段函数名并去除 Gin 的 `-fm` 后缀。

#### `ip` — 服务端 Pod IP

取值为当前处理请求的 Pod IP，用于多实例场景下区分哪个实例处理了请求，便于流量分布分析和问题定位。

**实现方式**：通过环境变量 `POD_IP` 注入（Kubernetes Downward API），服务启动时读取一次，作为常量写入所有指标

#### `code` — 业务错误码

取值为统一的业务错误码（`backend/internal/model/errors.go` 中定义），**不使用 HTTP status code**。

| code | 说明 | 对应常量 | 适用接口 |
|------|------|----------|----------|
| `0` | 请求成功 | `CodeSuccess` | 所有接口 |
| `10001` | Token 无效或缺失 | `CodeAuthInvalidToken` | 需要 token auth 的接口 |
| `10002` | 缺少认证信息 | `CodeAuthMissingAuth` | 需要认证的接口 |
| `10003` | Admin session 无效 | `CodeAuthInvalidSession` | Admin 接口 |
| `10004` | 未授权 | `CodeAuthUnauthorized` | Admin 接口 |
| `10005` | 密码错误 | `CodeAuthWrongPassword` | `/api/admin/auth/login` |
| `10100` | Admin 密码已设置 | `CodeAdminPasswordExists` | `/api/admin/auth/setup` |
| `10101` | Admin 密码过短 | `CodeAdminPasswordShort` | `/api/admin/auth/setup` |
| `10200` | 空消息 | `CodeChatEmptyMessage` | `/bridge/chat` |
| `10201` | Token 对应的 Bot 未连接 | `CodeChatNoBot` | `/bridge/chat` |
| `10202` | 参数错误（JSON 解析失败） | `CodeChatInvalidReq` | 所有接口 |
| `10203` | 发送消息到 Bot 失败 | `CodeChatSendFailed` | `/bridge/chat` |
| `10204` | SSE 流超时 | `CodeChatStreamTimeout` | `/bridge/chat` |
| `10205` | Chat 内部错误 | `CodeChatInternalError` | `/bridge/chat` |
| `10206` | 不支持流式响应 | `CodeChatStreamUnsupported` | `/bridge/chat` |
| `10300` | 媒体文件过大 | `CodeMediaFileTooLarge` | `/api/media/upload` |
| `10301` | 媒体文件无效 | `CodeMediaInvalidFile` | `/api/media/upload` |
| `10302` | 媒体 URL scheme 无效 | `CodeMediaBadURLScheme` | `/api/media/upload`, `/bridge/chat` |
| `10303` | 不支持的媒体类型 | `CodeMediaUnsupportedType` | `/api/media/upload`, `/bridge/chat` |
| `10304` | 媒体数量超限 | `CodeMediaTooMany` | `/bridge/chat` |
| `10400` | Session 不存在 | `CodeSessionNotFound` | `/bridge/chat` |
| `10401` | Session 创建失败 | `CodeSessionCreateFailed` | `/bridge/chat/sessions` |
| `10500` | Token 不存在 | `CodeTokenNotFound` | `/api/admin/tokens/:token` |
| `10600` | WebSocket Token 无效 | `CodeWSInvalidToken` | `/bridge/bot` |
| `10601` | WebSocket Token 已删除 | `CodeWSTokenDeleted` | `/bridge/bot` |
| `10602` | 服务器重启 | `CodeWSServerRestart` | `/bridge/bot` |
| `10603` | 被新连接驱逐 | `CodeWSEvicted` | `/bridge/bot` |
| `10700` | Bot 未知错误 | `CodeBotUnknownError` | `/bridge/chat` |

**特殊规则**：
- 成功响应（HTTP 200/201/204）统一记录为 `code=0`
- 未捕获的 panic 或未分类错误：当前代码中没有统一的"未知错误"码，建议在 handler 中显式处理或返回具体错误码
- 中间件层错误（如 token auth 失败）记录为对应的业务错误码

### 2.3 指标语义细化

#### `bridge.chat.requests`

- **记录时机**：每个 HTTP 请求结束时（无论成功或失败）
- **记录位置**：Metrics 中间件的 `defer` 中
- **特殊情况**：
  - 中间件层拦截的请求（如 token auth 失败）也会记录
  - WebSocket 接口（`/bridge/bot`）**不记录**到此指标（长连接特性与 HTTP 请求语义不符，未来单独设计 WebSocket 指标）

#### `bridge.chat.request.duration`

- **记录时机**：每个 HTTP 请求结束时
- **计时起点**：请求进入 Metrics 中间件时（`time.Now()`）
- **计时终点**：
  - 普通 HTTP 请求：响应完全写入时（中间件 `defer` 中，测量的是**总耗时**，非 TTFB）
  - SSE 流请求（`/bridge/chat`）：进入 SSE 流之前（首字节延迟，handler 中手动记录）
- **单位**：秒（float64）
- **Bucket 边界**：`[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]`

**注意**：对于普通 HTTP 请求，此指标测量的是完整请求处理时间，不是严格意义的 TTFB（Time To First Byte）。如需精确 TTFB，需包装 `gin.ResponseWriter` 追踪首次 `Write()` 调用。

#### `bridge.chat.stream.duration`

- **记录时机**：SSE 流结束时（仅 `/bridge/chat`）
- **计时起点**：进入 SSE 流时（发送首个 SSE 事件前）
- **计时终点**：SSE 流关闭时（客户端断开、Bot 断开、超时、错误等）
- **单位**：秒（float64）
- **Bucket 边界**：`[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0]`
- **维度**：`func`, `ip`, `code`, `close_reason`（新增维度）
  - `close_reason` 取值：
    - `done` — 正常结束
    - `client_disconnect` — 客户端断开
    - `bot_disconnect` — Bot 断开
    - `timeout` — 超时
    - `write_error` — 写入错误
    - `error` — 其他错误

#### `bridge.chat.active_streams`

- **记录时机**：SSE 流开始和结束时（仅 `/bridge/chat`）
- **+1 时机**：进入 SSE 流时
- **-1 时机**：SSE 流关闭时（`defer` 中）
- **维度**：仅 `func` 和 `ip`（无 `code`，因为流可能持续很久）

## 3. 实现方案

### 3.1 架构设计

```
HTTP Request
    ↓
[Metrics Middleware] ← 新增，记录 requests + request.duration（必须在 Recovery 之前）
    ↓
[gin.Recovery()]
    ↓
[CORS Middleware]
    ↓
[Token Auth Middleware]
    ↓
[Route Handler]
    ↓
    ├─ 普通 HTTP 响应 → 返回
    └─ SSE 流 (/bridge/chat) → 记录 active_streams + stream.duration
```

### 3.2 Metrics 中间件设计

新增 `backend/internal/middleware/metrics.go`，实现统一的指标采集逻辑。

#### 核心逻辑

```go
func MetricsMiddleware(podIP string) gin.HandlerFunc {
    return func(c *gin.Context) {
        // 跳过 WebSocket 接口（升级后是长连接，HTTP metrics 无意义）
        if c.FullPath() == "/bridge/bot" {
            c.Next()
            return
        }
        
        start := time.Now()
        funcName := extractFuncName(c.HandlerName())
        
        // 在 context 中注入 metrics 上下文，供 handler 使用
        c.Set("metrics_start", start)
        c.Set("metrics_func", funcName)
        c.Set("metrics_ip", podIP)
        
        // 默认 code=0（成功）
        code := "0"
        
        defer func() {
            // 从 context 中读取 handler 设置的 code（如果有）
            if handlerCode, exists := c.Get("metrics_code"); exists {
                code = handlerCode.(string)
            } else if c.Writer.Status() >= 400 {
                // 如果 handler 未设置 code，根据 HTTP status 推断
                code = inferCodeFromStatus(c.Writer.Status())
            }
            
            duration := time.Since(start).Seconds()
            
            // 记录 requests
            telemetry.ChatRequestTotal.Add(c.Request.Context(), 1,
                metric.WithAttributes(
                    attribute.String("func", funcName),
                    attribute.String("ip", podIP),
                    attribute.String("code", code),
                ))
            
            // 记录 request.duration（仅当 handler 未标记为 SSE 流时）
            if _, isSSE := c.Get("metrics_sse_stream"); !isSSE {
                telemetry.ChatRequestDuration.Record(c.Request.Context(), duration,
                    metric.WithAttributes(
                        attribute.String("func", funcName),
                        attribute.String("ip", podIP),
                        attribute.String("code", code),
                    ))
            }
        }()
        
        c.Next()
    }
}

func extractFuncName(full string) string {
    // 从完整的 handler 名称提取函数名
    // 例如: "astron-claw/backend/internal/router.(*App).chatSSE-fm" → "chatSSE"
    if i := strings.LastIndex(full, "."); i >= 0 {
        name := full[i+1:]
        name = strings.TrimSuffix(name, "-fm")
        return name
    }
    return full
}

func inferCodeFromStatus(status int) string {
    switch status {
    case 401:
        return strconv.Itoa(model.CodeAuthInvalidToken)
    case 403:
        return strconv.Itoa(model.CodeAuthUnauthorized)
    case 404:
        return strconv.Itoa(model.CodeChatInternalError)  // 或其他合适的错误码
    case 500:
        return strconv.Itoa(model.CodeChatInternalError)
    default:
        return "0"
    }
}
```

**注意**：`inferCodeFromStatus` 仅作为兜底机制，实际应用中应在 handler 中显式设置 `metrics_code`，避免依赖 HTTP status 推断。

#### Handler 中设置 code

Handler 在返回错误响应时，通过 `c.Set("metrics_code", ...)` 设置业务错误码：

```go
// 示例：token auth 失败
c.Set("metrics_code", strconv.Itoa(model.CodeAuthInvalidToken))
model.ErrorResponse(c, model.ErrAuthInvalidToken)
return
```

**推荐方案**：为避免每个 handler 手动 `c.Set("metrics_code", ...)`，建议封装统一的错误响应 helper，在返回错误时自动设置 `metrics_code`：

```go
// metricsErrorResponse 统一设置 metrics_code 并返回错误响应
func metricsErrorResponse(c *gin.Context, err model.AppError, detail ...string) {
    c.Set("metrics_code", strconv.Itoa(err.Code))
    model.ErrorResponse(c, err, detail...)
}
```

#### SSE 流特殊处理

`/bridge/chat` handler 需要：
1. 在进入 SSE 流前，标记 `c.Set("metrics_sse_stream", true)`，避免中间件重复记录 `request.duration`
2. 在进入 SSE 流前，手动记录 `request.duration`（首字节延迟）
3. 在 SSE 流开始时，记录 `active_streams +1`
4. 在 SSE 流结束时，记录 `active_streams -1` 和 `stream.duration`

### 3.3 修改清单

#### 新增文件

- `backend/internal/middleware/metrics.go` — Metrics 中间件实现

#### 修改文件

- `backend/internal/router/router.go` — 注册 Metrics 中间件；`adminAuthMiddleware()` 中的 `model.ErrorResponse` 调用需改为 `metricsErrorResponse`，确保认证失败时设置 `metrics_code`
- `backend/internal/router/sse.go` — 调整 `chatSSE` handler，适配新的中间件
- `backend/internal/router/tokens.go` — 将裸 `c.JSON(500, ...)` 替换为 `metricsErrorResponse` 或显式设置 `metrics_code`
- `backend/internal/router/admin.go` — 将多处裸 `c.JSON(500, ...)` 替换为 `metricsErrorResponse`（涉及约 6 处）
- `backend/internal/router/admin_auth.go` — 统一错误响应：部分已使用 `model.ErrorResponse`，部分仍为裸 `c.JSON(500/400, ...)`，需全部改为 `metricsErrorResponse`
- `backend/internal/router/media.go` — 添加 `metrics_code` 设置
- `backend/internal/middleware/token_auth.go` — 当前使用 `c.AbortWithStatusJSON(401, gin.H{"code": 401, ...})`，需改为使用 `model.ErrorResponse` + 设置 `metrics_code` 为业务错误码（如 `CodeAuthInvalidToken`），而非 HTTP status code
- `backend/cmd/server/main.go` — 读取 `POD_IP` 环境变量，传递给 Metrics 中间件
- `backend/internal/infra/telemetry/metrics.go` — 更新注释，说明指标适用于所有接口

#### 不修改文件

- `backend/internal/infra/telemetry/provider.go` — 无需修改
- `backend/internal/config/config.go` — 无需添加配置项（Pod IP 通过环境变量直接读取）

### 3.4 中间件注册顺序

```go
// backend/cmd/server/main.go
func main() {
    // ... 初始化逻辑
    
    // 读取 Pod IP
    podIP := os.Getenv("POD_IP")
    if podIP == "" {
        podIP = "unknown"
    }
    
    // 设置路由
    r := router.SetupRouter(app, podIP)
    
    // ... 启动服务
}

// backend/internal/router/router.go
func SetupRouter(app *App, podIP string) *gin.Engine {
    r := gin.New()
    r.Use(middleware.MetricsMiddleware(podIP))  // ← 新增，必须在 Recovery 之前
    r.Use(gin.Recovery())
    r.Use(middleware.CORS())
    
    // ... 路由注册
}
```

**顺序说明**：
- **Metrics 中间件必须在 `gin.Recovery()` 之前**：Gin 中间件使用 `defer` 栈（LIFO），如果 Recovery 在前，panic 发生时 Recovery 的 defer 先执行（捕获 panic 并写入 500 响应），Metrics 的 defer 后执行（此时响应已写入，无法再设置 `metrics_code`）。将 Metrics 放在最前，其 defer 最后执行，可以在 Recovery 处理完 panic 后读取 `c.Get("metrics_code")` 并记录指标
- Metrics 中间件在 CORS 之后可能更合理（避免 OPTIONS 预检请求干扰指标），但由于需要在 Recovery 之前，当前设计将其放在最前
- Metrics 中间件在 Token Auth 之前，确保认证失败的请求也被记录

**Panic 场景处理**：
- 当 handler 或后续中间件发生 panic 时，`gin.Recovery()` 会捕获并调用 `c.AbortWithStatus(500)`
- Metrics 中间件的 `defer` 需要检测这种情况：如果 `c.Get("metrics_code")` 未设置且 `c.Writer.Status() == 500`，推断为 panic 导致的 500，设置 `metrics_code` 为 `CodeChatInternalError`（10205）或新增专用错误码
- 或者在 Metrics 中间件内部也添加 `recover()` 逻辑，在捕获 panic 后设置 `metrics_code` 再 re-panic，让 `gin.Recovery()` 继续处理

## 4. 配置项

### 4.1 新增环境变量

```bash
# 服务端 Pod IP，通过 Kubernetes Downward API 注入
POD_IP=10.0.1.23
```

Kubernetes Deployment 配置示例：

```yaml
env:
  - name: POD_IP
    valueFrom:
      fieldRef:
        fieldPath: status.podIP
```

本地开发时不设置 `POD_IP`，指标中 `ip` 维度将记录为 `"unknown"`。

## 5. 测试计划

### 5.1 单元测试

- `middleware/metrics_test.go` — 测试 Metrics 中间件的基本功能
  - 测试 `func` 维度正确提取
  - 测试 `ip` 维度正确注入 Pod IP
  - 测试 `code` 维度正确设置（成功、失败、中间件拦截）
  - 测试 SSE 流标记生效
  - 测试 `/bridge/bot` WebSocket 接口被跳过

### 5.2 集成测试

- 启动服务，调用所有接口，验证指标是否正确记录
- 使用 Prometheus 查询验证指标维度和取值

### 5.3 性能测试

- 使用 `wrk` 或 `hey` 压测，验证 Metrics 中间件对性能的影响

## 6. 向后兼容性

### 6.1 指标名称

保持现有 4 个指标名称不变（`bridge.chat.requests`、`bridge.chat.request.duration`、`bridge.chat.stream.duration`、`bridge.chat.active_streams`）。

### 6.2 维度变化

| 指标 | 原维度 | 新维度 | 变化 |
|------|--------|--------|------|
| `bridge.chat.requests` | `ip`, `code` | `func`, `ip`, `code` | 新增 `func` |
| `bridge.chat.request.duration` | `ip`, `code` | `func`, `ip`, `code` | 新增 `func` |
| `bridge.chat.stream.duration` | `ip`, `code`, `close_reason` | `func`, `ip`, `code`, `close_reason` | 新增 `func` |
| `bridge.chat.active_streams` | `ip` | `func`, `ip` | 新增 `func` |

**重要**：维度变化会导致 Prometheus 时间序列标识（metric name + label set）发生变化，**破坏向后兼容性**：

1. **现有查询失效**：Grafana 面板中的 PromQL 查询需要更新，添加 `func` 维度过滤或聚合
2. **告警规则失效**：现有告警规则需要更新，添加 `func` 维度处理
3. **历史数据断层**：新旧版本的指标无法直接对比（label set 不同），会出现数据断层

**注意**：文档早期版本错误地提到"移除 `status` 维度"，实际上原始实现中 `/bridge/chat` 使用的维度是 `ip` 和 `code`，从未使用过 `status`。

### 6.3 迁移步骤

**部署前准备**：
1. 备份现有 Grafana 面板和告警规则
2. 准备更新后的面板和告警配置（添加 `func` 维度处理）

**部署后操作**：
1. 部署新版本后，立即更新 Grafana 面板：
   - 对于只关注 `/bridge/chat` 的面板，添加 `func="/bridge/chat"` 过滤器
   - 对于需要查看所有接口的面板，使用 `by (func)` 分组或 `sum without (func)` 聚合
2. 更新告警规则，添加相应的 `func` 维度处理
3. 观察 1-2 天，确认指标和告警正常工作
4. 历史数据（部署前）和新数据（部署后）无法直接对比，需要分别查询

**PromQL 迁移示例**：

```promql
# 旧查询（部署前）
sum(rate(bridge_chat_requests_total{code="0"}[5m]))

# 新查询（部署后，仅统计 /bridge/chat）
sum(rate(bridge_chat_requests_total{func="/bridge/chat", code="0"}[5m]))

# 新查询（部署后，统计所有接口）
sum(rate(bridge_chat_requests_total{code="0"}[5m])) by (func)
```

## 7. 未来扩展

### 7.1 WebSocket 指标

当前设计仅覆盖 HTTP 接口，WebSocket 连接（`/bridge/bot`）的指标需要单独设计：

- `bridge.bot.connections` — Bot 连接总数
- `bridge.bot.active_connections` — 当前活跃 Bot 连接数
- `bridge.bot.messages` — Bot 消息总数
- `bridge.bot.message.duration` — Bot 消息处理耗时

### 7.2 业务指标

除了通用的 HTTP 指标，未来可以添加业务相关的指标：

- `bridge.token.usage` — Token 使用次数
- `bridge.session.duration` — Session 持续时间
- `bridge.media.uploads` — 媒体上传次数
- `bridge.media.size` — 媒体文件大小

### 7.3 分布式追踪

当前仅实现 Metrics，未来可以添加 Traces 和 Logs，实现完整的可观测性：

- 使用 OTel Trace SDK 记录请求链路
- 使用 OTel Log SDK 统一日志格式
- 通过 Trace ID 关联 Metrics、Traces、Logs

## 8. 参考资料

- [OpenTelemetry Metrics Specification](https://opentelemetry.io/docs/specs/otel/metrics/)
- [Prometheus Naming Best Practices](https://prometheus.io/docs/practices/naming/)
- [Gin Middleware Documentation](https://gin-gonic.com/docs/examples/custom-middleware/)
