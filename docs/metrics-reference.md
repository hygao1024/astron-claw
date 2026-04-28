# Astron-Claw 指标参考手册

---

## 概述

Astron-Claw 使用 OpenTelemetry SDK 采集指标，通过 OTLP gRPC 协议推送到 OTel Collector，Collector 将指标暴露为 Prometheus 格式供抓取。

---

### 指标清单

| 指标名 | 类型 | 说明 | 维度 |
|--------|------|------|------|
| `bridge.chat.requests` | Counter | 请求总数（所有 HTTP 接口） | `func`, `ip`, `code` |
| `bridge.chat.request.duration` | Histogram | 请求处理耗时（普通 HTTP 为总耗时，SSE 为首个 Redis 结果延迟） | `func`, `ip`, `code` |
| `bridge.chat.stream.duration` | Histogram | SSE 流持续时间（仅 `/bridge/chat`） | `func`, `ip`, `code`, `close_reason` |
| `bridge.bot.alive_count` | Gauge | 30 秒内存活的 Bot 数量 | 无 |


**`code` 取值对照：**

| code | 说明 | 对应常量 |
|------|------|----------|
| `0` | 请求成功 | `CodeSuccess` |
| `10001` | Token 无效或缺失 | `CodeAuthInvalidToken` |
| `10002` | 缺少认证信息 | `CodeAuthMissingAuth` |
| `10003` | Admin session 无效 | `CodeAuthInvalidSession` |
| `10004` | 未授权 | `CodeAuthUnauthorized` |
| `10005` | 密码错误 | `CodeAuthWrongPassword` |
| `10100` | Admin 密码已设置 | `CodeAdminPasswordExists` |
| `10101` | Admin 密码过短 | `CodeAdminPasswordShort` |
| `10200` | 空消息 | `CodeChatEmptyMessage` |
| `10201` | Token 对应的 Bot 未连接 | `CodeChatNoBot` |
| `10202` | 参数错误（JSON 解析失败） | `CodeChatInvalidReq` |
| `10203` | 发送消息到 Bot 失败 | `CodeChatSendFailed` |
| `10204` | SSE 流超时 | `CodeChatStreamTimeout` |
| `10205` | Chat 内部错误 | `CodeChatInternalError` |
| `10206` | 不支持流式响应 | `CodeChatStreamUnsupported` |
| `10300` | 媒体文件过大 | `CodeMediaFileTooLarge` |
| `10301` | 媒体文件无效 | `CodeMediaInvalidFile` |
| `10302` | 媒体 URL scheme 无效 | `CodeMediaBadURLScheme` |
| `10303` | 不支持的媒体类型 | `CodeMediaUnsupportedType` |
| `10304` | 媒体数量超限 | `CodeMediaTooMany` |
| `10400` | 指定的 Session ID 不存在 | `CodeSessionNotFound` |
| `10401` | Session 创建失败 | `CodeSessionCreateFailed` |
| `10500` | Token 不存在 | `CodeTokenNotFound` |
| `10600` | WebSocket Token 无效 | `CodeWSInvalidToken` |
| `10601` | WebSocket Token 已删除 | `CodeWSTokenDeleted` |
| `10602` | WebSocket 服务器重启 | `CodeWSServerRestart` |
| `10603` | WebSocket 连接被新连接驱逐 | `CodeWSEvicted` |
| `10700` | Bot 未知错误 | `CodeBotUnknownError` |

**`func` 取值对照：**

取值为 Gin 路由路径（通过 `c.FullPath()` 获取）。

| func | 说明 |
|------|------|
| `/api/health` | 健康检查 |
| `/api/token` | 创建 Token |
| `/api/token/validate` | 验证 Token |
| `/api/media/upload` | 上传媒体文件 |
| `/bridge/chat` | Chat SSE 请求 |
| `/bridge/chat/sessions` | 列出 / 创建 Session |
| `/api/admin/tokens` | 列出 / 创建 Token（Admin） |
| `/api/admin/tokens/:token` | 更新 / 删除 Token（Admin） |
| `/api/admin/auth/setup` | 设置 Admin 密码 |
| `/api/admin/auth/login` | Admin 登录 |
| `/api/admin/auth/logout` | Admin 登出 |
| `/api/admin/auth/status` | Admin 认证状态 |
| `/api/admin/cleanup` | 清理过期数据 |

**注意**：`/bridge/bot` WebSocket 接口不记录在这些指标中。未匹配路由（`c.FullPath()` 为空）的请求不记录指标。