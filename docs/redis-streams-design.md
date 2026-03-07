# 技术方案：Redis Streams 替代 Redis List 作为消息队列

## 一、技术方案概述

### 1.1 背景

当前 `/bridge/chat` 与 `/bridge/bot` 之间的消息路由通过 Redis List（RPUSH/LPOP 轮询）实现，存在以下性能瓶颈：

| 问题 | 说明 |
|------|------|
| **轮询延迟** | 空闲时每 1 秒 LPOP 一次，最大引入 1s 延迟 |
| **无效 CPU 开销** | 空 inbox 时持续轮询 Redis，每个连接一个 asyncio.Task |
| **消息不可重放** | LPOP 是破坏性消费，消息一旦取出即丢失 |
| **无消费者组** | 无法实现消息位点追踪与 ACK 确认 |

### 1.2 业务场景

服务分布式部署在 A、B、C 机器上：

- **Claw（Bot）** 基于 `/bridge/bot` 接口通过 token 连接到某台服务器
- **用户（Chat）** 基于 `/bridge/chat`（SSE）通过 token + session id 连接到某台服务器
- Bot 与 token 是 **严格 1:1** 关系：每个 bot 通过唯一的 token 建连，只消费该 token 对应队列中的消息，不同 token 的队列完全隔离

**多 Bot 隔离示例：**

```
现网有两个 bot：bot1（token1）和 bot2（token2）

bot1 连接服务器 A → 只消费 bot_inbox:token1
bot2 连接服务器 C → 只消费 bot_inbox:token2

用户 X 通过 token1 发消息 → 写入 bot_inbox:token1 → 只有 bot1 消费
用户 Y 通过 token2 发消息 → 写入 bot_inbox:token2 → 只有 bot2 消费

两条队列完全隔离，互不影响
```

**单 Bot 完整消息流转：**

1. 用户通过 token1 + session_id 发送消息 "hi" → `XADD bot_inbox:token1`
2. 用户同时基于 token1 + session_id 消费 `chat_inbox:token1:{session_id}`，等待 bot1 的回复
3. bot1 从 `bot_inbox:token1` 消费到 "hi"，流式输出回复 → `XADD chat_inbox:token1:{session_id}`

### 1.3 目标

- 将 `bridge:bot_inbox:{token}` 和 `bridge:chat_inbox:{token}:{session_id}` 从 Redis List 替换为 Redis Streams
- 使用 `XADD` / `XREADGROUP`（Consumer Group）实现生产-消费模型
- 利用 `XREADGROUP BLOCK` 阻塞读取，消除轮询延迟和空转 CPU 开销
- 抽象出 `MessageQueue` 接口层，为将来接入 RabbitMQ 等 MQ 预留扩展点
- 兼容 Redis 单机版与 Redis Cluster

### 1.4 影响范围

| 文件 | 变更类型 |
|------|----------|
| `server/services/queue.py` (**新建**) | **新增** — MessageQueue 抽象基类 + RedisStreamQueue 实现 |
| `server/services/bridge.py` | **重构** — 剥离队列操作到抽象层，消费方式从轮询改为阻塞读 |
| `server/routers/sse.py` | **修改** — SSE 流消费方式适配新队列接口 |
| `server/app.py` | **修改** — 初始化队列实例，注入到 Bridge |
| `server/infra/config.py` | **微调** — 可选增加队列类型配置 |
| `server/tests/conftest.py` | **修改** — 新增 mock_queue fixture |
| `server/tests/test_queue.py` (**新建**) | **新增** — 队列层单元测试 |
| `server/tests/test_bridge.py` | **修改** — 适配新接口 |
| `server/tests/test_sse.py` | **修改** — 适配新接口 |

---

## 二、技术选型

| 维度 | 选择 | 理由 |
|------|------|------|
| 消息队列 | Redis Streams | 原生支持、无需引入新依赖、兼容 Cluster、支持 Consumer Group |
| 消费模式 | XREADGROUP + BLOCK | 阻塞读取消除轮询，ACK 机制保障可靠消费 |
| 扩展框架 | Python ABC（抽象基类） | 轻量、无外部依赖，符合项目 Python 风格 |
| 序列化 | JSON（保持不变） | 与现有协议一致，无迁移成本 |

