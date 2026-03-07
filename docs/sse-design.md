# HTTP SSE Chat 接入技术方案

## 1. 技术方案概述

### 背景

当前 Chat 客户端通过 WebSocket (`/bridge/chat`) 与 Bridge Server 建立全双工连接。WebSocket 的建连成本较高（协议升级握手、长连接维护、心跳管理），在弱网环境和 CDN/反向代理场景下兼容性也不如标准 HTTP。

### 目标

新增 **HTTP SSE** 接入方式，内置前端与第三方对接方均使用统一的 HTTP 接口。

### 核心思路

**`POST /bridge/chat` 就是对话接口，发消息收回复，仅此而已。**

```
POST /bridge/chat
{"token": "sk-xxx", "content": "Hello"}

→ Response (SSE stream):
event: session
data: {"sessionId":"550e8400-..."}

event: chunk
data: {"content":"Hi there!"}

event: done
data: {}
```

- 不传 `sessionId` → 自动恢复/创建会话，首个事件返回 `sessionId`
- 传 `sessionId` → 在指定会话中对话
- 会话管理（列表、新建）是独立接口，按需使用

## 2. 技术选型

| 维度 | 选型 | 理由 |
|------|------|------|
| 流式响应 | FastAPI `StreamingResponse` (`text/event-stream`) | 原生支持，无额外依赖 |
| 请求协议 | 标准 HTTP POST | 无状态，对接成本最低 |
| 消息投递 | 复用 Redis inbox (`bridge:chat_inbox:{token}:{session_id}`) | 与 WebSocket 共用，Bot 端零改动 |
| 认证 | `Authorization: Bearer {token}` 头 / 请求体 `token` 字段 | 标准 Bearer 认证，兼容无法设 Header 的场景 |

### 备选对比

| | POST SSE（本方案） | GET 长连接 + POST | WebSocket |
|--|-------------------|-------------------|-----------|
| 连接数 | 0 持久连接 | 1 持久 + N 次 POST | 1 持久连接 |
| 对接成本 | **最低**（发 POST 就行） | 中 | 高 |
| 服务端主动推送 | 不支持 | 支持 | 支持 |
| 流式传输 | 支持 | 支持 | 支持 |
| CDN/代理兼容 | **最好** | 好 | 一般 |

## 3. 架构设计

### 3.1 分层架构

```
┌───────────────────────────────────────────────────────────┐
│                      表现层 (Routers)                      │
│                                                           │
│  /bridge/chat                                             │
│  └─ POST              → routers/sse.py（对话，SSE 流响应） │
│                                                           │
│  /bridge/chat/sessions                                    │
│  ├─ GET               → routers/sse.py（列出会话）         │
│  └─ POST              → routers/sse.py（创建新会话）       │
│                                                           │
│  /bridge/bot  ── Bot 端 WebSocket（不变）                  │
├───────────────────────────────────────────────────────────┤
│                    业务层 (Services)                        │
│                                                           │
│  services/bridge.py     → 不变                             │
│  services/session_store.py → 不变                          │
│  services/token_manager.py → 不变                          │
├───────────────────────────────────────────────────────────┤
│                    数据层 (Infra)                           │
│                                                           │
│  Redis (inbox / cache / heartbeat) → 不变                  │
│  MySQL (tokens / sessions)         → 不变                  │
└───────────────────────────────────────────────────────────┘
```

### 3.2 消息流

```
Chat Client                          Bridge Server                    Bot
───────────                          ──────────────                    ───
     │                                     │                            │
     │── POST /bridge/chat ──────────────▶│                            │
     │   {"content":"Hello"}               │                            │
     │                                     │ validate token             │
     │                                     │ resolve/create session     │
     │                                     │── Redis bot_inbox ────────▶│
     │                                     │                            │
     │  ┌ SSE stream response ─────────────│◀── JSON-RPC notifications ─│
     │◀─│ event: session                   │                            │
     │◀─│ event: chunk                     │                            │
     │◀─│ event: chunk                     │                            │
     │◀─│ event: tool_call                 │                            │
     │◀─│ event: tool_result               │                            │
     │◀─│ event: done                      │                            │
     │  └──────────────────────────────────│                            │
     │                                     │                            │
     │   (下次对话带上 sessionId)            │                            │
     │── POST /bridge/chat ──────────────▶│                            │
     │   {"content":"Thanks",              │                            │
     │    "sessionId":"550e8400-..."}       │── Redis bot_inbox ────────▶│
     │◀── SSE stream ... ────────────────│                            │
```

### 3.3 关键设计决策

#### 决策 1：对话接口零前置步骤

**选择**: `POST /bridge/chat` 无需 connect/init，直接发消息即可对话

**理由**: 对接方最简路径——一个 POST 开始用。`sessionId` 自动管理：不传则恢复上次会话或创建新会话，首个 SSE 事件 `session` 返回 `sessionId`，后续请求带上即可。

#### 决策 2：会话管理独立接口

