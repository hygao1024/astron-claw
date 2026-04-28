# Astron Claw Bridge Server API 文档

## 概述

Astron Claw 是一个 AI Bot 实时对话桥接服务。服务器作为中转枢纽，Bot 端通过 WebSocket 连接，Chat 端通过 HTTP SSE 接入，服务器根据 Token 将双方配对并双向转发消息。

```
Chat Client ──HTTP SSE───► Bridge Server ◄──WebSocket── Bot Plugin
             /bridge/chat   (Token 配对)     /bridge/bot
```

- 每个 Token 对应 **1 个 Bot** 连接和 **N 个 Chat** 连接
- Bot 端无需公网 IP，主动向服务器发起出站 WebSocket 连接
- Chat 端通过相同 Token 连接后即可与 Bot 实时对话

### Base URL

```
http://127.0.0.1:8765
```

### 认证方式

| 接口类别 | 认证方式 |
|---------|---------|
| 健康检查 (`/api/health`) | 无需认证 |
| Token 接口 (`/api/token/*`) | 无需认证 |
| Metrics 接口 (`GET /api/metrics`, `GET /metrics`) | 无需认证 |
| Metrics 重置 (`DELETE /api/metrics`) | `Authorization: Bearer <admin_session>` |
| Admin 接口 (`/api/admin/*`) | Cookie `admin_session`（登录后自动携带） |
| 媒体上传 (`POST /api/media/upload`) | `Authorization: Bearer <token>`（仅 Header） |
| HTTP SSE (`/bridge/chat`, `/bridge/chat/sessions`) | `Authorization: Bearer <token>` |
| WebSocket `/bridge/bot` | Query 参数 `token` 或请求头 `X-Astron-Bot-Token` |

---

## 目录

