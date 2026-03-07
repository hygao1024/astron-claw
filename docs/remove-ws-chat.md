# 技术方案：移除 WebSocket /bridge/chat 端点

## 一、技术方案概述

SSE `/bridge/chat`（POST）已完全替代 WebSocket `/bridge/chat`，后者不再有使用场景。本次移除 WebSocket chat 端点及其关联的服务端状态管理、前端传输切换、测试用例和文档。

**移除范围**：WebSocket chat 端点及其独占的代码路径。
**保留范围**：WebSocket `/bridge/bot` 端点、SSE chat 端点、共享的 session/bridge 逻辑。

## 二、影响范围分析

### 2.1 需要修改的文件

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `server/routers/websocket.py` | 删除代码 | 移除 `ws_chat` handler（44-162 行），保留 `ws_bot` |
| `server/services/bridge.py` | 删除代码 | 移除 `register_chat`、`unregister_chat`、`update_chat_session`、`_poll_chat_inbox` 四个方法；移除 `_chats`、`_chat_sessions` 字典；清理 `shutdown()`、`remove_bot_sessions()`、`_run_heartbeat()` 中的 chat 相关分支 |
| `server/routers/admin.py` | 删除字段 | 移除 `chat_count` 相关字段（SSE 无状态，此计数永远为 0） |
| `server/tests/test_bridge.py` | 删除测试 | 移除 `TestRegisterChat`、`TestPollChatInbox`、`TestUpdateChatSession`、`TestUnregisterChat` |
| `server/tests/e2e/test_e2e_streaming.py` | 删除文件 | 整个文件仅测试 WS chat |
| `server/tests/e2e/test_integration.py` | 删除 WS 测试 | 移除使用 WS chat 的测试用例 |
| `server/tests/e2e/test_streaming.py` | 删除文件 | 整个文件仅测试 WS chat |
| `frontend/index.html` | 删除代码 | 移除 `connectWebSocket()`、`attemptReconnect()`、传输模式切换 UI；SSE 设为唯一模式 |
| `docs/api.md` | 删除章节 | 移除 "4. WebSocket — Chat 客户端" 整节；更新认证表和概览 |
| `docs/sse-design.md` | 更新描述 | 移除 WS 共存相关说明 |
| `docs/redis-streams-design.md` | 更新描述 | 移除 WS 弃用标注 |
| `README.md` | 更新描述 | 更新架构图和 API 表 |

### 2.2 不需要修改的文件

| 文件 | 原因 |
|------|------|
| `server/app.py` | `websocket.router` 仍需保留（`/bridge/bot` 仍在使用） |
| `server/routers/sse.py` | SSE 端点，无 WS chat 引用 |
| `server/services/state.py` | 无 chat 专属状态 |
| `server/services/queue.py` | 共享队列服务 |
| `server/services/session_store.py` | 共享 session 管理 |
| `server/tests/test_sse.py` | 仅测试 SSE |
| `server/tests/conftest.py` | 通用 fixture |
| `plugin/` | 不连接 `/bridge/chat`，仅连接 `/bridge/bot` |

## 三、详细实现计划

### Step 1：server/services/bridge.py — 清理服务层

**移除的成员变量**（`__init__` 中）：
- `self._chats: dict[str, set[WebSocket]]`
- `self._chat_sessions: dict[WebSocket, tuple[str, str]]`

**移除的方法**：
- `register_chat(token, ws, session_id)` — WS chat 注册
- `unregister_chat(token, ws)` — WS chat 注销
- `update_chat_session(ws, new_session_id)` — WS chat 切换 session
- `_poll_chat_inbox(token, session_id, ws)` — WS chat 消息轮询

**清理的方法**：
- `shutdown()` — 移除遍历 `_chat_sessions` 关闭 WS 连接的逻辑
- `remove_bot_sessions(token)` — 移除通知 chat clients 的逻辑
- `_run_heartbeat()` — 移除 `_CHAT_COUNTS_PREFIX` 刷新逻辑

**移除的常量**：
- `_CHAT_COUNTS_PREFIX`（仅用于 chat 连接计数）

**保留的共享逻辑**：
- `_CHAT_INBOX_PREFIX` — SSE 的 `_stream_response` 和 `_send_to_session` 仍在使用
- `send_to_bot`、`handle_bot_message`、`_send_to_session` — 共享
- `create_session`、`get_active_session`、`switch_session`、`get_sessions` — 共享

### Step 2：server/routers/websocket.py — 移除 ws_chat handler

- 删除 `ws_chat` 函数（约 120 行）
- 保留 `ws_bot` 函数
- 移除不再需要的 import（如果 `ws_bot` 不需要 `Query`）

### Step 3：server/routers/admin.py — 移除 chat_count 字段

- 从 admin token 列表响应中移除 `chat_count` 字段
- `get_connections_summary()` 中移除 chat 计数逻辑

### Step 4：server/tests/ — 清理测试

**删除**：
- `tests/e2e/test_e2e_streaming.py`（整个文件）
- `tests/e2e/test_streaming.py`（整个文件）

**修改**：
- `tests/test_bridge.py` — 移除 `TestRegisterChat`、`TestPollChatInbox`、`TestUpdateChatSession`、`TestUnregisterChat`
- `tests/e2e/test_integration.py` — 移除使用 WS chat 的测试

### Step 5：frontend/index.html — SSE 唯一化

- 移除 `connectWebSocket()` 函数
- 移除 `attemptReconnect()` 函数
- 移除传输模式切换 UI（`transport-ws` / `transport-sse` 按钮）
- `transportMode` 固定为 `'sse'`
- 移除 WebSocket 相关的事件处理函数

### Step 6：文档更新

- `docs/api.md` — 移除 "4. WebSocket — Chat 客户端" 整节
- `docs/sse-design.md` — 移除 WS 共存说明
- `docs/redis-streams-design.md` — 移除 WS 弃用标注
- `README.md` — 更新架构图和 API 表

## 四、关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| `_CHAT_INBOX_PREFIX` 是否移除 | 保留 | SSE 的 `_stream_response` 和 `_send_to_session` 依赖它 |
| `_CHAT_COUNTS_PREFIX` 是否移除 | 移除 | 仅用于 WS chat 连接计数，SSE 无状态不需要 |
| `websocket.py` 是否整文件删除 | 否 | `/bridge/bot` 仍在使用 |
| E2E 测试是否迁移到 SSE | 否（本次直接删除） | E2E 测试为手动脚本，SSE 已有 `test_sse.py` 覆盖核心逻辑 |
| 前端是否保留传输切换 | 否 | WS 已移除，无切换必要 |

## 五、验证计划

1. 运行现有单元测试：`pytest server/tests/test_bridge.py server/tests/test_sse.py` — 确认无回归
2. 确认 `/bridge/bot` WebSocket 仍正常工作
3. 确认 SSE `POST /bridge/chat` 仍正常工作
4. 确认 admin 接口正常（无 chat_count 字段）
5. 确认前端 SSE 模式正常对话