### 备选对比

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| Redis Pub/Sub | 实时性好 | 不持久化、Cluster 下跨 slot 限制、无 ACK | 不适合 |
| Redis List + BRPOP | 改动最小 | 仍无消费者组、无消息回溯 | 改进有限 |
| **Redis Streams** | 阻塞读、Consumer Group、消息持久化、Cluster 兼容 | API 稍复杂 | **最佳选择** |
| RabbitMQ | 完善的 MQ 特性 | 引入新基础设施依赖 | 作为扩展预留 |

---

## 三、架构设计

### 3.1 分层架构

```
┌─────────────────────────────────────────────────────────┐
│ 表现层 (Routers)                                         │
│  sse.py  /  websocket.py                                  │
├─────────────────────────────────────────────────────────┤
│ 业务层 (Services)                                        │
│  ConnectionBridge（连接管理 + 消息路由）                    │
├─────────────────────────────────────────────────────────┤
│ 队列抽象层 (Services/Queue)              ← 新增          │
│  MessageQueue (ABC)                                      │
│    ├── RedisStreamQueue（Redis Streams 实现）             │
│    └──（未来）RabbitMQQueue / KafkaQueue                  │
├─────────────────────────────────────────────────────────┤
│ 基础设施层 (Infra)                                       │
│  cache.py (Redis)  /  database.py (MySQL)                │
└─────────────────────────────────────────────────────────┘
```

### 3.2 消息流转（基于 Redis Streams 改造后）

```
服务器 B（用户连接）                 Redis Cluster              服务器 A（Claw 连接）
┌──────────────┐                ┌─────────────────┐          ┌──────────────────┐
│ Chat Client  │                │                 │          │ Bot (Claw)       │
│ (SSE)        │                │                 │          │ (WS)             │
│              │   XADD         │  bot_inbox:     │ XREAD-   │                  │
│  "hi" ───────┼───────────────►│  {token}        │─GROUP───►│  收到 "hi"       │
│              │                │  (Stream)       │ BLOCK    │                  │
│              │                │                 │          │  Claw 处理...    │
│              │   XREADGROUP   │  chat_inbox:    │  XADD    │                  │
│  收到回复 ◄──┼────────────────│  {token}:{sid}  │◄─────────│  流式输出 chunk  │
│  (BLOCK)     │                │  (Stream)       │          │                  │
└──────────────┘                └─────────────────┘          └──────────────────┘
```

**关键变化：**
- **生产端**：`RPUSH` → `XADD`（追加到 Stream）
- **消费端**：`LPOP` + `asyncio.sleep(1s)` 轮询 → `XREADGROUP BLOCK` 阻塞读取
- **消息确认**：新增 `XACK` 确认机制
- **清理机制**：`expire` TTL → `XTRIM MAXLEN ~` 限制 Stream 长度

### 3.3 Redis Streams Key 设计

| Key Pattern | Stream 用途 | Consumer Group | 说明 |
|---|---|---|---|
| `bridge:bot_inbox:{token}` | Chat→Bot 消息 | `bot` | 每个 token 一个 Stream，Bot 所在 worker XREADGROUP 消费 |
| `bridge:chat_inbox:{token}:{session_id}` | Bot→Chat 消息 | `sse` | 每个 session 一个 Stream，SSE 请求 XREADGROUP 消费 |

### 3.4 Consumer Group 与 Consumer Name 设计

#### 核心约束

- **Bot 与 token 严格 1:1**：bot1 只消费 `bot_inbox:token1`，bot2 只消费 `bot_inbox:token2`，完全隔离
- **WS chat 已移除**：`chat_inbox` 只有 SSE 消费，不存在竞争

#### Consumer Group 定位

