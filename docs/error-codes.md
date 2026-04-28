# 错误码规范

## 概述

Astron Claw 使用标准化的业务错误码系统，从 10000 开始，按功能模块分段。

## 错误码结构

```go
type AppError struct {
    Code       int    // 业务错误码 (10000+)
    HTTPStatus int    // HTTP 状态码
    Message    string // 错误消息
}
```

## 错误码分段

| 范围 | 模块 | 说明 |
|------|------|------|
| 0 | 成功 | 请求成功 |
| 10001-10099 | 认证 | Token、Session、密码验证 |
| 10100-10199 | 管理员设置 | 密码设置相关 |
| 10200-10299 | Chat/SSE | 对话流相关 |
| 10300-10399 | 媒体 | 文件上传、URL 验证 |
| 10400-10499 | 会话 | Session 管理 |
| 10500-10599 | Token | Token CRUD |
| 10600-10699 | WebSocket | Bot 连接相关 |
| 10700-10799 | Bot | Bot 内部错误 |

## 完整错误码清单

### 认证错误 (10001-10099)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10001 | `CodeAuthInvalidToken` | 401 | Invalid or missing token |
| 10002 | `CodeAuthMissingAuth` | 401 | Missing authorization |
| 10003 | `CodeAuthInvalidSession` | 401 | Invalid admin session |
| 10004 | `CodeAuthUnauthorized` | 401 | Unauthorized |
| 10005 | `CodeAuthWrongPassword` | 401 | Wrong password |

### 管理员设置错误 (10100-10199)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10100 | `CodeAdminPasswordExists` | 400 | Password already set |
| 10101 | `CodeAdminPasswordShort` | 400 | Password too short |

### Chat/SSE 错误 (10200-10299)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10200 | `CodeChatEmptyMessage` | 400 | Empty message |
| 10201 | `CodeChatNoBot` | 400 | No bot connected |
| 10202 | `CodeChatInvalidReq` | 400 | Invalid request |
| 10203 | `CodeChatSendFailed` | 500 | Failed to send message to bot |
| 10204 | `CodeChatStreamTimeout` | 500 | Stream timeout |
| 10205 | `CodeChatInternalError` | 500 | Internal server error |
| 10206 | `CodeChatStreamUnsupported` | 500 | Streaming not supported |

### 媒体错误 (10300-10399)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10300 | `CodeMediaFileTooLarge` | 413 | File too large |
| 10301 | `CodeMediaInvalidFile` | 400 | Invalid file or unsupported type |
| 10302 | `CodeMediaBadURLScheme` | 400 | Invalid media URL scheme |
| 10303 | `CodeMediaUnsupportedType` | 400 | Unsupported media type |
| 10304 | `CodeMediaTooMany` | 400 | Too many media items (max 10) |

### 会话错误 (10400-10499)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10400 | `CodeSessionNotFound` | 404 | Session not found |
| 10401 | `CodeSessionCreateFailed` | 500 | Failed to create session |

### Token 错误 (10500-10599)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10500 | `CodeTokenNotFound` | 404 | Token not found |

### WebSocket 错误 (10600-10699)

| 错误码 | 常量名 | WS 关闭码 | 消息 |
|--------|--------|----------|------|
| 10600 | `CodeWSInvalidToken` | 4001 | Invalid or missing bot token |
| 10601 | `CodeWSTokenDeleted` | 4003 | Token deleted |
| 10602 | `CodeWSServerRestart` | 4000 | Server restarting |
| 10603 | `CodeWSEvicted` | 4005 | Evicted by newer connection |

### Bot 错误 (10700-10799)

| 错误码 | 常量名 | HTTP 状态 | 消息 |
|--------|--------|----------|------|
| 10700 | `CodeBotUnknownError` | 500 | Unknown error from bot |

## 使用示例

### Go 后端

```go
// 返回错误
model.ErrorResponse(c, model.ErrAuthInvalidToken)

// 返回错误（带详情）
model.ErrorResponse(c, model.ErrSessionNotFound, sessionID)
```

### 客户端处理

```javascript
const resp = await fetch('/api/endpoint', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data)
});

const result = await resp.json();

if (result.code === 0) {
  // 成功
  console.log('Success:', result);
} else if (result.code >= 10001 && result.code <= 10099) {
  // 认证错误，跳转登录
  window.location.href = '/login';
} else {
  // 其他错误
  console.error('Error:', result.error);
}
```

## 添加新错误码

1. 在 `backend/internal/model/errors.go` 中添加常量：
   ```go
   const (
       CodeNewError = 10XXX  // 选择合适的分段
   )
   ```

2. 添加错误变量：
   ```go
   var (
       ErrNewError = AppError{CodeNewError, http.StatusXXX, "Error message"}
   )
   ```

3. 更新 `docs/api.md` 和 `docs/error-codes.md` 文档

## 注意事项

- 错误码一旦分配，不应修改，以保持向后兼容
- 新错误码应选择对应模块的空闲号段
- HTTP 状态码和业务错误码分离，便于统一监控和分类
- WebSocket 错误使用 WS 关闭码（4000-4999）
