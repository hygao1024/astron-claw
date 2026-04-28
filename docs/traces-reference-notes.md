# Traces 设计说明

Astron-Claw 的追踪设计说明，补充 `traces-reference.md` 中各 Span 的设计背景、链路关系和约束。

> Span 总表见：[traces-reference.md](traces-reference.md)

---

## 概述

Astron-Claw 使用 OpenTelemetry SDK 采集分布式追踪数据，通过 OTLP gRPC 协议推送到 OTel Collector。

本文档从业务逻辑定义 Span 边界，不绑定当前代码实现细节。即使后续 bridge、queue、router 等实现发生调整，只要业务语义不变，Span 设计应尽量保持稳定。

## 设计原则

### 1. 先业务、后实现

Span 应优先表达稳定的业务阶段，而不是当前内部实现步骤。

例如：

- `chat.turn` 比 `bridge.handle_bot_message` 更稳定
- `chat.bot.dispatch` 比 `bridge.send_to_bot` 更稳定
- `chat.response.stream` 比 `bridge.send_to_session` 更稳定

### 2. 一轮对话必须有主 Span

对于 `POST /bridge/chat`，一次请求对应一轮完整对话流程：

1. 校验请求
2. 解析或创建会话
3. 将消息派发给 Bot
4. 持续接收并返回 SSE 事件
5. 直到 `done`、`error`、`timeout` 或客户端断开

因此应存在一个覆盖整轮流程的主 Span，避免只看到零散的子步骤。

### 3. HTTP Span 不是业务 Span

`otelgin` 自动生成的 HTTP Span 用于表示传输层请求，本身不是业务语义的替代品。

对于流式场景，HTTP Span 解决的是“请求来了”，业务 Span 解决的是“这一轮对话经历了什么”。

### 4. 会话和轮次要分开

`session_id` 表示长期会话上下文，不等于一次提问。

同一个 session 下可能连续发生多轮对话，因此业务追踪上应区分：

- 会话级动作：创建、查询、列出、删除
- 轮次级动作：本轮消息发送、Bot 处理、SSE 返回、取消

## 推荐业务链路

一次 `POST /bridge/chat` 的推荐追踪结构如下（包含正向和反向链路）：

```text
HTTP POST /bridge/chat                              SERVER
└── chat.turn                                       INTERNAL
    ├── chat.session.resolve                        INTERNAL
    │   └── session.create                          INTERNAL  (仅新建会话时出现)
    ├── chat.bot.availability_check                 INTERNAL
    ├── chat.bot.dispatch                           PRODUCER
    ├── chat.response.stream                        INTERNAL
    │   ├── bot.message.receive                     INTERNAL  (每条 Bot 消息触发)
    │   │   └── bot.event.translate                 INTERNAL
    │   └── chat.message.deliver                    PRODUCER  (翻译后投递到 Chat Inbox)
    └── chat.cancel                                 INTERNAL  (仅客户端提前断开时出现)
```

说明：

- `chat.turn` 是单轮对话的业务主 Span
- `chat.response.stream` 是这轮流式回复的生命周期
- `bot.message.receive` → `bot.event.translate` → `chat.message.deliver` 是反向链路（Bot 回复 → 翻译 → 投递到 Chat Inbox），在 `chat.response.stream` 期间可能出现多次
- `session.create` 属于会话域能力，不应替代 `chat.turn`
- `HTTP POST /bridge/chat` 仍然保留，但只作为传输层根入口

## 实现相关 Span 的处理建议

下列名称更偏当前实现结构，不建议作为长期稳定的顶层业务 Span：

- `bridge.handle_bot_message`
- `bridge.send_to_session`
- `bridge.send_to_bot`
- `bridge.is_bot_connected`

处理建议：

- 如果这些步骤对排障有价值，可以保留为内部子 Span
- 对外参考文档优先展示业务语义 Span
- 未来即使 queue、bridge、transport 方案调整，也尽量不要改动对外业务 Span 名称