Consumer Group 在本场景中 **不是为了多消费者负载均衡**，而是利用其 **自动位点管理** 能力：
- 通过 `>` 语法自动获取未消费的新消息，无需手动维护 last_read_id
- 通过 ACK 机制确认消息处理完成
- Bot 断连重连后（即使换 worker）能自动续接上次的消费位置

#### 隔离模型

每个 Stream 独立拥有自己的 Consumer Group，不同 token 的 Group 互不影响：

```
bot_inbox:token1  →  Group "bot"  →  Consumer "bot"  →  只有 bot1 消费
bot_inbox:token2  →  Group "bot"  →  Consumer "bot"  →  只有 bot2 消费
                     ↑ 同名但属于不同 Stream，完全隔离

chat_inbox:token1:sid_a  →  Group "sse"  →  Consumer "{request_id}"
chat_inbox:token2:sid_b  →  Group "sse"  →  Consumer "{request_id}"
                              ↑ 同理，各自独立
```

#### 命名方案

| Stream | Group Name | Consumer Name | 理由 |
|---|---|---|---|
| `bot_inbox:{token}` | `bot` | 固定值 `"bot"` | 1:1 单消费者，固定名称使 bot 断连重连（即使换 worker）后 consumer name 不变，Redis 自动续接消费位点，无 PEL 孤儿问题 |
| `chat_inbox:{token}:{session_id}` | `sse` | `{request_id}` | 每次 SSE 请求独立消费，用 request_id 唯一标识。请求前 purge + 重建 group（offset=`$`），只消费本次请求后的新消息，不会有 PEL 残留 |

---

## 四、模块划分

### 4.1 新增模块：`server/services/queue.py`

| 类/函数 | 职责 |
|---------|------|
| `MessageQueue`（ABC） | 消息队列抽象基类，定义通用接口 |
| `RedisStreamQueue` | 基于 Redis Streams 的实现 |
| `create_queue()` | 工厂函数，根据配置创建队列实例 |

### 4.2 修改模块

| 模块 | 变更点 |
|------|--------|
| `ConnectionBridge` | 将所有 `rpush/lpop` 操作替换为 `MessageQueue` 方法调用；轮询 task 改为阻塞消费 task |
| `sse.py` | `_stream_response()` 中的 `redis.lpop` 改为通过 `MessageQueue` 消费 |
| `app.py` | 初始化 `MessageQueue`，注入到 `ConnectionBridge` |

---

## 五、接口设计

### 5.1 MessageQueue 抽象接口

```python
from abc import ABC, abstractmethod
from typing import Optional


class MessageQueue(ABC):
    """消息队列抽象基类，支持不同后端实现。"""

    @abstractmethod
    async def publish(self, queue_name: str, message: str) -> str:
        """发布消息到指定队列。

        Args:
            queue_name: 队列名称（如 "bridge:bot_inbox:{token}"）
            message: JSON 序列化后的消息字符串

        Returns:
            消息 ID（Redis Streams 为自动生成的 entry ID）
        """
        ...

    @abstractmethod
    async def consume(
        self,
        queue_name: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
    ) -> Optional[tuple[str, str]]:
        """从指定队列消费一条消息（阻塞模式）。

        Args:
            queue_name: 队列名称
            group: Consumer Group 名称
            consumer: Consumer 名称
            block_ms: 阻塞等待毫秒数，0 表示永久阻塞

        Returns:
            (message_id, message_data) 或 None（超时无消息）
        """
        ...

    @abstractmethod
    async def ack(self, queue_name: str, group: str, message_id: str) -> None:
        """确认消息已被成功处理。"""
        ...

    @abstractmethod
    async def delete_queue(self, queue_name: str) -> None:
        """删除整个队列及其数据。"""
        ...

    @abstractmethod
    async def purge(self, queue_name: str) -> None:
        """清空队列中的所有消息，但保留队列本身。"""
        ...

    @abstractmethod
    async def ensure_group(self, queue_name: str, group: str) -> None:
        """确保 Consumer Group 存在，不存在则创建。"""
        ...
```

### 5.2 RedisStreamQueue 实现要点