**选择**: `/bridge/chat/sessions` 独立于对话接口

**理由**: 职责分离。对话接口只管对话，会话管理只管会话。不传 `sessionId` 时自动处理，让 90% 场景不需要碰会话接口。有多会话需求时再显式调用。

#### 决策 3：无状态，无"切换会话"概念

**选择**: 去掉 `switch_session`，改为直接在 `POST /bridge/chat` 中传不同的 `sessionId`

**理由**: 纯 POST 模型天然无状态——想在哪个会话对话，就传那个 `sessionId`。不存在"当前连接绑定到某个会话"的概念，服务端无需维护连接级状态。

#### 决策 4：SSE 流生命周期 = 一轮对话

**选择**: 每次 POST 开启一个 SSE 流，`done` 或 `error` 后流关闭

**理由**: 与 HTTP 请求-响应语义对齐。一个问题 → 一个流式回答 → 连接关闭。无需 conn_id、无需心跳管理、无需断线重连。超时 5 分钟无事件自动关闭，防止悬挂。

#### 决策 5：复用 Redis inbox，Bridge 层零修改

**选择**: SSE router 直接调用 `bridge.send_to_bot()` 并从 Redis inbox LPOP 消费事件

**理由**: Bot 端写入 inbox 的逻辑完全不变。SSE handler 在请求期间创建短生命周期的 inbox consumer，读到 `done`/`error` 后停止。与 WebSocket 的长连接 consumer 共用同一 inbox 结构，Bridge 层无需任何修改。

## 4. 接口设计

### 4.1 对话 — `POST /bridge/chat`

发送消息，流式接收 AI 回复。

**请求**:
```
POST /bridge/chat HTTP/1.1
Authorization: Bearer sk-xxx
Content-Type: application/json

{
  "content": "Hello",
  "sessionId": "550e8400-...",
  "msgType": "text",
  "media": null
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 条件 | 消息内容（text 类型必填） |
| `sessionId` | string | 否 | 不传则自动恢复/创建会话 |
| `msgType` | string | 否 | `text`(默认) / `image` / `file` / `audio` / `video` |
| `media` | object | 条件 | 非 text 类型必填，含 `mediaId`, `fileName`, `mimeType`, `fileSize` |

认证: `Authorization: Bearer {token}` 头 **或** 请求体 `"token": "sk-xxx"` 字段。

**成功响应** (`text/event-stream`):
```
event: session
data: {"sessionId":"550e8400-...","sessionNumber":1}

event: chunk
data: {"content":"Hi"}

event: chunk
data: {"content":" there!"}

event: thinking
data: {"content":"Let me think about this..."}

event: tool_call
data: {"name":"search","input":"{\"query\":\"...\"}"}

event: tool_result
data: {"name":"search","status":"completed","content":"..."}

event: message
data: {"msgType":"image","content":"","media":{"mediaId":"...","fileName":"...","mimeType":"image/png","fileSize":12345,"downloadUrl":"/api/media/download/..."}}