- [1. Token 接口](#1-token-接口)
  - [1.1 创建 Token](#11-创建-token)
  - [1.2 验证 Token](#12-验证-token)
- [2. Admin 认证接口](#2-admin-认证接口)
  - [2.1 查询认证状态](#21-查询认证状态)
  - [2.2 首次设置密码](#22-首次设置密码)
  - [2.3 管理员登录](#23-管理员登录)
  - [2.4 管理员登出](#24-管理员登出)
- [3. Admin Token 管理接口](#3-admin-token-管理接口)
  - [3.1 获取 Token 列表](#31-获取-token-列表)
  - [3.2 创建 Token（管理端）](#32-创建-token管理端)
  - [3.3 更新 Token](#33-更新-token)
  - [3.4 删除 Token](#34-删除-token)
  - [3.5 清理过期 Token](#35-清理过期-token)
- [4. HTTP SSE — Chat 客户端](#4-http-sse--chat-客户端)
  - [4.1 对话（SSE 流式响应）](#41-对话sse-流式响应)
  - [4.2 获取会话列表](#42-获取会话列表)
  - [4.3 创建新会话](#43-创建新会话)
  - [4.4 SSE 事件类型](#44-sse-事件类型)
  - [4.5 交互时序](#45-交互时序)
  - [4.6 接入示例](#46-接入示例)
- [5. WebSocket — Bot 插件](#5-websocket--bot-插件)
  - [5.1 连接](#51-连接)
  - [5.2 接收用户请求](#52-接收用户请求)
  - [5.3 发送流式更新](#53-发送流式更新)
  - [5.4 发送回复完成](#54-发送回复完成)
  - [5.5 接入示例](#55-接入示例)
- [6. Media 接口](#6-media-接口)
  - [6.1 上传媒体文件](#61-上传媒体文件)
- [7. 健康检查接口](#7-健康检查接口)
- [8. Metrics 接口](#8-metrics-接口)
  - [8.1 Metrics 可视化页面](#81-metrics-可视化页面)
  - [8.2 获取 Prometheus 指标](#82-获取-prometheus-指标)
  - [8.3 重置指标数据](#83-重置指标数据)

---

## 1. Token 接口

### 1.1 创建 Token

创建一个 `sk-` 前缀的随机 Token，有效期 24 小时。

```
POST /api/token
```

**请求参数：** 无

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `token` | string | 生成的 Token |

**响应示例：**

```json
{
  "token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/token
```

```python
import requests

resp = requests.post("http://127.0.0.1:8765/api/token")
print(resp.json())
# {'token': 'sk-a1b2c3d4e5f6...'}
```

---

### 1.2 验证 Token

校验 Token 是否有效，并返回对应 Bot 是否在线。

```
POST /api/token/validate
```

**请求体：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `token` | string | 是 | 待验证的 Token |

**请求示例：**

```json
{
  "token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
}
```

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `valid` | boolean | Token 是否有效 |
| `bot_connected` | boolean | 对应的 Bot 是否在线（Token 无效时固定 `false`） |

**响应示例：**

```json
// Token 有效，Bot 在线
{"valid": true, "bot_connected": true}

// Token 有效，Bot 离线
{"valid": true, "bot_connected": false}

// Token 无效
{"valid": false, "bot_connected": false}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/token/validate \
  -H "Content-Type: application/json" \
  -d '{"token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"}'
```

```python
import requests

resp = requests.post("http://127.0.0.1:8765/api/token/validate", json={
    "token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
})
data = resp.json()
print(f"Valid: {data['valid']}, Bot online: {data['bot_connected']}")
```

---

## 2. Admin 认证接口

### 2.1 查询认证状态

返回当前 Admin 的认证状态，前端据此决定显示哪个界面。

```
GET /api/admin/auth/status
```

**请求参数：** 无（自动携带 Cookie）

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `need_setup` | boolean | 是否需要首次设置密码 |
| `authenticated` | boolean | 当前 Session 是否已认证 |

**状态判断逻辑：**

| `need_setup` | `authenticated` | 含义 |
|:---:|:---:|------|
| `true` | `false` | 首次使用，需设置密码 |
| `false` | `true` | 已登录 |
| `false` | `false` | 已设置密码，但未登录 |

**响应示例：**

```json
{"need_setup": false, "authenticated": true}
```

**测试代码：**

```bash
curl http://127.0.0.1:8765/api/admin/auth/status
```

```python
import requests

resp = requests.get("http://127.0.0.1:8765/api/admin/auth/status")
print(resp.json())
# {'need_setup': False, 'authenticated': False}
```

---

### 2.2 首次设置密码

仅在密码未设置时可用。设置成功后自动创建 Session 并通过 `Set-Cookie` 返回。

```
POST /api/admin/auth/setup
```

**请求体：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `password` | string | 是 | 管理员密码（至少 4 个字符） |

**请求示例：**

```json
{"password": "your_password"}
```

**响应：**

| 状态码 | 说明 |
|--------|------|
| `200` | 设置成功，响应头包含 `Set-Cookie: admin_session=xxx` |
| `400` | 密码已设置（`Password already set`）或密码过短（`Password too short`） |

**成功响应：**

```json
{"code": 0}
```

**失败响应：**

```json
{"code": 400, "error": "Password already set"}
{"code": 400, "error": "Password too short"}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/admin/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"password": "your_password"}' \
  -c cookies.txt
```

```python
import requests

session = requests.Session()
resp = session.post("http://127.0.0.1:8765/api/admin/auth/setup", json={
    "password": "your_password"
})
print(resp.json())
# session 对象自动保存 cookie，后续请求自动携带
```

---

### 2.3 管理员登录

验证密码，成功后通过 `Set-Cookie` 返回 Session（有效期 24 小时）。

```
POST /api/admin/auth/login
```

**请求体：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `password` | string | 是 | 管理员密码 |

**请求示例：**

```json
{"password": "your_password"}
```

**响应：**

| 状态码 | 说明 |
|--------|------|
| `200` | 登录成功，响应头包含 `Set-Cookie: admin_session=xxx` |
| `401` | 密码错误 |

**成功响应：**

```json
{"code": 0}
```

**失败响应：**

```json
{"code": 401, "error": "Wrong password"}
```

**测试代码：**

```bash
# 登录并保存 cookie
curl -X POST http://127.0.0.1:8765/api/admin/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your_password"}' \
  -c cookies.txt

# 后续请求携带 cookie
curl http://127.0.0.1:8765/api/admin/tokens -b cookies.txt
```

```python
import requests

session = requests.Session()

# 登录
resp = session.post("http://127.0.0.1:8765/api/admin/auth/login", json={
    "password": "your_password"
})
print(resp.json())  # {'ok': True}

# 后续请求自动携带 cookie
resp = session.get("http://127.0.0.1:8765/api/admin/tokens")
print(resp.json())  # {'tokens': [...]}
```

---

### 2.4 管理员登出

清除服务端 Session 并删除客户端 Cookie。

```
POST /api/admin/auth/logout
```

**请求参数：** 无（自动携带 Cookie）

**响应：**

```json
{"code": 0}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/admin/auth/logout -b cookies.txt
```

```python
resp = session.post("http://127.0.0.1:8765/api/admin/auth/logout")
print(resp.json())  # {'ok': True}
```

---

## 3. Admin Token 管理接口

> 以下接口均需要登录后携带 `admin_session` Cookie，未认证返回 `401`。

### 3.1 获取 Token 列表

返回所有未过期的 Token 及其连接状态，支持分页、搜索、排序和过滤。

```
GET /api/admin/tokens
```

**查询参数：**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page` | integer | `1` | 页码（≥1） |
| `page_size` | integer | `20` | 每页条数（1-100） |
| `search` | string | `""` | 按 Token 值或名称模糊搜索 |
| `sort_by` | string | `"created_at"` | 排序字段：`created_at` \| `bot_online` |
| `sort_order` | string | `"desc"` | 排序方向：`asc` \| `desc` |
| `bot_status` | string | `""` | Bot 状态过滤：`""` 全部 \| `"online"` 仅在线 |

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `tokens` | array | 当前页 Token 列表 |
| `total` | integer | 过滤后的 Token 总数 |
| `page` | integer | 当前页码 |
| `page_size` | integer | 每页条数 |
| `online_bots` | integer | 全局在线 Bot 数量（不受过滤影响） |
| `total_tokens` | integer | 全局 Token 总数（不受过滤影响） |

**Token 对象结构：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `token` | string | Token 值（`sk-` 前缀） |
| `name` | string | Token 名称（未设置时为空字符串） |
| `created_at` | number | 创建时间（Unix 时间戳，秒） |
| `expires_at` | number | 过期时间（Unix 时间戳，秒；永不过期时为 `9999999999`） |
| `bot_online` | boolean | Bot 是否在线 |

**响应示例：**

```json
{
  "tokens": [
    {
      "token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
      "name": "Production Bot",
      "created_at": 1709280000.0,
      "expires_at": 1709366400.0,
      "bot_online": true
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20,
  "online_bots": 1,
  "total_tokens": 5
}
```

**测试代码：**

```bash
# 默认分页
curl http://127.0.0.1:8765/api/admin/tokens -b cookies.txt

# 搜索 + 仅在线 Bot
curl "http://127.0.0.1:8765/api/admin/tokens?search=prod&bot_status=online&page=1&page_size=10" -b cookies.txt
```

```python
resp = session.get("http://127.0.0.1:8765/api/admin/tokens", params={
    "page": 1, "page_size": 10, "sort_by": "bot_online", "sort_order": "desc"
})
data = resp.json()
print(f"Page {data['page']}/{-(-data['total']//data['page_size'])} | Online: {data['online_bots']}/{data['total_tokens']}")
for t in data["tokens"]:
    status = "online" if t["bot_online"] else "offline"
    print(f"  {t['token'][:10]}... | Bot: {status}")
```

---

### 3.2 创建 Token（管理端）

生成新的 `sk-` 前缀 Token，支持自定义名称和过期时间。

```
POST /api/admin/tokens
```

**请求体（JSON，可选）：**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `name` | string | 否 | `""` | Token 名称，用于标识用途 |
| `expires_in` | integer | 否 | `86400` | 过期秒数（`0` 表示永不过期） |

**常用 `expires_in` 值：**

| 值 | 含义 |
|----|------|
| `3600` | 1 小时 |
| `21600` | 6 小时 |
| `86400` | 1 天（默认） |
| `604800` | 7 天 |
| `2592000` | 30 天 |
| `0` | 永不过期 |

**请求示例：**

```json
{"name": "Production Bot", "expires_in": 604800}
```

**响应：**

```json
{"token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/admin/tokens \
  -H "Content-Type: application/json" \
  -d '{"name": "My Bot", "expires_in": 604800}' \
  -b cookies.txt
```

```python
resp = session.post("http://127.0.0.1:8765/api/admin/tokens", json={
    "name": "My Bot",
    "expires_in": 604800  # 7 天
})
print(f"New token: {resp.json()['token']}")
```

---

### 3.3 更新 Token

更新指定 Token 的名称和/或过期时间。

```
PATCH /api/admin/tokens/{token_value}
```

**路径参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `token_value` | string | 要更新的 Token 值 |

**请求体（JSON）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 否 | 新的 Token 名称（传入则更新） |
| `expires_in` | integer | 否 | 从当前时间起的新过期秒数，`0` 表示永不过期（传入则更新） |

**请求示例：**

```json
{"name": "Renamed Bot", "expires_in": 2592000}
```

**响应：**

| 状态码 | 说明 |
|--------|------|
| `200` | 更新成功 |
| `404` | Token 不存在 |

**成功响应：**

```json
{"code": 0}
```

**失败响应：**

```json
{"code": 404, "error": "Token not found"}
```

**测试代码：**

```bash
curl -X PATCH http://127.0.0.1:8765/api/admin/tokens/sk-a1b2c3d4e5f6... \
  -H "Content-Type: application/json" \
  -d '{"name": "New Name", "expires_in": 604800}' \
  -b cookies.txt
```

```python
token = "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
resp = session.patch(f"http://127.0.0.1:8765/api/admin/tokens/{token}", json={
    "name": "New Name",
    "expires_in": 604800  # 续期 7 天
})
print(resp.json())  # {'ok': True}
```

---

### 3.4 删除 Token

立即删除指定 Token，同时清理该 Token 关联的 Redis 会话数据（sessions、active session、bot 注册信息）。

```
DELETE /api/admin/tokens/{token_value}
```

**路径参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `token_value` | string | 要删除的 Token 值 |

**响应：**

```json
{"code": 0}
```

**测试代码：**

```bash
curl -X DELETE http://127.0.0.1:8765/api/admin/tokens/sk-a1b2c3d4e5f6... -b cookies.txt
```

```python
token = "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
resp = session.delete(f"http://127.0.0.1:8765/api/admin/tokens/{token}")
print(resp.json())  # {'ok': True}
```

---

### 3.5 清理过期 Token

批量删除所有已过期的 Token，返回删除数量。

```
POST /api/admin/cleanup
```

**请求参数：** 无

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `removed_tokens` | integer | 已删除的过期 Token 数量 |
| `removed_sessions` | integer | 已清理的过期会话数量（超过 30 天） |

**响应示例：**

```json
{"removed_tokens": 3, "removed_sessions": 5}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/admin/cleanup -b cookies.txt
```

```python
resp = session.post("http://127.0.0.1:8765/api/admin/cleanup")
print(f"Removed {resp.json()['removed_tokens']} tokens, {resp.json()['removed_sessions']} sessions")
```

---

## 4. HTTP SSE — Chat 客户端

Chat 端通过 HTTP SSE（Server-Sent Events）接入。每次对话发起一个 POST 请求，服务端以 SSE 流式返回 Bot 的回复事件。

### 认证方式

所有 SSE 端点统一使用 `Authorization` Header 认证：

```
Authorization: Bearer sk-xxx
```

---

### 4.1 对话（SSE 流式响应）

发送消息给 Bot 并以 SSE 流式接收回复。

```
POST /bridge/chat
```

**请求头：**

| 头部 | 值 | 说明 |
|------|------|------|
| `Content-Type` | `application/json` | 必填 |
| `Authorization` | `Bearer sk-xxx` | 必填 |

**请求体（JSON）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 条件 | 消息文本（无媒体时不能为空） |
| `sessionId` | string | 否 | 会话 ID。不传则自动创建新会话 |
| `media` | array | 否 | 媒体项列表（最多 10 个），每项为 `MediaItem` 对象 |

**MediaItem 对象：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 媒体类型，目前仅支持 `"url"` |
| `content` | string | 是 | 主要值：URL / base64 数据 / S3 key（不能为空） |
| `mimeType` | string | 否 | MIME 类型（仅 `type="base64"` 时需要） |

**请求示例：**

```json
{"content": "你好，请帮我写一段代码"}
```

```json
{"content": "看看这张图", "sessionId": "550e8400-...", "media": [{"type": "url", "content": "http://host:9000/astron-claw-media/sid/photo.jpg"}]}
```

```json
{"content": "对比这两张图", "media": [{"type": "url", "content": "http://host:9000/bucket/a.jpg"}, {"type": "url", "content": "http://host:9000/bucket/b.png"}]}
```

**响应：** `Content-Type: text/event-stream`

成功时返回 SSE 事件流（详见 [4.4 SSE 事件类型](#44-sse-事件类型)）。

**错误响应（JSON）：**

| 状态码 | 说明 |
|--------|------|
| `400` | 空消息、不支持的媒体类型、无效 URL scheme、Bot 未连接 |
| `401` | Token 无效或缺失 |
| `404` | 指定的 sessionId 不存在 |
| `500` | 发送到 Bot 失败 |

```json
{"code": 400, "error": "Empty message"}
{"code": 400, "error": "No bot connected"}
{"code": 401, "error": "Invalid or missing token"}
```

---

### 4.2 获取会话列表

```
GET /bridge/chat/sessions
```

**认证：** `Authorization: Bearer sk-xxx`

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | integer | `0` 表示成功 |
| `sessions` | array | 会话列表 `[{id, number}, ...]` |

**响应示例：**

```json
{
  "code": 0,
  "sessions": [
    {"id": "550e8400-e29b-41d4-a716-446655440000", "number": 1},
    {"id": "660e8400-e29b-41d4-a716-446655440001", "number": 2}
  ]
}
```

---

### 4.3 创建新会话

```
POST /bridge/chat/sessions
```

**请求体：** 无

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | `true` |
| `sessionId` | string | 新建会话 ID |
| `sessionNumber` | integer | 新建会话编号 |
| `sessions` | array | 更新后的所有会话列表 |

**响应示例：**

```json
{
  "code": 0,
  "sessionId": "770e8400-e29b-41d4-a716-446655440002",
  "sessionNumber": 3,
  "sessions": [
    {"id": "550e8400-e29b-41d4-a716-446655440000", "number": 1},
    {"id": "660e8400-e29b-41d4-a716-446655440001", "number": 2},
    {"id": "770e8400-e29b-41d4-a716-446655440002", "number": 3}
  ]
}
```

---

### 4.4 SSE 事件类型

SSE 流中的每个事件格式为：

```
event: <event_type>
data: <json_data>

```

| 事件类型 | 说明 | data 字段 |
|---------|------|----------|
| `session` | 首个事件，包含会话信息 | `sessionId`, `sessionNumber` |
| `chunk` | Bot 回复文本片段（流式） | `content` |
| `thinking` | Bot 思考过程（流式） | `content` |
| `tool_call` | Bot 调用工具 | `name`, `input` |
| `tool_result` | 工具执行结果 | `name`, `status`, `content` |
| `media` | Bot 发送的媒体消息 | `type`, `content`, `caption`（可选） |
| `done` | 本轮回复结束（**终止事件**） | `content` |
| `error` | 错误（**终止事件**） | `content` |
| `: heartbeat` | 心跳注释（15s 间隔），保持连接 | — |

> 收到 `done` 或 `error` 事件后，SSE 流自动关闭。流超时时间为 5 分钟。

**事件示例：**

```
event: session
data: {"sessionId":"550e8400-...","sessionNumber":1}

event: thinking
data: {"content":"让我分析一下..."}

event: chunk
data: {"content":"这是一段"}

event: chunk
data: {"content":"回复文本"}

event: done
data: {"content":"这是一段回复文本"}

```

**媒体事件示例：**

```
event: media
data: {"type":"url","content":"http://host:9000/bucket/result.png"}

event: media
data: {"type":"url","content":"http://host:9000/bucket/doc.md","caption":"这是生成的文件"}

```

---

### 4.5 交互时序

```
Client                          Server                          Bot
  │                               │                              │
  ├── POST /bridge/chat ─────────►│                              │
  │   {content, sessionId?}       │── JSON-RPC request ─────────►│
  │                               │                              │
  │◄── event: session ────────────┤                              │
  │◄── event: thinking ───────────┤◄── session/update ───────────┤
  │◄── event: chunk ──────────────┤◄── session/update ───────────┤
  │◄── event: chunk ──────────────┤◄── session/update ───────────┤
  │◄── event: done ───────────────┤◄── agent_message_final ──────┤
  │    (stream closes)            │◄── JSON-RPC response ────────┤
  │                               │   (request cleanup only)     │
  │                               │                              │
```

---

### 4.6 接入示例

#### JavaScript (fetch + ReadableStream)

```javascript
async function chat(token, message, sessionId) {
  const resp = await fetch('/bridge/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
    body: JSON.stringify({ content: message, sessionId }),
  });

  if (!resp.ok) {
    const err = await resp.json();
    console.error('Error:', err.error);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let replyText = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    let eventType = 'message';
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7);
      } else if (line.startsWith('data: ')) {
        const data = JSON.parse(line.slice(6));

        switch (eventType) {
          case 'session':
            console.log(`Session #${data.sessionNumber}`);
            break;
          case 'chunk':
            replyText += data.content;
            process.stdout.write(data.content);
            break;
          case 'thinking':
            // 可选：展示思考过程
            break;
          case 'done':
            console.log('\n--- Reply complete ---');
            break;
          case 'media':
            console.log(`Media: ${data.content}`);
            break;
          case 'error':
            console.error('Error:', data.content);
            break;
        }
        eventType = 'message';
      }
    }
  }

  return replyText;
}
```

#### Python (requests + SSE 解析)

```python
import json
import requests


def chat_sse(token: str, message: str, session_id: str = None):
    """Send a message and stream the SSE response."""
    resp = requests.post(
        "http://127.0.0.1:8765/bridge/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": message, "sessionId": session_id},
        stream=True,
    )

    if resp.status_code != 200:
        print(f"Error: {resp.json()}")
        return

    reply_text = ""
    event_type = "message"

    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])

            if event_type == "session":
                print(f"Session #{data['sessionNumber']}")
            elif event_type == "chunk":
                reply_text += data["content"]
                print(data["content"], end="", flush=True)
            elif event_type == "done":
                print("\n--- Reply complete ---")
            elif event_type == "media":
                print(f"\nMedia: {data['content']}")
            elif event_type == "error":
                print(f"\nError: {data['content']}")

            event_type = "message"

    return reply_text


# 使用
token = "sk-your-token-here"
chat_sse(token, "你好，介绍一下你自己")
```

#### curl

```bash
# 对话（SSE 流式响应）
curl -N -X POST http://127.0.0.1:8765/bridge/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-xxx" \
  -d '{"content": "你好"}'

# 获取会话列表
curl http://127.0.0.1:8765/bridge/chat/sessions \
  -H "Authorization: Bearer sk-xxx"

# 创建新会话
curl -X POST http://127.0.0.1:8765/bridge/chat/sessions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-xxx"
```

---

## 5. WebSocket — Bot 插件

Bot 端通过此 WebSocket 接收来自 Chat 客户端的请求，处理后返回流式结果。

### 5.1 连接

```
ws://{host}:{port}/bridge/bot?token={token}
```

Token 支持两种传递方式（二选一）：

| 方式 | 示例 |
|------|------|
| Query 参数 | `ws://host:8765/bridge/bot?token=sk-xxx` |
| 请求头 | `X-Astron-Bot-Token: sk-xxx` |

**关闭码：**

| Code | 含义 |
|------|------|
| `1012` | 服务重启（uvicorn 标准） |
| `4000` | 服务重启（graceful shutdown） |
| `4001` | Token 无效或已过期 |
| `4002` | 该 Token 已有另一个 Bot 在线（每个 Token 只允许 1 个 Bot） |
| `4003` | Token 被管理员删除（强制断开） |

---

### 5.2 接收用户请求

服务端将 Chat 消息封装为 JSON-RPC 2.0 格式发送给 Bot：

| 字段 | 类型 | 说明 |
|------|------|------|
| `jsonrpc` | string | 固定 `"2.0"` |
| `id` | string | 请求唯一标识（`req_` 前缀），回复时需原样返回 |
| `method` | string | 固定 `"session/prompt"` |
| `params.sessionId` | string | 会话 ID，不同会话隔离上下文 |
| `params.prompt.content` | array | 消息内容项列表 |

**Content item 类型：**

| type | 字段 | 说明 |
|------|------|------|
| `text` | `content` | 文本内容 |
| `url` | `content` | 媒体下载 URL |

**文本消息示例：**

```json
{
  "jsonrpc": "2.0",
  "id": "req_a1b2c3d4e5f6",
  "method": "session/prompt",
  "params": {
    "sessionId": "550e8400-e29b-41d4-a716-446655440000",
    "prompt": {
      "content": [
        {"type": "text", "content": "你好，请帮我写一段代码"}
      ]
    }
  }
}
```

**媒体消息示例（单文件 + 文本）：**

```json
{
  "jsonrpc": "2.0",
  "id": "req_b2c3d4e5f6a7",
  "method": "session/prompt",
  "params": {
    "sessionId": "550e8400-e29b-41d4-a716-446655440000",
    "prompt": {
      "content": [
        {"type": "text", "content": "看看这张图"},
        {"type": "url", "content": "http://host:9000/astron-claw-media/sid/photo.jpg"}
      ]
    }
  }
}
```

**多文件消息示例：**

```json
{
  "jsonrpc": "2.0",
  "id": "req_c3d4e5f6a7b8",
  "method": "session/prompt",
  "params": {
    "sessionId": "550e8400-e29b-41d4-a716-446655440000",
    "prompt": {
      "content": [
        {"type": "text", "content": "对比这两张图"},
        {"type": "url", "content": "http://host:9000/astron-claw-media/sid/a.jpg"},
        {"type": "url", "content": "http://host:9000/astron-claw-media/sid/b.png"}
      ]
    }
  }
}
```

> **注意：** 所有 content item 统一使用 `{type, content}` 二元结构。Bot 端应根据下载后的实际 MIME 类型判断媒体类别（图片/音频/视频/文件）。

---

### 5.3 发送流式更新

Bot 通过 JSON-RPC Notification（无 `id` 字段）发送流式更新：

**基本结构：**

```json
{
  "method": "session/update",
  "params": {
    "update": {
      "sessionUpdate": "<update_type>",
      "content": {"type": "text", "text": "..."}
    }
  }
}
```

**sessionUpdate 类型：**

| 类型 | 说明 | Chat 端接收为 |
|------|------|-------------|
| `agent_message_chunk` | Bot 回复文本片段（token 级别增量） | `chunk` |
| `agent_message_final` | Bot 回复完成（含最终完整文本） | `done`（含 content） |
| `agent_thought_chunk` | Bot 思考过程片段 | `thinking` |
| `tool_call` | 工具调用（含 title/status/content 字段） | `tool_call` |
| `tool_result` | 工具执行结果（含 title/status/content 字段） | `tool_result` |
| `agent_media` | Bot 发送媒体文件 | `media`（含 `{type, content, caption?}`） |

**回复文本片段示例：**

```json
{
  "method": "session/update",
  "params": {
    "update": {
      "sessionUpdate": "agent_message_chunk",
      "content": {"type": "text", "text": "这是一段回复"}
    }
  }
}
```

**思考过程示例：**

```json
{
  "method": "session/update",
  "params": {
    "update": {
      "sessionUpdate": "agent_thought_chunk",
      "content": {"type": "text", "text": "让我分析一下..."}
    }
  }
}
```

**工具调用示例：**

```json
{
  "method": "session/update",
  "params": {
    "update": {
      "sessionUpdate": "tool_call",
      "title": "read",
      "status": "running",
      "content": "{\"path\":\"src/main.py\"}"
    }
  }
}
```

**工具执行结果示例：**

```json
{
  "method": "session/update",
  "params": {
    "update": {
      "sessionUpdate": "tool_result",
      "title": "read",
      "status": "completed",
      "content": "file contents here..."
    }
  }
}
```

---

### 5.4 发送回复完成

回复结束时发送 JSON-RPC Response（携带 `id` 和 `sessionId`）：

```json
{
  "jsonrpc": "2.0",
  "id": "req_a1b2c3d4e5f6",
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "result": {
    "stopReason": "end_turn"
  }
}
```

> `id` 必须与收到的请求 `id` 一致。`sessionId` 必须与收到的请求 `params.sessionId` 一致，用于跨 Worker 响应路由。
>
> **注意：** JSON-RPC Response 仅用于请求跟踪和错误路由，不会产生 `done` 事件。Chat 端收到的 `done` 事件来自 `agent_message_final` Notification（见 [5.3](#53-发送流式更新)）。Bot 端应在发送完所有流式更新后，先发送 `agent_message_final`，再发送此 JSON-RPC Response。

**错误响应示例：**

```json
{
  "jsonrpc": "2.0",
  "id": "req_a1b2c3d4e5f6",
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "error": {
    "code": -32000,
    "message": "Dispatch not available"
  }
}
```

> 错误响应同样需要携带 `sessionId`，服务端据此将错误事件路由到对应的 Chat 客户端 SSE 流。

---

### 5.5 接入示例

#### Python（最小 Bot 实现）

```python
import asyncio
import json
import websockets


async def bot(token: str):
    uri = f"ws://127.0.0.1:8765/bridge/bot?token={token}"

    async with websockets.connect(uri) as ws:
        print("Bot connected, waiting for messages...")

        async for raw in ws:
            msg = json.loads(raw)

            # 只处理 session/prompt 请求
            if msg.get("method") != "session/prompt":
                continue

            request_id = msg["id"]
            session_id = msg["params"]["sessionId"]
            user_text = msg["params"]["prompt"]["content"][0]["content"]
            print(f"User: {user_text}")

            # 发送思考过程（可选）
            await ws.send(json.dumps({
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "正在思考..."}
                    }
                }
            }))

            # 流式发送回复（分多个 chunk）
            reply = f"你好！你说的是：{user_text}"
            for char in reply:
                await ws.send(json.dumps({
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": char}
                        }
                    }
                }))
                await asyncio.sleep(0.05)  # 模拟流式延迟

            # 发送完成
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "sessionId": session_id,
                "result": {"stopReason": "end_turn"}
            }))
            print(f"Bot: {reply}")


asyncio.run(bot("sk-your-token-here"))
```

---

## 6. Media 接口

媒体文件通过 S3/MinIO 对象存储管理。上传后返回 S3 公网下载 URL，可直接用于在消息中引用。

### 6.1 上传媒体文件

上传文件到 S3 存储，返回文件信息和下载 URL。

```
POST /api/media/upload
```

**请求头：**

| 头部 | 值 | 说明 |
|------|------|------|
| `Authorization` | `Bearer sk-xxx` | Token 认证 |
| `Content-Type` | `multipart/form-data` | 文件上传 |

**请求体（multipart/form-data）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 要上传的文件（最大 500MB） |
| `sessionId` | string | 否 | 会话 ID，用作 S3 key 前缀（不传则自动生成） |

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `fileName` | string | 文件名 |
| `mimeType` | string | MIME 类型 |
| `fileSize` | integer | 文件大小（字节） |
| `sessionId` | string | 会话 ID（传入的或自动生成的） |
| `downloadUrl` | string | S3 公网下载 URL |

**响应示例：**

```json
{
  "fileName": "photo.jpg",
  "mimeType": "image/jpeg",
  "fileSize": 102400,
  "sessionId": "64c3459397604f5590cb50f97e236f43",
  "downloadUrl": "http://host:9000/astron-claw-media/64c3459397604f5590cb50f97e236f43/photo.jpg"
}
```

**错误响应：**

| 状态码 | 说明 |
|--------|------|
| `401` | Token 无效或缺失 |
| `400` | 无效文件或不支持的类型 |
| `413` | 文件超过大小限制 |

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/media/upload \
  -H "Authorization: Bearer sk-your-token" \
  -F "file=@photo.jpg" \
  -F "sessionId=my-session-id"
```

```python
import requests

with open("photo.jpg", "rb") as f:
    resp = requests.post(
        "http://127.0.0.1:8765/api/media/upload",
        headers={"Authorization": "Bearer sk-your-token"},
        files={"file": ("photo.jpg", f, "image/jpeg")},
        data={"sessionId": "my-session-id"},
    )
print(resp.json())
# {'fileName': 'photo.jpg', 'downloadUrl': 'http://host:9000/...', ...}
```

---

## 7. 健康检查接口

检查服务端 MySQL 和 Redis 的连通性，无需认证。

```
GET /api/health
```

**请求参数：** 无

**响应示例（全部健康）：**

```json
{
  "code": 0,
  "status": "ok",
  "mysql": true,
  "redis": true
}
```

**响应示例（部分不可用）：**

```json
{
  "code": 0,
  "status": "degraded",
  "mysql": true,
  "redis": false
}
```

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `"ok"` 表示全部正常，`"degraded"` 表示部分服务不可用 |
| `mysql` | boolean | MySQL 连通性 |
| `redis` | boolean | Redis 连通性 |

> 注：该接口始终返回 HTTP 200，通过 `status` 字段区分健康状态。Dockerfile 中的 `HEALTHCHECK` 即使用此端点。

**测试代码：**

```bash
curl http://127.0.0.1:8765/api/health
```

```python
import requests

resp = requests.get("http://127.0.0.1:8765/api/health")
data = resp.json()
print(data["status"])  # "ok" or "degraded"
```

---

## 错误码汇总

### 统一响应格式

所有 HTTP 响应统一包含 `code` 字段，客户端通过 `code === 0` 判断成功：

**成功响应：**

```json
{"code": 0, "token": "sk-abc123..."}
```

**错误响应：**

```json
{"code": 10100, "error": "Password already set"}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | integer | `0` 表示成功，`10000+` 表示业务错误码 |
| `error` | string | 仅失败时存在，人类可读的错误描述（可含动态详情，如 `"Session not found: <id>"`) |

### 业务错误码清单

错误码从 10000 开始，按功能模块分段：

#### 认证错误 (10001-10099)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10001` | 401 | Invalid or missing token | Token 无效或缺失 |
| `10002` | 401 | Missing authorization | 缺少 Authorization Header |
| `10003` | 401 | Invalid admin session | Admin Session 无效或过期 |
| `10004` | 401 | Unauthorized | Admin 未认证 |
| `10005` | 401 | Wrong password | 管理员登录密码错误 |

#### 管理员设置错误 (10100-10199)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10100` | 400 | Password already set | 重复设置密码 |
| `10101` | 400 | Password too short | 密码少于 8 个字符 |

#### Chat/SSE 错误 (10200-10299)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10200` | 400 | Empty message | 消息内容和媒体均为空 |
| `10201` | 400 | No bot connected | Token 对应的 Bot 未在线 |
| `10202` | 400 | Invalid request | 请求格式错误 |
| `10203` | 500 | Failed to send message to bot | 消息推送到 Bot 失败 |
| `10204` | 500 | Stream timeout | SSE 流超时（10 分钟） |
| `10205` | 500 | Internal server error | 内部服务器错误 |
| `10206` | 500 | Streaming not supported | 流式响应不支持 |

#### 媒体错误 (10300-10399)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10300` | 413 | File too large | 文件超过大小限制（500MB） |
| `10301` | 400 | Invalid file or unsupported type | 无效文件或不支持的类型 |
| `10302` | 400 | Invalid media URL scheme | 媒体 URL 非 http/https |
| `10303` | 400 | Unsupported media type | 不支持的媒体类型 |
| `10304` | 400 | Too many media items (max 10) | 媒体项超过 10 个 |

#### 会话错误 (10400-10499)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10400` | 404 | Session not found | 指定的会话不存在 |
| `10401` | 500 | Failed to create session | 会话创建失败 |

#### Token 错误 (10500-10599)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10500` | 404 | Token not found | 指定的 Token 不存在 |

#### WebSocket 错误 (10600-10699)

| code | WS 关闭码 | 消息 | 使用场景 |
|------|----------|------|---------|
| `10600` | 4001 | Invalid or missing bot token | Bot Token 无效或缺失 |
| `10601` | 4003 | Token deleted | Token 被管理员删除 |
| `10602` | 4000 | Server restarting | 服务器重启中 |
| `10603` | 4005 | Evicted by newer connection | 被新连接踢出 |

#### Bot 错误 (10700-10799)

| code | HTTP 状态码 | 消息 | 使用场景 |
|------|-----------|------|---------|
| `10700` | 500 | Unknown error from bot | Bot 返回未知错误 |

### HTTP 状态码

| 状态码 | 说明 |
|--------|------|
| `200` | 请求成功 |
| `400` | 请求参数错误 |
| `401` | 未认证或密码错误 |
| `404` | 路径不存在或资源未找到 |
| `413` | 文件超过大小限制 |

### WebSocket 关闭码

| 关闭码 | 说明 |
|--------|------|
| `1012` | 服务重启（uvicorn 标准关闭码，等同 `4000`） |
| `4000` | 服务重启（自定义关闭码，graceful shutdown 时发送） |
| `4001` | Token 无效或已过期 |
| `4002` | 该 Token 已有 Bot 在线（仅 `/bridge/bot`） |
| `4003` | Token 被管理员删除（仅 `/bridge/bot`） |

**客户端重连建议：**

| 关闭码 | 推荐行为 |
|--------|---------|
| `4000` / `1012` | 重置重试计数器，立即快速重连 |
| `4001` | 停止重试，返回登录/Token 输入页 |
| `4002` | 停止重试，提示用户已有 Bot 在线 |
| `4003` | 停止重试，提示 Token 已被删除 |
| 其他 | 指数退避重连 |

---

## 8. Metrics 接口

OTLP 指标通过 Redis 聚合后，以 Prometheus exposition format 对外暴露。需启用环境变量 `OTLP_ENABLED=true`。

### 8.1 Metrics 可视化页面

浏览器访问，可视化展示所有指标的实时仪表盘。

```
GET /metrics
```

**响应：** HTML 页面（自动每 10 秒刷新数据）

**包含内容：**
- 汇总卡片：Total Requests、Active Streams、Avg Request Duration、Avg Stream Duration
- Counter 指标表格（按 label 分行展示值）
- Histogram 指标柱状图（bucket 分布 + count/sum/avg）
- Gauge 指标表格
- Raw 模式切换（查看原始 Prometheus 文本）

**访问方式：**

```
http://127.0.0.1:8765/metrics
```

---

### 8.2 获取 Prometheus 指标

返回 Prometheus exposition format 文本，供 Prometheus Server 抓取或程序解析。

```
GET /api/metrics
```

**请求参数：** 无需认证

**响应头：**

| 头部 | 值 |
|------|------|
| `Content-Type` | `text/plain; version=0.0.4; charset=utf-8` |

**响应示例：**

```
# HELP bridge_chat_requests_total /bridge/chat 请求总数
# TYPE bridge_chat_requests_total counter
bridge_chat_requests_total{code="200",service="astron-claw",status="success",token_prefix="sk-a1b2c3..."} 42

# HELP bridge_chat_request_duration_seconds /bridge/chat 首字节耗时
# TYPE bridge_chat_request_duration_seconds histogram
bridge_chat_request_duration_seconds_bucket{code="200",le="0.005",service="astron-claw",status="success",token_prefix="sk-a1b2c3..."} 5
bridge_chat_request_duration_seconds_bucket{code="200",le="0.01",service="astron-claw",status="success",token_prefix="sk-a1b2c3..."} 18
bridge_chat_request_duration_seconds_bucket{code="200",le="+Inf",service="astron-claw",status="success",token_prefix="sk-a1b2c3..."} 42
bridge_chat_request_duration_seconds_sum{code="200",service="astron-claw",status="success",token_prefix="sk-a1b2c3..."} 1.234
bridge_chat_request_duration_seconds_count{code="200",service="astron-claw",status="success",token_prefix="sk-a1b2c3..."} 42

# HELP bridge_chat_active_streams 当前活跃 SSE 流数量
# TYPE bridge_chat_active_streams gauge
bridge_chat_active_streams{service="astron-claw",token_prefix="sk-a1b2c3..."} 3
```

**指标清单：**

| 指标名 | 类型 | 说明 | Labels |
|--------|------|------|--------|
| `bridge.chat.requests` | Counter | `/bridge/chat` 请求总数 | `status`, `code`, `token_prefix` |
| `bridge.chat.request.duration` | Histogram | 首字节耗时（秒） | `status`, `code`, `token_prefix` |
| `bridge.chat.stream.duration` | Histogram | SSE 流持续时长（秒） | `close_reason`, `token_prefix` |
| `bridge.chat.active_streams` | UpDownCounter (Gauge) | 当前活跃 SSE 流数量 | `token_prefix` |

**status / code label 取值：**

| status | code | 说明 |
|--------|------|------|
| `success` | `200` | 请求成功，进入 SSE 流 |
| `auth_fail` | `401` | Token 无效或缺失 |
| `bad_request` | `400` | 参数错误（空消息、无效媒体等） |
| `no_bot` | `400` | Bot 未连接 |
| `session_not_found` | `404` | 指定会话不存在 |
| `send_fail` | `500` | 发送到 Bot 失败 |

**close_reason label 取值：**

| 值 | 说明 |
|----|------|
| `done` | 正常结束 |
| `error` | 流式错误 |
| `timeout` | 5 分钟超时 |
| `client_disconnect` | 客户端断开 |

**Prometheus 接入配置：**

```yaml
# prometheus.yml
scrape_configs:
  - job_name: "astron-claw"
    scrape_interval: 15s
    metrics_path: /api/metrics
    static_configs:
      - targets: ["127.0.0.1:8765"]
```

**PromQL 查询示例：**

> 以下查询中 Prometheus 指标名使用下划线（`bridge_chat_*`），即 OTel 名称中 `.` 自动转换为 `_`。

#### 请求速率与成功率

```promql
# QPS — 每秒请求数（5 分钟窗口平滑）
rate(bridge_chat_requests_total[5m])

# 成功 QPS
rate(bridge_chat_requests_total{code="200"}[5m])

# 成功率（%）
sum(rate(bridge_chat_requests_total{code="200"}[5m]))
/
sum(rate(bridge_chat_requests_total[5m]))
* 100

# 错误 QPS — 按 HTTP 状态码分组
sum by (code) (
  rate(bridge_chat_requests_total{code!="200"}[5m])
)

# 错误 QPS — 按业务错误类型分组
sum by (status) (
  rate(bridge_chat_requests_total{code!="200"}[5m])
)

# 4xx / 5xx 分组
sum by (code) (
  rate(bridge_chat_requests_total{code=~"4.."}[5m])
)
sum by (code) (
  rate(bridge_chat_requests_total{code=~"5.."}[5m])
)

# 某个 Token 的请求速率
rate(bridge_chat_requests_total{token_prefix="sk-a1b2c3..."}[5m])
```

#### 请求耗时（首字节延迟）

```promql
# P50 延迟（中位数）
histogram_quantile(0.5,
  sum by (le) (rate(bridge_chat_request_duration_seconds_bucket[5m]))
)

# P95 延迟
histogram_quantile(0.95,
  sum by (le) (rate(bridge_chat_request_duration_seconds_bucket[5m]))
)

# P99 延迟
histogram_quantile(0.99,
  sum by (le) (rate(bridge_chat_request_duration_seconds_bucket[5m]))
)

# 按 status 分组的 P95 延迟
histogram_quantile(0.95,
  sum by (le, status) (rate(bridge_chat_request_duration_seconds_bucket[5m]))
)

# 平均请求耗时
sum(rate(bridge_chat_request_duration_seconds_sum[5m]))
/
sum(rate(bridge_chat_request_duration_seconds_count[5m]))
```

#### SSE 流时长

```promql
# 流式连接平均持续时长
sum(rate(bridge_chat_stream_duration_seconds_sum[5m]))
/
sum(rate(bridge_chat_stream_duration_seconds_count[5m]))

# P95 流式时长
histogram_quantile(0.95,
  sum by (le) (rate(bridge_chat_stream_duration_seconds_bucket[5m]))
)

# 按关闭原因分组的流式连接速率
sum by (close_reason) (
  rate(bridge_chat_stream_duration_seconds_count[5m])
)

# 超时 / 客户端断连比例
sum(rate(bridge_chat_stream_duration_seconds_count{close_reason=~"timeout|client_disconnect"}[5m]))
/
sum(rate(bridge_chat_stream_duration_seconds_count[5m]))
```

#### 活跃连接

```promql
# 当前活跃 SSE 流总数
sum(bridge_chat_active_streams)

# 按 Token 分组的活跃流数
sum by (token_prefix) (bridge_chat_active_streams)
```

#### Grafana 告警规则参考

```yaml
# 成功率低于 95% 持续 5 分钟
- alert: AstronClawHighErrorRate
  expr: |
    sum(rate(bridge_chat_requests_total{code="200"}[5m]))
    / sum(rate(bridge_chat_requests_total[5m]))
    < 0.95
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Chat 请求成功率低于 95%"

# P95 延迟超过 2 秒
- alert: AstronClawHighLatency
  expr: |
    histogram_quantile(0.95,
      sum by (le) (rate(bridge_chat_request_duration_seconds_bucket[5m]))
    ) > 2
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Chat 请求 P95 延迟超过 2s"

# 活跃流数量异常高
- alert: AstronClawTooManyStreams
  expr: sum(bridge_chat_active_streams) > 100
  for: 3m
  labels:
    severity: critical
  annotations:
    summary: "活跃 SSE 流数量超过 100"
```

**测试代码：**

```bash
# 获取 Prometheus 格式指标
curl http://127.0.0.1:8765/api/metrics
```

```python
import requests

resp = requests.get("http://127.0.0.1:8765/api/metrics")
print(resp.text)
```

---

### 8.3 重置指标数据

删除 Redis 中所有 OTLP 指标数据。需要 Admin 认证。

```
DELETE /api/metrics
```

**请求头：**

| 头部 | 值 | 说明 |
|------|------|------|
| `Authorization` | `Bearer <admin_session>` | Admin Session Token |

**响应：**

| 状态码 | 说明 |
|--------|------|
| `200` | 重置成功 |
| `401` | 未认证或 Session 无效 |

**成功响应：**

```json
{"code": 0, "message": "All metrics reset"}
```

**测试代码：**

```bash
# 需先通过 /api/admin/auth/login 获取 admin_session
curl -X DELETE http://127.0.0.1:8765/api/metrics \
  -H "Authorization: Bearer <admin_session_token>"
```

```python
# admin_session 需从登录接口获取
resp = requests.delete("http://127.0.0.1:8765/api/metrics", headers={
    "Authorization": f"Bearer {admin_session}"
})
print(resp.json())  # {'ok': True, 'message': 'All metrics reset'}
```
