# Astron Claw Bridge Server API 文档

## 概述

Astron Claw 是一个 AI Bot 实时对话桥接服务。服务器作为中转枢纽，Bot 端和 Chat 端分别通过 WebSocket 连接到服务器，服务器根据 Token 将双方配对并双向转发消息。

```
Chat Client ──WebSocket──► Bridge Server ◄──WebSocket── Bot Plugin
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
| Admin 接口 (`/api/admin/*`) | Cookie `admin_session`（登录后自动携带） |
| 媒体上传 (`POST /api/media/upload`) | `Authorization: Bearer <token>`（仅 Header） |
| 媒体下载 (`GET /api/media/download/*`) | `Authorization: Bearer <token>` 或 Query 参数 `token` |
| WebSocket `/bridge/bot` | Query 参数 `token` 或请求头 `X-Astron-Bot-Token` |
| WebSocket `/bridge/chat` | Query 参数 `token` |

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
- [4. WebSocket — Chat 客户端](#4-websocket--chat-客户端)
  - [4.1 连接](#41-连接)
  - [4.2 客户端发送消息](#42-客户端发送消息)
  - [4.3 服务端推送消息](#43-服务端推送消息)
  - [4.4 会话管理](#44-会话管理)
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
  - [6.2 下载媒体文件](#62-下载媒体文件)
- [7. 健康检查接口](#7-健康检查接口)

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
{"ok": true}
```

**失败响应：**

```json
{"error": "Password already set"}
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
{"ok": true}
```

**失败响应：**

```json
{"error": "Wrong password"}
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
{"ok": true}
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

返回所有未过期的 Token 及其连接状态。

```
GET /api/admin/tokens
```

**请求参数：** 无

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `tokens` | array | Token 列表 |

**Token 对象结构：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `token` | string | Token 值（`sk-` 前缀） |
| `name` | string | Token 名称（未设置时为空字符串） |
| `created_at` | number | 创建时间（Unix 时间戳，秒） |
| `expires_at` | number | 过期时间（Unix 时间戳，秒；永不过期时为 `9999999999`） |
| `bot_online` | boolean | Bot 是否在线 |
| `chat_count` | integer | 当前 Chat 连接数 |

**响应示例：**

```json
{
  "tokens": [
    {
      "token": "sk-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
      "name": "Production Bot",
      "created_at": 1709280000.0,
      "expires_at": 1709366400.0,
      "bot_online": true,
      "chat_count": 2
    }
  ]
}
```

**测试代码：**

```bash
curl http://127.0.0.1:8765/api/admin/tokens -b cookies.txt
```

```python
resp = session.get("http://127.0.0.1:8765/api/admin/tokens")
for t in resp.json()["tokens"]:
    status = "online" if t["bot_online"] else "offline"
    print(f"{t['token'][:10]}... | Bot: {status} | Chats: {t['chat_count']}")
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
{"ok": true}
```

**失败响应：**

```json
{"error": "Token not found"}
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
{"ok": true}
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
| `removed_media` | integer | 已删除的过期媒体文件数量 |

**响应示例：**

```json
{"removed_tokens": 3, "removed_media": 5}
```

**测试代码：**

```bash
curl -X POST http://127.0.0.1:8765/api/admin/cleanup -b cookies.txt
```

```python
resp = session.post("http://127.0.0.1:8765/api/admin/cleanup")
print(f"Removed {resp.json()['removed_tokens']} tokens, {resp.json()['removed_media']} media files")
```

---

## 4. WebSocket — Chat 客户端

Chat 端通过 WebSocket 与 Bot 进行实时对话。服务器根据 Token 将 Chat 与 Bot 配对并双向转发消息。

### 4.1 连接

```
ws://{host}:{port}/bridge/chat?token={token}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `token` | string | 是 | 由 Admin 生成的 `sk-` 前缀 Token |

**连接结果：**

| 场景 | 行为 |
|------|------|
| Token 有效 | 连接保持，服务端依次推送 `bot_status` 和 `session_info` 消息 |
| Token 无效/过期 | 服务端关闭连接，close code `4001`，reason `"Invalid or missing token"` |
| 重连（已有会话） | 自动恢复 Redis 中的活跃会话，而非创建新会话 |
| 服务重启 | 服务端发送 close code `4000` 或 `1012`，客户端应快速重连 |

---

### 4.2 客户端发送消息

客户端通过 WebSocket 发送 JSON 文本帧。

#### 文本消息

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 固定为 `"message"` |
| `msgType` | string | 否 | 消息类型，默认 `"text"` |
| `content` | string | 是 | 用户消息文本，不能为空 |

**示例：**

```json
{"type": "message", "content": "你好，请帮我写一段代码"}
```

#### 媒体消息

发送图片、文件等媒体消息前，需先通过 [Media 上传接口](#61-上传媒体文件) 上传文件获取 `mediaId`。

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 固定为 `"message"` |
| `msgType` | string | 是 | 媒体类型：`"image"` / `"file"` / `"audio"` / `"video"` |
| `content` | string | 否 | 附带的文本描述 |
| `media` | object | 是 | 媒体信息 |
| `media.mediaId` | string | 是 | 上传后获得的媒体 ID |
| `media.fileName` | string | 否 | 文件名 |
| `media.mimeType` | string | 否 | MIME 类型 |
| `media.fileSize` | integer | 否 | 文件大小（字节） |

**示例：**

```json
{
  "type": "message",
  "msgType": "image",
  "content": "",
  "media": {
    "mediaId": "abc123",
    "fileName": "photo.jpg",
    "mimeType": "image/jpeg",
    "fileSize": 102400
  }
}
```

---

### 4.3 服务端推送消息

服务端通过 WebSocket 向客户端推送以下类型的 JSON 消息：

#### `bot_status` — Bot 在线状态

连接成功后立即推送一次，Bot 上下线时也会推送。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"bot_status"` |
| `connected` | boolean | Bot 是否在线 |

```json
{"type": "bot_status", "connected": true}
```

#### `chunk` — Bot 回复文本片段（流式）

Bot 的回复内容分多个 chunk 推送，客户端需拼接显示。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"chunk"` |
| `content` | string | 文本片段 |

```json
{"type": "chunk", "content": "这是一段回复"}
```

#### `thinking` — Bot 思考过程（流式）

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"thinking"` |
| `content` | string | 思考内容片段 |

```json
{"type": "thinking", "content": "让我分析一下这个问题..."}
```

#### `tool_call` — Bot 调用工具

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"tool_call"` |
| `name` | string | 工具名称 |
| `input` | string | 工具输入参数（JSON 字符串） |

```json
{"type": "tool_call", "name": "read", "input": "{\"path\":\"src/main.py\"}"}
```

#### `tool_result` — 工具执行结果

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"tool_result"` |
| `name` | string | 工具名称 |
| `status` | string | 执行状态：`"completed"` 或 `"error"` |
| `content` | string | 工具执行结果文本 |

```json
{"type": "tool_result", "name": "read", "status": "completed", "content": "file contents here..."}
```

#### `done` — 本轮回复结束

收到此消息表示 Bot 对当前提问的回复已完成。`done` 事件有两种来源：

1. Bot 发送 `session/update` Notification（`sessionUpdate: "agent_message_final"`）— 此时 `content` 携带最终完整文本
2. Bot 发送 JSON-RPC Response（`result.stopReason: "end_turn"`）— 此时无 `content` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"done"` |
| `content` | string | 最终完整回复文本（可选，可为空） |

```json
{"type": "done", "content": "完整的回复文本"}
```

#### `error` — 错误

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"error"` |
| `content` | string | 错误描述 |

```json
{"type": "error", "content": "No bot connected"}
```

**可能的错误值：**

| content | 说明 |
|---------|------|
| `Empty message` | 发送了空文本消息 |
| `Missing media info` | 媒体消息缺少 media 对象 |
| `No bot connected` | 当前 Token 没有 Bot 在线 |
| `Failed to send to bot` | 发送到 Bot 失败 |

#### `message` — Bot 发送的媒体消息

Bot 发送的带媒体附件的消息（通过 `agent_media` update type 触发）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"message"` |
| `msgType` | string | 媒体类型：`"image"` / `"file"` / `"audio"` / `"video"` |
| `content` | string | 附带文本（可为空） |
| `media` | object | 媒体信息 |
| `media.mediaId` | string | 媒体文件 ID |
| `media.fileName` | string | 文件名 |
| `media.mimeType` | string | MIME 类型 |
| `media.fileSize` | integer | 文件大小（字节） |
| `media.downloadUrl` | string | 下载路径 |

```json
{
  "type": "message",
  "msgType": "image",
  "content": "",
  "media": {
    "mediaId": "abc123",
    "fileName": "output.png",
    "mimeType": "image/png",
    "fileSize": 204800,
    "downloadUrl": "/api/media/download/abc123"
  }
}
```

#### `session_info` — 会话信息（连接后推送）

连接成功后紧随 `bot_status` 推送，包含初始会话和已有会话列表。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"session_info"` |
| `sessionId` | string | 当前活跃会话 ID |
| `sessionNumber` | integer | 当前会话编号 |
| `sessions` | array | 所有会话列表 `[{id, number}, ...]` |
| `activeSessionId` | string | 活跃会话 ID |

```json
{
  "type": "session_info",
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "sessionNumber": 1,
  "sessions": [{"id": "550e8400-e29b-41d4-a716-446655440000", "number": 1}],
  "activeSessionId": "550e8400-e29b-41d4-a716-446655440000"
}
```

#### `new_session_ack` — 新建会话确认

客户端发送 `new_session` 后收到的确认消息。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"new_session_ack"` |
| `sessionId` | string | 新建会话 ID |
| `sessionNumber` | integer | 新会话编号 |
| `sessions` | array | 更新后的所有会话列表 |
| `activeSessionId` | string | 活跃会话 ID（即新建的会话） |

```json
{
  "type": "new_session_ack",
  "sessionId": "660e8400-e29b-41d4-a716-446655440001",
  "sessionNumber": 2,
  "sessions": [
    {"id": "550e8400-e29b-41d4-a716-446655440000", "number": 1},
    {"id": "660e8400-e29b-41d4-a716-446655440001", "number": 2}
  ],
  "activeSessionId": "660e8400-e29b-41d4-a716-446655440001"
}
```

#### `switch_session_ack` — 切换会话确认

客户端发送 `switch_session` 后收到的确认消息。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"switch_session_ack"` |
| `sessionId` | string | 切换到的会话 ID |
| `sessions` | array | 所有会话列表 |
| `activeSessionId` | string | 活跃会话 ID |

```json
{
  "type": "switch_session_ack",
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "sessions": [
    {"id": "550e8400-e29b-41d4-a716-446655440000", "number": 1},
    {"id": "660e8400-e29b-41d4-a716-446655440001", "number": 2}
  ],
  "activeSessionId": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

### 4.4 会话管理

Chat 客户端支持多会话管理。每个 Token 连接后会自动创建第一个会话，之后可以创建新会话或在已有会话之间切换。不同会话的消息通过不同的 `sessionId` 路由到 Bot，Bot 端会自动隔离不同会话的上下文。

#### 新建会话

```json
{"type": "new_session"}
```

服务端响应 `new_session_ack`，客户端应清空消息列表并保存当前会话的消息快照。

#### 切换会话

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 固定为 `"switch_session"` |
| `sessionId` | string | 是 | 要切换到的会话 ID |

```json
{"type": "switch_session", "sessionId": "550e8400-e29b-41d4-a716-446655440000"}
```

服务端响应 `switch_session_ack`（成功）或 `error`（会话不存在）。

---

### 4.5 交互时序

```
Client                          Server                          Bot
  │                               │                              │
  ├── WS connect ────────────────►│                              │
  │◄── bot_status ────────────────┤                              │
  │◄── session_info ──────────────┤                              │
  │                               │                              │
  ├── {"type":"message"} ────────►│── JSON-RPC request ─────────►│
  │                               │                              │
  │◄── {"type":"thinking"} ───────┤◄── session/update ───────────┤
  │◄── {"type":"tool_call"} ──────┤◄── session/update ───────────┤
  │◄── {"type":"chunk"} ──────────┤◄── session/update ───────────┤
  │◄── {"type":"chunk"} ──────────┤◄── session/update ───────────┤
  │◄── {"type":"done"} ───────────┤◄── JSON-RPC response ────────┤
  │                               │                              │
  ├── {"type":"new_session"} ────►│                              │
  │◄── new_session_ack ───────────┤                              │
  │                               │                              │
  ├── {"type":"switch_session"} ─►│                              │
  │◄── switch_session_ack ────────┤                              │
  │                               │                              │
```

---

### 4.6 接入示例

#### JavaScript

```javascript
const ws = new WebSocket('ws://127.0.0.1:8765/bridge/chat?token=sk-xxx');

let botOnline = false;
let assistantText = '';
let thinkingText = '';

ws.onopen = () => console.log('Connected');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case 'bot_status':
      botOnline = msg.connected;
      console.log('Bot online:', botOnline);
      break;

    case 'thinking':
      thinkingText += msg.content;
      // 可选：展示思考过程
      break;

    case 'chunk':
      assistantText += msg.content;
      process.stdout.write(msg.content);  // 流式输出
      break;

    case 'tool_call':
      console.log(`\n[Tool: ${msg.name}] ${msg.input}`);
      break;

    case 'tool_result':
      console.log(`\n[Tool Result] ${msg.content}`);
      break;

    case 'done':
      console.log('\n--- Reply complete ---');
      console.log('Full reply:', assistantText);
      assistantText = '';
      thinkingText = '';
      break;

    case 'error':
      console.error('Error:', msg.content);
      break;
  }
};

ws.onclose = (e) => console.log('Disconnected:', e.code, e.reason);

// 发送消息（需等 ws.onopen 触发后）
function send(text) {
  ws.send(JSON.stringify({ type: 'message', content: text }));
}

// 示例：连接成功后发送
ws.onopen = () => {
  console.log('Connected, waiting for bot_status...');
};
```

#### Python

```python
import asyncio
import json
import websockets


async def chat(token: str, message: str):
    uri = f"ws://127.0.0.1:8765/bridge/chat?token={token}"

    async with websockets.connect(uri) as ws:
        # 1. 等待 bot_status
        raw = await ws.recv()
        status = json.loads(raw)
        print(f"Bot online: {status['connected']}")

        if not status["connected"]:
            print("Bot is offline, cannot send message")
            return

        # 2. 等待 session_info
        raw = await ws.recv()
        session = json.loads(raw)
        print(f"Session: {session['sessionId'][:8]}... (#{session['sessionNumber']})")

        # 3. 发送消息
        await ws.send(json.dumps({"type": "message", "content": message}))
        print(f"Sent: {message}")

        # 4. 接收流式回复
        reply_text = ""
        thinking_text = ""

        async for raw in ws:
            msg = json.loads(raw)
            msg_type = msg["type"]

            if msg_type == "thinking":
                thinking_text += msg["content"]

            elif msg_type == "chunk":
                reply_text += msg["content"]
                print(msg["content"], end="", flush=True)

            elif msg_type == "tool_call":
                print(f"\n[Tool: {msg['name']}] {msg['input']}")

            elif msg_type == "tool_result":
                print(f"\n[Tool Result] {msg['content']}")

            elif msg_type == "done":
                print("\n--- Reply complete ---")
                break

            elif msg_type == "error":
                print(f"\nError: {msg['content']}")
                break

            elif msg_type == "bot_status":
                if not msg["connected"]:
                    print("\nBot went offline")
                    break

        return reply_text


# 运行
token = "sk-your-token-here"
asyncio.run(chat(token, "你好，介绍一下你自己"))
```

#### Python（多轮对话）

```python
import asyncio
import json
import websockets


async def multi_turn_chat(token: str):
    uri = f"ws://127.0.0.1:8765/bridge/chat?token={token}"

    async with websockets.connect(uri) as ws:
        # 等待 bot_status
        status = json.loads(await ws.recv())
        print(f"Bot online: {status['connected']}")

        # 等待 session_info
        session = json.loads(await ws.recv())
        print(f"Session #{session['sessionNumber']}\n")

        while True:
            # 用户输入
            user_input = input("You: ").strip()
            if not user_input or user_input.lower() in ("exit", "quit"):
                break

            # 发送
            await ws.send(json.dumps({
                "type": "message",
                "content": user_input
            }))

            # 接收回复
            print("Bot: ", end="", flush=True)
            async for raw in ws:
                msg = json.loads(raw)

                if msg["type"] == "chunk":
                    print(msg["content"], end="", flush=True)
                elif msg["type"] == "done":
                    print("\n")
                    break
                elif msg["type"] == "error":
                    print(f"\n[Error] {msg['content']}\n")
                    break
                elif msg["type"] == "bot_status" and not msg["connected"]:
                    print("\n[Bot disconnected]")
                    return


asyncio.run(multi_turn_chat("sk-your-token-here"))
```

#### curl + websocat

```bash
# 安装 websocat: https://github.com/nickel-org/websocat
# 连接并交互
echo '{"type":"message","content":"你好"}' | \
  websocat 'ws://127.0.0.1:8765/bridge/chat?token=sk-xxx'
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
| `text` | `text` | 文本内容 |
| `media` | `msgType`, `media` | 媒体内容（图片/文件等） |

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
        {"type": "text", "text": "你好，请帮我写一段代码"}
      ]
    }
  }
}
```

**媒体消息示例：**

```json
{
  "jsonrpc": "2.0",
  "id": "req_b2c3d4e5f6a7",
  "method": "session/prompt",
  "params": {
    "sessionId": "550e8400-e29b-41d4-a716-446655440000",
    "prompt": {
      "content": [
        {"type": "text", "text": "[image]"},
        {
          "type": "media",
          "msgType": "image",
          "media": {
            "mediaId": "abc123",
            "fileName": "photo.jpg",
            "mimeType": "image/jpeg",
            "fileSize": 102400,
            "downloadUrl": "/api/media/download/abc123"
          }
        }
      ]
    }
  }
}
```

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
| `agent_media` | Bot 发送媒体文件 | `message`（含 media 对象） |

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

回复结束时发送 JSON-RPC Response（携带 `id`）：

```json
{
  "jsonrpc": "2.0",
  "id": "req_a1b2c3d4e5f6",
  "result": {
    "stopReason": "end_turn"
  }
}
```

> `id` 必须与收到的请求 `id` 一致。

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
            user_text = msg["params"]["prompt"]["content"][0]["text"]
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
                "result": {"stopReason": "end_turn"}
            }))
            print(f"Bot: {reply}")


asyncio.run(bot("sk-your-token-here"))
```

---

## 6. Media 接口

媒体文件上传和下载接口。所有媒体接口需通过 `Authorization: Bearer <token>` 或 Query 参数 `token` 进行认证。

### 6.1 上传媒体文件

上传文件并获取 `mediaId`，用于在 WebSocket 消息中引用。

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
| `file` | file | 是 | 要上传的文件（最大 50MB） |

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `mediaId` | string | 媒体文件唯一标识 |
| `fileName` | string | 文件名 |
| `mimeType` | string | MIME 类型 |
| `fileSize` | integer | 文件大小（字节） |
| `downloadUrl` | string | 下载路径 |

**响应示例：**

```json
{
  "mediaId": "a1b2c3d4",
  "fileName": "photo.jpg",
  "mimeType": "image/jpeg",
  "fileSize": 102400,
  "downloadUrl": "/api/media/download/a1b2c3d4"
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
  -F "file=@photo.jpg"
```

```python
import requests

with open("photo.jpg", "rb") as f:
    resp = requests.post(
        "http://127.0.0.1:8765/api/media/upload",
        headers={"Authorization": "Bearer sk-your-token"},
        files={"file": ("photo.jpg", f, "image/jpeg")},
    )
print(resp.json())
# {'mediaId': 'a1b2c3d4', 'fileName': 'photo.jpg', ...}
```

---

### 6.2 下载媒体文件

通过 `mediaId` 下载已上传的媒体文件。

```
GET /api/media/download/{media_id}
```

**认证方式（二选一）：**

| 方式 | 示例 |
|------|------|
| Authorization 头 | `Authorization: Bearer sk-xxx` |
| Query 参数 | `?token=sk-xxx` |

**路径参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `media_id` | string | 媒体文件 ID |

**响应：**

文件二进制流，`Content-Type` 为文件的 MIME 类型。

**错误响应：**

| 状态码 | 说明 |
|--------|------|
| `401` | Token 无效或缺失 |
| `404` | 媒体文件不存在或已过期 |

**测试代码：**

```bash
# 通过 Authorization 头
curl -H "Authorization: Bearer sk-your-token" \
  http://127.0.0.1:8765/api/media/download/a1b2c3d4 -o photo.jpg

# 通过 Query 参数
curl "http://127.0.0.1:8765/api/media/download/a1b2c3d4?token=sk-your-token" -o photo.jpg
```

```python
import requests

resp = requests.get(
    "http://127.0.0.1:8765/api/media/download/a1b2c3d4",
    headers={"Authorization": "Bearer sk-your-token"},
)
with open("downloaded.jpg", "wb") as f:
    f.write(resp.content)
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
  "status": "ok",
  "mysql": true,
  "redis": true
}
```

**响应示例（部分不可用）：**

```json
{
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

**客户端重连建议：**

| 关闭码 | 推荐行为 |
|--------|---------|
| `4000` / `1012` | 重置重试计数器，立即快速重连 |
| `4001` | 停止重试，返回登录/Token 输入页 |
| `4002` | 停止重试，提示用户已有 Bot 在线 |
| 其他 | 指数退避重连 |