| 方法 | Redis 命令 | 说明 |
|------|-----------|------|
| `publish` | `XADD queue_name MAXLEN ~ 1000 * data <json>` | 追加消息，近似修剪保持 Stream 大小可控 |
| `consume` | `XREADGROUP GROUP group consumer COUNT 1 BLOCK block_ms STREAMS queue_name >` | 阻塞读取未消费消息 |
| `ack` | `XACK queue_name group message_id` | 确认消费完成 |
| `delete_queue` | `DELETE queue_name` | 删除整个 Stream |
| `purge` | `XTRIM queue_name MAXLEN 0` | 清空 Stream |
| `ensure_group` | `XGROUP CREATE queue_name group $ MKSTREAM` | 懒创建 Group + Stream |

### 5.3 ConnectionBridge 接口变更

```python
class ConnectionBridge:
    def __init__(self, redis: Redis, session_store: SessionStore, queue: MessageQueue):
        # 新增 queue 参数
        self._queue = queue
        ...
```

**内部方法变更对照表：**

| 原方法 | 原实现 | 新实现 |
|--------|--------|--------|
| `send_to_bot()` | `redis.rpush(bot_inbox, ...)` | `queue.publish(bot_inbox, ...)` |
| `_send_to_session()` | `redis.rpush(chat_inbox, ...)` | `queue.publish(chat_inbox, ...)` |
| `_poll_bot_inbox()` | `redis.lpop(inbox)` + `sleep(1)` | `queue.consume(inbox, "bot", "bot", block_ms=5000)` + `queue.ack(...)` |
| `_poll_chat_inbox()` | `redis.lpop(inbox)` + `sleep(1)` | `queue.consume(inbox, "sse", consumer, block_ms=5000)` + `queue.ack(...)` |
| `unregister_bot()` | `redis.delete(bot_inbox)` | `queue.delete_queue(bot_inbox)` |
| `unregister_chat()` | `redis.delete(chat_inbox)` | `queue.delete_queue(chat_inbox)` |

### 5.4 SSE 路由变更

`sse.py` 的 `_stream_response()` 改为通过 `MessageQueue` 消费：

```python
# 改造前
raw = await redis.lpop(inbox)

# 改造后
result = await queue.consume(inbox, group="sse", consumer=req_id, block_ms=1000)
if result:
    msg_id, raw = result
    await queue.ack(inbox, "sse", msg_id)
```

SSE 场景下 `block_ms` 使用较短的值（1000ms），以便在无消息时仍能发送 SSE heartbeat comment。

**清理旧 inbox**（原 `sse.py:214`）：

```python
# 改造前
await redis.delete(f"{_CHAT_INBOX_PREFIX}{token}:{session_id}")

# 改造后
await queue.purge(inbox)
await queue.ensure_group(inbox, "sse")  # 以 offset="$" 重建，只消费新消息
```

---

## 六、数据模型

### 6.1 Redis Stream Entry 格式

**Bot Inbox（`bridge:bot_inbox:{token}`）：**

```
Stream Entry:
  ID: 自动生成（如 1709827200000-0）
  Fields:
    data: '{"rpc_request": {"jsonrpc":"2.0", "id":"req_xxx", ...}}'
```

特殊消息（disconnect 命令）：

```
Fields:
  data: '{"_disconnect": true}'
```

**Chat Inbox（`bridge:chat_inbox:{token}:{session_id}`）：**

```
Stream Entry:
  ID: 自动生成
  Fields:
    data: '{"type":"chunk","content":"hello"}'
```

### 6.2 Consumer Group 配置

| Stream Key Pattern | Group Name | Consumer Name | 消费者数量 | 说明 |
|---|---|---|---|---|
| `bot_inbox:{token}` | `bot` | `"bot"`（固定） | 1（bot 与 token 1:1） | 利用 Group 的位点追踪能力，bot 断连重连自动续接 |
| `chat_inbox:{token}:{session_id}` | `sse` | `{request_id}` | 1（每次 SSE 请求） | 请求前 purge + 重建 group，只消费新消息 |

### 6.3 配置模型扩展

