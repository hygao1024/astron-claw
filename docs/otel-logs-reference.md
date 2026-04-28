# OTel Logs 参考文档

Resource: `service.name` = `astron-claw`（环境变量 `OTLP_SERVICE_NAME`，默认值 `astron-claw`）

开关：`OTLP_LOGS_ENABLED`（需同时开启 `OTLP_ENABLED`）

Logger name: `astron-claw/chat`

## 触发时机

在 SSE 请求结束时统一发射一条日志（无论成功或失败），触发点位于 `chatSSE()` 的所有 return 路径。

## Log Record 属性

| 字段 | 类型 | 说明 |
|------|------|------|
| `SeverityText` | string | 日志级别：`INFO` / `WARN` / `ERROR`（见下方判定规则） |
| `Body` | string | 固定值 `chat.request.completed` |
| `token_id` | string | Token 完整值 |
| `session_id` | string | 会话 ID，校验阶段失败时为空字符串 |
| `duration_ms` | float64 | 请求总耗时（毫秒），从 middleware 注入的 `metrics_start` 计算 |
| `ttfb_ms` | float64 | 首字节耗时（毫秒），从请求开始到写入第一个 SSE 事件；未进入流阶段时为 `0` |
| `code` | int | 业务错误码，`0` 表示成功 |
| `close_reason` | string | 流关闭原因：`done` / `client_disconnect` / `bot_disconnect` / `timeout` / `error` / `write_error`，未进入流阶段时为空字符串 |
| `pod_ip` | string | Pod IP（middleware 注入） |
| `trace_id` | string | 当前 turn span 的 Trace ID |

## Severity 判定规则

```
code == 0                                    → INFO   （成功，含 client_disconnect）
code ∈ {10200, 10201, 10202, 10204, 10206,   → WARN   （客户端错误，不影响服务健康）
        10300, 10301, 10302, 10303, 10304,
        10400}
code ∈ {10203, 10205, 10401, 10700}          → ERROR  （服务端/Bot 错误，需关注）
```

> `10205`（CodeChatInternalError）是服务端错误，归 ERROR 而非 WARN。