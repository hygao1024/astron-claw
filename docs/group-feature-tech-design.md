# Astron Claw 群组功能技术方案

## Context

当前 Astron Claw 仅支持 1 Token = 1 Bot 的一对一直聊模式（SSE 推送）。需要新增群组功能，让多个 Agent（Token）在同一群组中协作对话。用户通过前端管理后台创建群组、拉入 Agent，群组 ID 可导出到 OpenClaw 配置中使用。因为 Agent 会主动发言，群聊客户端需要使用 WebSocket（双向），与现有 SSE 直聊完全独立，互不影响。

---

## 一、技术选型

| 层级 | 选型 | 理由 |
|------|------|------|
| 群聊客户端通信 | WebSocket（新端点） | Agent 主动发言需双向推送；beta 版不兼容 SSE |
| 数据存储 | MySQL（GORM）+ Redis Streams | 复用现有基础设施 |
| 群组管理 API | Gin REST（Admin cookie 鉴权） | 复用现有 admin 中间件 |
| 前端 | Vue 3 + Pinia + Naive UI | 复用现有技术栈 |

---

## 二、架构设计

```
┌─────────────────┐     WS /bridge/group/:groupId     ┌──────────────┐
│  Group Chat UI  │◄──────────────────────────────────►│  Go Backend  │
│  (Vue 3 + WS)   │                                    │              │
└─────────────────┘                                    │  GroupChat   │
                                                       │  Manager     │
┌─────────────────┐     WS /bridge/bot                 │              │
│  Bot A (Token1) │◄──────────────────────────────────►│  Connection  │
│  OpenClaw Agent │                                    │  Bridge      │
└─────────────────┘                                    │              │
┌─────────────────┐     WS /bridge/bot                 │              │
│  Bot B (Token2) │◄──────────────────────────────────►│              │
│  OpenClaw Agent │                                    └──────┬───────┘
└─────────────────┘                                           │
                                                       ┌──────┴───────┐
                                                       │ MySQL + Redis│
                                                       └──────────────┘
```

**消息流**：
1. 群聊客户端通过 WS 发送消息 → GroupChatManager 查找群组内所有 Agent Token
2. 对每个 Agent 调用现有 `SendToBot()` → 消息进入各 Bot 的 Redis inbox stream
3. Bot 处理后通过现有 WS 发回 `session/update` → `HandleBotMessage()` 转译事件
4. Bridge 检测到该 session 属于群组 → 额外广播到 `bridge:group_inbox:{groupId}` stream
5. GroupChatManager 的消费者协程读取 group inbox → 扇出到所有连接该群组的 WS 客户端

---

## 三、数据模型

### 3.1 新建表（Migration `000003_add_groups`）

**文件**: `backend/migrations/000003_add_groups.up.sql`