```python
# infra/config.py 新增
@dataclass
class QueueConfig:
    type: str = "redis_stream"  # "redis_stream" | "rabbitmq"（未来）
    max_stream_len: int = 1000  # MAXLEN ~ 近似修剪上限
    block_ms: int = 5000        # 消费阻塞超时
```

环境变量：
- `QUEUE_TYPE=redis_stream`（默认，当前唯一实现）
- `QUEUE_MAX_STREAM_LEN=1000`
- `QUEUE_BLOCK_MS=5000`

---

## 七、关键设计决策

### 7.1 为什么用 XREADGROUP 而非 XREAD

即便 bot_inbox 只有单消费者，仍使用 XREADGROUP，因为：

- **自动位点管理**：通过 `>` 语法只读取新的未消费消息，无需手动维护 last_read_id
- **ACK 确认**：消费后 XACK 确认，未确认的消息可通过 XPENDING 追踪
- **断连续接**：bot 断连重连后（即使换了 worker），由于 consumer name 固定为 `"bot"`，Redis 自动从上次消费位点继续

### 7.2 Consumer Name 与 Worker 解耦

Consumer Name **不使用 worker_id**，原因：

- bot 可能断连后重连到不同 worker（A → C）
- 若 consumer name = worker_id，重连后 name 变化，旧 consumer 的 PEL 成为孤儿
- 固定 consumer name `"bot"` 使身份与 worker 无关，Redis 视为同一消费者续接

SSE 场景使用 `request_id` 作为 consumer name，配合每次请求 purge + 重建 group 的模式，天然无 PEL 残留问题。

### 7.3 SSE 场景特殊处理

SSE 请求是短生命周期（最长 5 分钟），设计要点：

1. **清理旧消息**：SSE 请求前 purge + 重建 group（offset=`$`），等效于当前 `redis.delete(inbox)`
2. **短阻塞时间**：`block_ms=1000`，以便定期发送 heartbeat comment
3. **单一消费模式**：chat inbox 只有 SSE 消费，单一 consumer group 即可

### 7.4 Stream 修剪策略

使用 `MAXLEN ~ 1000` 近似修剪：

- `~` 表示 Redis 可以稍微超过限制（实际 ~1000-1100 条），性能远优于精确修剪
- 1000 条对于单个 session 的消息量绰绰有余（一次对话通常几十到几百条 chunk）
- 每次 `XADD` 自带修剪，无需额外定时任务

### 7.5 Cluster 兼容性

- 每个 Stream Key 独立分槽，无跨 slot 操作
- `XADD`、`XREADGROUP`、`XACK` 均为单 key 命令，天然 Cluster 友好
- 不使用 `{hash_tag}`，各 inbox 独立、无需共槽

### 7.6 扩展性设计

通过 `MessageQueue` ABC 保留扩展点：

- 新增 MQ 后端只需实现 `MessageQueue` 接口
- 通过 `QueueConfig.type` 配置选择后端
- `create_queue()` 工厂函数根据配置创建对应实现
- `ConnectionBridge` 和 `sse.py` 仅依赖 `MessageQueue` 接口，无需感知底层实现

---

## 八、实现计划

| 阶段 | 任务 | 涉及文件 | 说明 |
|------|------|----------|------|
| **P1** | 定义 `MessageQueue` ABC | `services/queue.py` | 定义抽象接口 |
| **P2** | 实现 `RedisStreamQueue` | `services/queue.py` | XADD / XREADGROUP / XACK / XTRIM 封装 |
| **P3** | 重构 `ConnectionBridge` | `services/bridge.py` | 注入 `MessageQueue`，替换所有 RPUSH/LPOP |
| **P4** | 适配 SSE 路由 | `routers/sse.py` | `_stream_response()` 使用 `MessageQueue` |
| **P5** | 初始化注入 | `app.py`、`infra/config.py` | 创建队列实例，注入 Bridge |
| **P6** | 单元测试 | `tests/test_queue.py`、`tests/test_bridge.py`、`tests/test_sse.py`、`tests/conftest.py` | 新增队列测试 + 适配现有测试 |