event: done
data: {"content":"Here is the final answer."}
```

**SSE 事件类型**:

| event | 说明 | 出现次数 |
|-------|------|----------|
| `session` | 会话信息（sessionId），**首个事件** | 1 次 |
| `chunk` | 文本片段（token 级流式） | 0~N 次 |
| `thinking` | 思考过程 | 0~N 次 |
| `tool_call` | 工具调用开始 | 0~N 次 |
| `tool_result` | 工具调用结果 | 0~N 次 |
| `message` | 媒体消息（图片/文件等） | 0~N 次 |
| `done` | 回复完成，**流终止** | 1 次 |
| `error` | 处理异常，**流终止** | 0~1 次 |

**流终止条件**: `done` 或 `error` 事件后连接关闭。超时 5 分钟无事件自动关闭。

**错误响应**（HTTP 级，不返回 SSE 流）:

| HTTP Status | 响应 | 场景 |
|-------------|------|------|
| 400 | `{"ok": false, "error": "No bot connected"}` | Bot 不在线 |
| 400 | `{"ok": false, "error": "Empty message"}` | 消息为空 |
| 401 | `{"ok": false, "error": "Invalid or missing token"}` | 认证失败 |
| 404 | `{"ok": false, "error": "Session not found"}` | sessionId 无效 |
| 422 | `{"ok": false, "error": "..."}` | 请求体格式错误 |

---

### 4.2 列出会话 — `GET /bridge/chat/sessions`

**请求**:
```
GET /bridge/chat/sessions HTTP/1.1
Authorization: Bearer sk-xxx
```

**响应** (`application/json`):
```json
{
  "ok": true,
  "sessions": [
    {"id": "550e8400-...", "number": 1},
    {"id": "661f9511-...", "number": 2}
  ],
  "activeSessionId": "661f9511-..."
}
```

---

### 4.3 创建新会话 — `POST /bridge/chat/sessions`

**请求**:
```
POST /bridge/chat/sessions HTTP/1.1
Authorization: Bearer sk-xxx
```

**响应** (`application/json`):
```json
{
  "ok": true,
  "sessionId": "772a0622-...",
  "sessionNumber": 3,
  "sessions": [
    {"id": "550e8400-...", "number": 1},
    {"id": "661f9511-...", "number": 2},
    {"id": "772a0622-...", "number": 3}
  ],
  "activeSessionId": "772a0622-..."
}
```

---

### 4.4 接口总览

| 方法 | 路径 | 说明 | 响应类型 |
|------|------|------|----------|
| POST | `/bridge/chat` | **对话**（发消息，流式收回复） | SSE stream |
| GET | `/bridge/chat/sessions` | 列出会话 | JSON |
| POST | `/bridge/chat/sessions` | 创建新会话 | JSON |

**最小对接路径**: 只需 `POST /bridge/chat` + 一个 `content` 字段即可开始对话。

## 5. 数据模型

### 5.1 新增结构

无持久化新增。`POST /bridge/chat` 请求期间创建临时的 inbox consumer：

```python
# 请求局部变量，非全局状态
queue = asyncio.Queue()          # inbox → queue → SSE yield
poll_task = asyncio.Task(...)    # 后台 inbox LPOP 任务
# 请求结束后自动销毁
```

### 5.2 Redis / MySQL 变更

**无变更**。完全复用现有结构。

## 6. 模块划分

| 模块 | 文件 | 操作 | 职责 |
|------|------|------|------|
| SSE 路由 | `routers/sse.py` | **新建** | `POST /bridge/chat` + `/bridge/chat/sessions` |
| App 注册 | `app.py` | **修改** | 挂载 SSE router |
| 前端适配 | `frontend/index.html` | **修改** | 新增 SSE transport 模式 |

**不变的模块**: `services/bridge.py`、`services/session_store.py`、`services/token_manager.py`、`infra/*`、`routers/websocket.py`

## 7. 关键实现细节

### 7.1 Session 自动解析逻辑

```python
async def resolve_session(token: str, session_id: str | None) -> tuple[str, int]:
    """解析或自动创建 session。"""
    if session_id:
        # 显式指定 → 验证存在性
        if not await bridge.session_exists(token, session_id):
            raise SessionNotFound(session_id)
        return session_id, await bridge.get_session_number(token, session_id)

    # 未指定 → 恢复上次活跃会话
    active = await bridge.get_active_session(token)
    if active:
        return active, await bridge.get_session_number(token, active)

    # 首次 → 自动创建
    return await bridge.create_session(token)
```

### 7.2 SSE 流生成

```python
async def stream_response(
    token: str, session_id: str, session_number: int, redis: Redis,
):
    """从 Redis inbox 消费事件，生成 SSE 流。"""
    inbox = f"bridge:chat_inbox:{token}:{session_id}"
    deadline = time.time() + 300  # 5 分钟超时

    # 首个事件：返回 sessionId
    yield _sse_event("session", {"sessionId": session_id, "sessionNumber": session_number})

    while time.time() < deadline:
        raw = await redis.lpop(inbox)
        if raw is None:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1.0)
            continue

        event = json.loads(raw)
        event_type = event.pop("type", "message")
        yield _sse_event(event_type, event)

        if event_type in ("done", "error"):
            break


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
```

### 7.3 前端对接示例

```javascript
// 最简对接：一个 fetch，流式读取
async function chat(token, content, sessionId) {
  const body = { content };
  if (sessionId) body.sessionId = sessionId;

  const res = await fetch("/bridge/chat", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  let currentSessionId = sessionId;

  // 解析 SSE 流
  for await (const { event, data } of parseSSE(res.body)) {
    switch (event) {
      case "session":
        currentSessionId = data.sessionId;  // 保存，下次对话带上
        break;
      case "chunk":
        appendText(data.content);
        break;
      case "tool_call":
        showToolCall(data.name, data.input);
        break;
      case "tool_result":
        showToolResult(data.name, data.status, data.content);
        break;
      case "done":
        finalize(data.content);
        break;
      case "error":
        showError(data.content);
        break;
    }
  }

  return currentSessionId;
}

// 首次对话（自动创建 session）
const sid = await chat("sk-xxx", "Hello");
// 继续对话（传入 sessionId）
await chat("sk-xxx", "Tell me more", sid);
```

## 8. 实现计划

| 步骤 | 内容 | 涉及文件 |
|------|------|----------|
| **Step 1** | SSE Router：`POST /bridge/chat`（对话 + SSE 流），`GET/POST /bridge/chat/sessions`（会话管理） | `routers/sse.py`（新建） |
| **Step 2** | App 挂载：注册 SSE router | `app.py` |
| **Step 3** | 前端适配：新增 SSE transport 层，登录界面添加连接模式选择 | `frontend/index.html` |
| **Step 4** | 测试验证：流式推送、自动会话、多会话、WS 回退兼容 | 手动 + e2e |