```sql
CREATE TABLE IF NOT EXISTS `groups` (
    `id`          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `group_id`    VARCHAR(36) NOT NULL,
    `name`        VARCHAR(255) NOT NULL DEFAULT '',
    `description` TEXT NOT NULL,
    `created_at`  DATETIME NOT NULL,
    `updated_at`  DATETIME NOT NULL,
    UNIQUE INDEX `uk_groups_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `group_agents` (
    `id`         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    `group_id`   VARCHAR(36) NOT NULL,
    `token`      VARCHAR(64) NOT NULL,
    `added_at`   DATETIME NOT NULL,
    INDEX `idx_group_agents_group_id` (`group_id`),
    INDEX `idx_group_agents_token` (`token`),
    UNIQUE INDEX `uk_group_agents_group_token` (`group_id`, `token`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

- `groups.group_id`: UUID，对外暴露的群组 ID，用于 OpenClaw 配置
- `group_agents.token`: 虚拟外键引用 `tokens.token`（不加 DB 级 FK 约束，与 `chat_sessions.token` 模式一致）

### 3.2 GORM 模型

**文件**: `backend/internal/model/group.go`

```go
type Group struct {
    ID          uint      `gorm:"primaryKey;autoIncrement" json:"-"`
    GroupID     string    `gorm:"column:group_id;type:varchar(36);uniqueIndex:uk_groups_group_id;not null" json:"group_id"`
    Name        string    `gorm:"column:name;type:varchar(255);default:''" json:"name"`
    Description string    `gorm:"column:description;type:text;not null" json:"description"`
    CreatedAt   time.Time `gorm:"column:created_at;type:datetime;not null" json:"created_at"`
    UpdatedAt   time.Time `gorm:"column:updated_at;type:datetime;not null" json:"updated_at"`
}

type GroupAgent struct {
    ID      uint      `gorm:"primaryKey;autoIncrement" json:"-"`
    GroupID string    `gorm:"column:group_id;type:varchar(36);not null" json:"group_id"`
    Token   string    `gorm:"column:token;type:varchar(64);not null" json:"token"`
    AddedAt time.Time `gorm:"column:added_at;type:datetime;not null" json:"added_at"`
}
```

### 3.3 错误码

**文件**: `backend/internal/model/errors.go`（追加）

```go
// Group
ErrGroupNotFound      = AppError{http.StatusNotFound, "Group not found"}
ErrGroupAgentExists   = AppError{http.StatusConflict, "Agent already in group"}
ErrGroupAgentNotFound = AppError{http.StatusNotFound, "Agent not in group"}
ErrGroupInvalidReq    = AppError{http.StatusBadRequest, "Invalid group request"}

// Group WebSocket
ErrWSGroupNotFound    = AppError{4010, "Group not found"}
ErrWSGroupNoAgents    = AppError{4011, "No agents in group"}
```

---

## 四、模块划分

### 4.1 GroupManager（群组 CRUD 服务）

**文件**: `backend/internal/service/group_manager.go`

| 方法 | 功能 |
|------|------|
| `Create(ctx, name, desc) (*Group, error)` | 创建群组，生成 UUID |
| `Get(ctx, groupID) (*Group, error)` | 查询单个群组 |
| `List(ctx, page, pageSize) (*GroupListResult, error)` | 分页列表，含 agent_count |
| `Update(ctx, groupID, name?, desc?) error` | 部分更新 |
| `Delete(ctx, groupID) error` | 删除群组 + 成员（事务） |
| `AddAgent(ctx, groupID, token) error` | 添加 agent（校验 token 存在） |
| `RemoveAgent(ctx, groupID, token) error` | 移除 agent |
| `GetAgents(ctx, groupID) ([]GroupAgentInfo, error)` | 查询群组内所有 agent（含 name、online 状态） |
| `GetAgentTokens(ctx, groupID) ([]string, error)` | 仅返回 token 列表（消息路由用） |

### 4.2 GroupChatManager（群组消息路由服务）

**文件**: `backend/internal/service/group_chat.go`

**Redis 键设计**：

| 键 | 类型 | 用途 |
|----|------|------|
| `bridge:group_inbox:{groupId}` | Stream | 群组消息广播通道，所有 bot 的回复汇聚于此 |
| `bridge:group_session:{sessionId}` | String (value=groupId) | 映射 sessionId→groupId，TTL 1h |

**核心结构**：

```go
type GroupChatManager struct {
    bridge   *ConnectionBridge
    groupMgr *GroupManager
    tokenMgr *TokenManager
    queue    MessageQueue
    rdb      redis.UniversalClient

    conns       sync.Map // groupID -> map[string]*GroupWSConn
    consumers   sync.Map // groupID -> context.CancelFunc
}
```

**关键方法**：

| 方法 | 功能 |
|------|------|
| `RegisterGroupSession(ctx, groupID, sessionID)` | Redis SET 映射 session→group，TTL 1h |
| `LookupGroupForSession(ctx, sessionID) (string, bool)` | 查询 session 是否属于群组 |
| `BroadcastToGroup(ctx, groupID, event)` | 发布事件到 group inbox stream |
| `RegisterConn(groupID, conn)` | 注册 WS 连接，按需启动消费者协程 |
| `UnregisterConn(groupID, connID)` | 注销 WS 连接，无连接时停止消费者 |
| `SendMessageToGroup(ctx, groupID, sessionID, content, mediaURLs)` | 对群组内每个 agent 调用 bridge.SendToBot() |
| `startConsumer(ctx, groupID)` | 从 group inbox stream 读取并扇出到所有 WS 连接 |
| `Shutdown()` | 关闭所有消费者和连接 |

### 4.3 修改 ConnectionBridge（最小侵入）

**文件**: `backend/internal/service/bridge.go`

修改点仅 1 处 — `HandleBotMessage` 方法中的 `sendToSession` 调用后追加群组广播：

```go
// 在 sendToSession 之后（约 line 483）
if b.groupChat != nil && sessionID != "" {
    if groupID, ok := b.groupChat.LookupGroupForSession(ctx, sessionID); ok {
        agentEvent := copyMap(chatEvent)
        agentEvent["agent"] = map[string]interface{}{
            "token": token,
            // name 通过 tokenMgr 查询
        }
        b.groupChat.BroadcastToGroup(ctx, groupID, agentEvent)
    }
}
```

新增字段：
```go
type ConnectionBridge struct {
    // ... existing fields ...
    groupChat *GroupChatManager // 可选，nil-safe
}
```

新增方法：
```go
func (b *ConnectionBridge) SetGroupChat(gc *GroupChatManager)
```

---

## 五、接口设计

### 5.1 Admin API — 群组管理

所有路由在 `/api/admin/groups` 下，使用 admin session cookie 鉴权。

**文件**: `backend/internal/router/groups.go`

| Method | Path | 功能 | 请求体 | 响应 |
|--------|------|------|--------|------|
| GET | `/api/admin/groups` | 列表 | `?page=1&page_size=20` | `{code:0, groups:[], total, page, page_size}` |
| POST | `/api/admin/groups` | 创建 | `{name, description}` | `{code:0, group: {group_id, name, ...}}` |
| GET | `/api/admin/groups/:groupId` | 详情 | - | `{code:0, group: {...}, agents: [{token, name, bot_online}]}` |
| PATCH | `/api/admin/groups/:groupId` | 更新 | `{name?, description?}` | `{code:0}` |
| DELETE | `/api/admin/groups/:groupId` | 删除 | - | `{code:0}` |
| POST | `/api/admin/groups/:groupId/agents` | 添加 Agent | `{token}` | `{code:0}` |
| DELETE | `/api/admin/groups/:groupId/agents/:token` | 移除 Agent | - | `{code:0}` |

### 5.2 WebSocket — 群组聊天

**路由**: `GET /bridge/group/:groupId`

**鉴权**: Bearer token（query param `?token=` 或 Header），复用现有 TokenAuth 逻辑。在 `middleware/token_auth.go` 的 `excludedPaths` 中添加 `/bridge/group/`（前缀匹配），改为 handler 内自行校验。

**协议（JSON over WebSocket）**：

客户端 → 服务端：
```json
{
    "type": "message",
    "content": "Hello group!",
    "media": [{"type": "url", "content": "https://..."}]
}
```

服务端 → 客户端（初始连接）：
```json
{
    "type": "session",
    "sessionId": "uuid",
    "groupId": "group-uuid",
    "agents": [{"token": "sk-xxx...", "name": "Agent A"}, ...]
}
```

服务端 → 客户端（Agent 回复，所有事件均携带 agent 标识）：
```json
{
    "type": "chunk|done|thinking|tool_call|tool_result|media|error",
    "agent": {"token": "sk-xxx...", "name": "Agent A"},
    "sessionId": "uuid",
    "content": "...",
    // ... 其他字段与现有 SSE 事件结构一致
}
```

服务端 → 客户端（心跳）：
```json
{"type": "ping"}
```

客户端 → 服务端（心跳回复）：
```json
{"type": "pong"}
```

---

## 六、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 群组 ID 格式 | UUID (VARCHAR 36) | 与 session_id 一致，安全可外部使用 |
| 群聊传输协议 | WebSocket | Agent 主动发言需双向；beta 不兼容 SSE |
| 群组消息扇出 | 服务端单消费者协程 + 内存扇出 | beta 足够简单，避免多消费者组复杂度 |
| Session 归属追踪 | Redis String `bridge:group_session:{sid}` → groupId | O(1) 查找，TTL 自动清理 |
| Bridge 修改策略 | 追加 groupChat 字段 + HandleBotMessage 后置钩子 | 最小侵入，nil-safe，不影响现有直聊 |
| Plugin 改动 | Beta 不改 | Bot 无需感知群组语义，只需按 session/prompt 响应 |
| 群组 WS 鉴权 | 任意有效 Token 即可加入 | Beta 简化，ACL 后续迭代 |
| 删除 Token 时的级联 | 从 group_agents 移除 | 应用层处理，避免 DB FK |
| 多 Agent 回复展示 | 独立消息气泡 + Agent 标识 | 清晰区分不同 Agent，支持并发流式输出 |

---

## 七、实现计划

### Phase 1：数据层 + GroupManager（后端）
1. 创建 `backend/migrations/000003_add_groups.up.sql` 和 `.down.sql`
2. 创建 `backend/internal/model/group.go`（Group、GroupAgent 模型）
3. 在 `backend/internal/model/errors.go` 追加群组错误码
4. 创建 `backend/internal/service/group_manager.go`（CRUD 服务）
5. 在 `cmd/server/main.go` 初始化 GroupManager

### Phase 2：Admin API + 前端管理（后端 + 前端）
1. 创建 `backend/internal/router/groups.go`（7 个 admin 端点）
2. 在 `router.App` 中添加 `GroupMgr` 字段
3. 在 `router.go` admin 组下注册群组路由
4. 前端：创建 `web/src/api/group.ts`（群组 API 客户端）
5. 前端：创建 `web/src/stores/groupAdmin.ts`（群组管理 store）
6. 前端：在 `AdminView.vue` 中添加 Groups Tab（群组列表 + CRUD + Agent 管理）
7. 前端：在 `web/src/types/index.ts` 添加群组类型定义

### Phase 3：群组消息路由（后端核心）
1. 创建 `backend/internal/service/group_chat.go`（GroupChatManager）
2. 在 `bridge.go` 添加 `groupChat` 字段和 `SetGroupChat()` 方法
3. 修改 `HandleBotMessage()` — 在 `sendToSession` 后追加群组广播逻辑
4. 在 `cmd/server/main.go` 初始化 GroupChatManager 并注入 Bridge

### Phase 4：群组聊天 WebSocket + 前端聊天（后端 + 前端）
1. 创建 `backend/internal/router/group_ws.go`（WS handler）
2. 在 `router.go` 注册 `GET /bridge/group/:groupId`
3. 在 `middleware/token_auth.go` 豁免 `/bridge/group/` 路径（handler 自行鉴权）
4. 前端：创建 `web/src/composables/useGroupWS.ts`（WS 连接管理）
5. 前端：创建 `web/src/stores/groupChat.ts`（群聊 store，处理多 agent 并发回复）
6. 前端：创建 `web/src/views/GroupChatView.vue`（群聊页面）
7. 前端：在 `router/index.ts` 添加 `/group/:groupId` 路由

### Phase 5：边界处理 + 收尾
1. 删除 Token 时级联清理 group_agents（修改 `admin.go` 的 `adminDeleteToken`）
2. 删除群组时关闭所有活跃 WS 连接
3. Agent 离线时向群组发送通知事件
4. Redis key cleanup（group inbox、group session map）
5. GroupChatManager.Shutdown() 集成到 main.go 优雅关闭流程

---

## 八、需修改的关键文件清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `backend/migrations/000003_add_groups.up.sql` | **新建** | 建表 |
| `backend/migrations/000003_add_groups.down.sql` | **新建** | 回滚 |
| `backend/internal/model/group.go` | **新建** | GORM 模型 |
| `backend/internal/model/errors.go` | 追加 | 群组错误码 |
| `backend/internal/service/group_manager.go` | **新建** | 群组 CRUD |
| `backend/internal/service/group_chat.go` | **新建** | 群组消息路由 + WS 管理 |
| `backend/internal/service/bridge.go` | 小改 | 添加 groupChat 字段 + HandleBotMessage 钩子 |
| `backend/internal/router/groups.go` | **新建** | Admin API |
| `backend/internal/router/group_ws.go` | **新建** | 群聊 WS handler |
| `backend/internal/router/router.go` | 小改 | 注册路由 + App 字段 |
| `backend/internal/router/admin.go` | 小改 | deleteToken 级联清理 |
| `backend/internal/middleware/token_auth.go` | 小改 | 豁免 `/bridge/group/` |
| `backend/cmd/server/main.go` | 小改 | 初始化 + 优雅关闭 |
| `web/src/types/index.ts` | 追加 | 群组类型 |
| `web/src/api/group.ts` | **新建** | API 客户端 |
| `web/src/stores/groupAdmin.ts` | **新建** | 群组管理 store |
| `web/src/stores/groupChat.ts` | **新建** | 群聊 store |
| `web/src/composables/useGroupWS.ts` | **新建** | WS composable |
| `web/src/views/GroupChatView.vue` | **新建** | 群聊页面 |
| `web/src/views/AdminView.vue` | 中改 | 添加 Groups Tab |
| `web/src/router/index.ts` | 小改 | 添加路由 |

---

## 九、验证方案

1. **数据层验证**：启动后端，确认 migration 自动执行，`groups` 和 `group_agents` 表创建成功
2. **Admin API 验证**：通过 curl/Postman 测试全部 7 个群组 CRUD 端点
3. **前端管理验证**：在 Admin 面板创建群组、添加/移除 Agent、编辑/删除群组
4. **群组消息路由验证**：
   - 创建群组，添加 2 个 Agent Token
   - 启动 2 个 Bot 连接到对应 Token
   - 打开群聊 WS 连接，发送消息
   - 验证 2 个 Bot 均收到 `session/prompt`
   - 验证 2 个 Bot 的回复均通过 WS 推送到客户端，且携带正确的 `agent` 标识
5. **边界验证**：删除 Token 后验证 group_agents 级联清理；Agent 离线后验证群组通知

---

## 附录：Multi-Agent 团队配置示例

以下以一个 5 人 AI 团队为例，演示完整的配置流程。

### 团队成员

| Agent | agentId | 职责 |
|-------|---------|------|
| AIBoss | aiboss | 团队协调、任务分发 |
| AINews | ainews | AI 行业资讯收集、每日推送 |
| AIContent | aicontent | 文章写作、视频脚本、社交媒体内容 |
| AICode | aicode | 代码审查、技术方案、问题解决 |
| AITask | aitask | 任务跟踪、提醒、进度管理 |

### Step 1：Astron Claw 侧 — 创建 Token（Admin 面板）

在 Admin 面板为每个 Agent 创建独立的 Token，**命名与 agentId 对应**以便识别：

| Token Name | 生成的 Token 值 | 用途 |
|------------|-----------------|------|
| aiboss | `sk-a1b2c3d4e5f6...` | AIBoss 连接凭证 |
| ainews | `sk-f7e8d9c0b1a2...` | AINews 连接凭证 |
| aicontent | `sk-1a2b3c4d5e6f...` | AIContent 连接凭证 |
| aicode | `sk-9f8e7d6c5b4a...` | AICode 连接凭证 |
| aitask | `sk-0a1b2c3d4e5f...` | AITask 连接凭证 |

也可以通过 Admin API 批量创建：

```bash
# 为每个 agent 创建 token
for name in aiboss ainews aicontent aicode aitask; do
  curl -s -X POST http://localhost:8765/api/admin/tokens \
    -H "Cookie: admin_session=YOUR_SESSION" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"$name\", \"expires_in\": 0}" | jq .
done
```

### Step 2：Astron Claw 侧 — 创建群组并添加 Agent

**通过 Admin 面板操作**：
1. 进入 Groups Tab → 点击 "Create Group"
2. 填写群组名称（如 "AI Team"）和描述
3. 创建后获得 `group_id`（如 `g-550e8400-e29b-41d4-a716-446655440000`）
4. 在群组详情中逐个添加 5 个 Agent Token

**通过 API 操作**：

```bash
# 1. 创建群组
GROUP_ID=$(curl -s -X POST http://localhost:8765/api/admin/groups \
  -H "Cookie: admin_session=YOUR_SESSION" \
  -H "Content-Type: application/json" \
  -d '{"name": "AI Team", "description": "5人AI协作团队：Boss/News/Content/Code/Task"}' \
  | jq -r '.group.group_id')

echo "Group ID: $GROUP_ID"

# 2. 将 5 个 agent token 加入群组
for token in "sk-a1b2c3d4e5f6..." "sk-f7e8d9c0b1a2..." "sk-1a2b3c4d5e6f..." "sk-9f8e7d6c5b4a..." "sk-0a1b2c3d4e5f..."; do
  curl -s -X POST "http://localhost:8765/api/admin/groups/$GROUP_ID/agents" \
    -H "Cookie: admin_session=YOUR_SESSION" \
    -H "Content-Type: application/json" \
    -d "{\"token\": \"$token\"}"
done

# 3. 验证群组配置
curl -s "http://localhost:8765/api/admin/groups/$GROUP_ID" \
  -H "Cookie: admin_session=YOUR_SESSION" | jq .
```

响应示例：

```json
{
  "code": 0,
  "group": {
    "group_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "AI Team",
    "description": "5人AI协作团队：Boss/News/Content/Code/Task"
  },
  "agents": [
    {"token": "sk-a1b2c3....", "name": "aiboss",    "bot_online": true},
    {"token": "sk-f7e8d9....", "name": "ainews",     "bot_online": true},
    {"token": "sk-1a2b3c....", "name": "aicontent",  "bot_online": false},
    {"token": "sk-9f8e7d....", "name": "aicode",     "bot_online": true},
    {"token": "sk-0a1b2c....", "name": "aitask",     "bot_online": false}
  ]
}
```

### Step 3：OpenClaw 侧 — Agent 定义与 Binding 配置

在 `~/.openclaw/openclaw.json` 中配置 5 个 Agent 及其路由绑定：

```json
{
  "agents": {
    "list": [
      {
        "id": "aiboss",
        "workspace": "~/.openclaw/workspace-aiboss",
        "description": "团队协调、任务分发"
      },
      {
        "id": "ainews",
        "workspace": "~/.openclaw/workspace-ainews",
        "description": "AI行业资讯收集、每日推送"
      },
      {
        "id": "aicontent",
        "workspace": "~/.openclaw/workspace-aicontent",
        "description": "文章写作、视频脚本、社交媒体内容"
      },
      {
        "id": "aicode",
        "workspace": "~/.openclaw/workspace-aicode",
        "description": "代码审查、技术方案、问题解决"
      },
      {
        "id": "aitask",
        "workspace": "~/.openclaw/workspace-aitask",
        "description": "任务跟踪、提醒、进度管理"
      }
    ]
  },

  "channels": {
    "astron-claw": {
      "accounts": {
        "aiboss":    { "bridge": { "url": "ws://localhost:8765/bridge/bot", "token": "sk-a1b2c3d4e5f6..." } },
        "ainews":    { "bridge": { "url": "ws://localhost:8765/bridge/bot", "token": "sk-f7e8d9c0b1a2..." } },
        "aicontent": { "bridge": { "url": "ws://localhost:8765/bridge/bot", "token": "sk-1a2b3c4d5e6f..." } },
        "aicode":    { "bridge": { "url": "ws://localhost:8765/bridge/bot", "token": "sk-9f8e7d6c5b4a..." } },
        "aitask":    { "bridge": { "url": "ws://localhost:8765/bridge/bot", "token": "sk-0a1b2c3d4e5f..." } }
      }
    }
  },

  "bindings": [
    {
      "agentId": "aiboss",
      "match": { "channel": "astron-claw", "accountId": "aiboss" }
    },
    {
      "agentId": "ainews",
      "match": { "channel": "astron-claw", "accountId": "ainews" }
    },
    {
      "agentId": "aicontent",
      "match": { "channel": "astron-claw", "accountId": "aicontent" }
    },
    {
      "agentId": "aicode",
      "match": { "channel": "astron-claw", "accountId": "aicode" }
    },
    {
      "agentId": "aitask",
      "match": { "channel": "astron-claw", "accountId": "aitask" }
    }
  ],

  "tools": {
    "agentToAgent": {
      "enabled": true,
      "allow": ["aiboss", "ainews", "aicontent", "aicode", "aitask"]
    }
  }
}
```

### Step 4：启动验证

```bash
# 1. 启动 Astron Claw 后端
cd astron-claw/backend && go run cmd/server/main.go

# 2. 启动 OpenClaw gateway（所有 agent 自动连接）
openclaw gateway restart

# 3. 检查所有 agent 连接状态
openclaw agents list --bindings

# 4. 在 Admin 面板确认 5 个 bot 均显示 Online

# 5. 打开群聊页面
# 浏览器访问: http://localhost:5173/group/550e8400-e29b-41d4-a716-446655440000
# 输入任意有效 token 登录后即可与 5 个 agent 群聊
```

### 群聊交互流程示意

```
用户: @AIBoss 帮我安排一下今天的工作

  ┌─ AIBoss (chunk): 好的，让我来协调今天的任务...
  │  AIBoss (tool_call): sessions_send → ainews
  │  AIBoss (tool_call): sessions_send → aitask
  │  AIBoss (done): 已安排：AINews 收集今日资讯，AITask 同步待办项...
  │
  ├─ AINews (chunk): 收到，正在抓取今日 AI 资讯...
  │  AINews (done): 今日要闻：1) OpenAI发布... 2) Google推出...
  │
  └─ AITask (chunk): 收到，正在同步任务列表...
     AITask (done): 今日待办：1) 完成技术方案评审 2) 发布周报...
```

> **注意**：Agent 之间通过 `agentToAgent` 的 `sessions_send` 工具互发消息实现协作。AIBoss 作为协调者可以主动指派任务给其他 Agent。每个 Agent 的回复都会实时推送到群聊中，以独立消息气泡展示。