## HTTP Span 说明

otelgin 中间件自动为所有 HTTP 请求创建 span，span 名称格式为 `HTTP {method}`，例如 `HTTP GET`、`HTTP POST`。

维度由 otelgin 自动添加，包括：

- `http.method`
- `http.target`
- `http.status_code`
- `http.user_agent`
- 等标准 HTTP 语义约定属性

建议：

- 保留 HTTP Span 作为入口
- 不要仅依赖 HTTP Span 表达业务阶段
- 对 `POST /bridge/chat`，应始终补充 `chat.turn` 这一业务主 Span

## 流关闭原因

对于 `chat.response.stream`，建议使用更贴近业务归因的 `close_reason`：

| 值 | 说明 |
|----|------|
| `done` | Bot 正常完成本轮回复 |
| `bot_error` | Bot 返回业务错误并终止 |
| `internal_error` | 服务端内部异常导致流结束 |
| `timeout` | 长时间无活动，流超时关闭 |
| `client_disconnect` | 客户端主动断开 |
| `bot_disconnect` | Bot 中途断开，无法继续完成本轮 |

相比仅使用 `error`，上述分类更利于从业务上区分问题归因。

## 反向链路说明

反向链路指 Bot 回复消息经由后端投递到 Chat SSE 的路径：

```text
Bot WebSocket → HandleBotMessage → TranslateBotEvent → sendToSession → [Redis Stream] → SSE Consume → Client
```

Bot 和 Chat 是两个独立的连接（甚至可能在不同 Worker 实例），中间通过 Redis Stream 解耦。因此反向链路存在一次异步跳跃，trace 设计需要体现这一点。

对应的 Span 设计：

- `bot.message.receive`：解析 Bot 的 WebSocket 消息，区分 ping/notification/result/error
- `bot.event.translate`：将 Bot 的 JSON-RPC method（如 `session/chunk`）翻译为 Chat SSE 事件（如 `chunk`）
- `chat.message.deliver`：将翻译后的事件写入 Chat Inbox（PRODUCER），SSE 端消费后推送给客户端

在一次 `chat.response.stream` 期间，反向链路可能触发多次（Bot 多次回复 chunk/thinking/done）。

## 跨 Worker 路由

多实例部署时，Chat SSE 和 Bot WebSocket 可能不在同一个 Worker 上。消息通过 Worker Inbox（Redis Stream）路由到持有 Bot 连接的 Worker。

正向路由（Chat → Bot）：

```text
chat.bot.dispatch (Worker A, PRODUCER)
  → PublishToWorkerInbox → [Redis Stream: bridge:worker_inbox:{workerB}]
    → runWorkerInboxConsumer (Worker B, CONSUMER)
      → conn.WriteJSON → Bot WebSocket
```

如果需要对跨 Worker 路由进行追踪，可选的 Span：

| Span 名称 | SpanKind | 说明 |
|-----------|----------|------|
| `worker.inbox.publish` | PRODUCER | 将消息发布到目标 Worker 的 Inbox |
| `worker.inbox.consume` | CONSUMER | 从本 Worker 的 Inbox 消费消息 |
| `bot.connection.forward` | INTERNAL | 将消息通过 WebSocket 转发给本地 Bot |

这些 Span 偏实现层，建议作为可选的内部子 Span，不作为顶层业务 Span。

### Context Propagation

跨 Worker 的消息经由 Redis Stream 传递，trace context 不会自动传播。需要：

1. 发布时：在消息体中注入 `traceparent` 字段（W3C Trace Context 格式）
2. 消费时：从消息体中提取 `traceparent`，作为新 Span 的 parent context

这样跨实例的 Span 才能关联到同一条 trace。

## SpanKind 约定

所有 Span 应声明 SpanKind，遵循 OTel 规范：

| SpanKind | 适用场景 | 本项目示例 |
|----------|----------|-----------|
| SERVER | 接收外部请求的入口 | `HTTP {method}`（otelgin 自动设置） |
| PRODUCER | 向异步队列发布消息 | `chat.bot.dispatch`、`chat.message.deliver` |
| CONSUMER | 从异步队列消费消息 | Worker Inbox 消费（可选） |
| INTERNAL | 进程内部的业务步骤 | `chat.turn`、`chat.session.resolve`、`bot.message.receive` 等 |

## 不建议的设计

- 只有 HTTP Span，没有单轮对话主 Span
- 把 `session_id` 当作单轮请求 ID 使用
- 用内部队列或内部桥接动作替代业务主 Span
- 在文档中把”编排层创建 session”和”存储层创建 session”写成同级、同义动作
- 只覆盖正向链路（Chat → Bot），不追踪反向链路（Bot → Chat）
- Span 缺少 SpanKind 声明
- 跨 Worker 消息不传播 trace context，导致链路断裂

## Error / Status 处理约定

Span 在出错时应设置 `Status = ERROR` 并记录异常事件，遵循 OTel 规范：

| 场景 | Span Status | 是否记录 exception 事件 |
|------|-------------|----------------------|
| 请求参数校验失败（`bad_request`） | `OK` | 否，属于正常业务拒绝 |
| Bot 未连接（`no_bot`） | `OK` | 否，属于正常业务状态 |
| Session 不存在（`session_not_found`） | `OK` | 否，属于正常业务状态 |
| Session 创建失败 | `ERROR` | 是 |
| 消息发送失败（`send_fail`） | `ERROR` | 是 |
| Bot 返回 JSON-RPC error | `ERROR` | 是，记录 `error.message` |
| 内部异常（consume error、marshal error 等） | `ERROR` | 是，记录完整 error |
| 流超时（`timeout`） | `OK` | 否，属于正常生命周期结束 |
| 客户端断开（`client_disconnect`） | `OK` | 否，属于正常行为 |
| Bot 断开（`bot_disconnect`） | `ERROR` | 是 |

原则：业务上的合理拒绝（参数错误、资源不存在）不算 ERROR；基础设施故障和意外中断才是 ERROR。

记录 exception 事件的方式（OTel 规范）：

```go
span.SetStatus(codes.Error, “send to bot failed”)
span.RecordError(err)  // 自动添加 exception.type、exception.message、exception.stacktrace
```

## 属性命名约定

自定义属性使用 `astron.` 命名空间前缀，与 OTel 语义约定的标准属性区分：

| 当前名称 | 建议名称 | 说明 |
|---------|---------|------|
| `token_prefix` | `astron.token_prefix` | Token 标识前缀 |
| `session_id` | `astron.session_id` | 会话 ID |
| `turn_id` | `astron.turn_id` | 轮次 ID |
| `worker_id` | `astron.worker_id` | Worker 实例 ID |
| `close_reason` | `astron.close_reason` | 流关闭原因 |
| `cancel_reason` | `astron.cancel_reason` | 取消原因 |
| `bot_method` | `astron.bot_method` | Bot JSON-RPC method |
| `chat_event_type` | `astron.chat_event_type` | Chat SSE 事件类型 |

OTel 语义约定的标准属性保持原始命名，不加前缀：

- `http.method`、`http.target`、`http.status_code`（HTTP 语义约定）
- `messaging.system`、`messaging.destination.name`、`messaging.operation`（Messaging 语义约定，用于 Redis Stream 相关 Span）

## 总结

推荐的稳定业务主轴是：

1. `HTTP POST /bridge/chat` 负责传输入口
2. `chat.turn` 负责单轮对话主流程
3. `chat.bot.dispatch` 负责正向消息投递（Chat → Bot）
4. `bot.message.receive` → `chat.message.deliver` 负责反向消息投递（Bot → Chat）
5. `chat.response.stream` 负责用户可感知的流式生命周期
6. `session.*` 与 `bot.connection.*` 负责领域能力与资源生命周期

这样设计后，文档不会被当前实现细节绑死，也更适合后续演进实现方案。
