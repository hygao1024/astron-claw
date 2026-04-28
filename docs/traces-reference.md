# Traces 参考文档

Astron-Claw 的 Span 参考总表，按稳定业务语义列出推荐的追踪名称。

> 设计说明与补充约束见：[traces-reference-notes.md](traces-reference-notes.md)

---

## Span 总表

| Span 名称 | 域 | SpanKind | 父 Span | 说明 | 推荐维度 | 出现条件 |
|-----------|----|----------|---------|------|----------|----------|
| `HTTP {method}` | HTTP | SERVER | 无 | otelgin 自动创建的传输层入口 Span，例如 `HTTP POST` | `http.method`, `http.target`, `http.status_code`, `http.user_agent` | 所有 HTTP 请求 |
| `chat.turn` | Chat | INTERNAL | `HTTP POST /bridge/chat` | 单轮对话主流程，从接收 chat 请求到本轮结束 | `token_id`, `session_id`, `turn_id`, `is_new_session`, `close_reason`, `user_input`, `bot_reply` | 每次 `POST /bridge/chat` |
| `chat.session.resolve` | Chat | INTERNAL | `chat.turn` | 解析已有 session 或创建新 session | `token_id`, `session_id`, `is_new_session`, `error_code`, `user_input` | 每次 `POST /bridge/chat` |
| `chat.bot.availability_check` | Chat | INTERNAL | `chat.turn` | 检查当前 Token 对应 Bot 是否可服务 | `token_id`, `bot_available`, `error_code`, `user_input` | 每次 `POST /bridge/chat` |
| `chat.bot.dispatch` | Chat | PRODUCER | `chat.turn` | 将本轮用户消息派发给 Bot（经由 Worker Inbox 投递） | `token_id`, `session_id`, `turn_id`, `message_size`, `media_count`, `error_code`, `user_input` | 每次 `POST /bridge/chat` |
| `chat.response.stream` | Chat | INTERNAL | `chat.turn` | 本轮 SSE 流生命周期，从首个事件到关闭 | `token_id`, `session_id`, `turn_id`, `close_reason`, `stream_duration_ms`, `bot_reply` | 每次 `POST /bridge/chat` |
| `chat.cancel` | Chat | INTERNAL | `chat.turn` | 客户端提前断开后，向 Bot 发送取消语义 | `token_id`, `session_id`, `turn_id`, `cancel_reason` | 客户端提前断开时 |
| `bot.message.receive` | Bot | INTERNAL | 无（`context.Background()`） | 接收并解析 Bot WebSocket 消息（JSON-RPC） | `token_id`, `message_type`, `method`, `session_id`, `turn_id` | Bot 发送消息时 |
| `chat.message.deliver` | Chat | PRODUCER | `bot.message.receive` | 将翻译后的事件写入 Chat Inbox，供 SSE 消费 | `token_id`, `session_id`, `event_type`, `inbox_exists` | Bot 回复投递时 |
| `bot.connection.register` | Bot | INTERNAL | 对应连接入口 Span | Bot 建立连接并完成注册 | `token_id`, `client_addr` | Bot 成功注册时 |
| `bot.connection.unregister` | Bot | INTERNAL | 无（`context.Background()`） | Bot 连接注销或下线清理 | `token_id` | Bot 断开或被清理时 |
| `session.create` | Session | INTERNAL | `chat.session.resolve` 或对应 HTTP Span | 创建新的聊天会话，并完成必要的持久化与缓存更新 | `token_id`, `session_id` | 新建会话时 |
| `session.get` | Session | INTERNAL | 对应 HTTP Span | 校验指定 session 是否属于当前 Token | `token_id`, `session_id`, `found` | 查询单个会话时 |
| `session.list` | Session | INTERNAL | 对应 HTTP Span | 获取 Token 的会话列表 | `token_id` | 列表查询时 |
| `session.remove` | Session | INTERNAL | 对应 HTTP Span | 删除 Token 的会话数据 | `token_id` | 删除会话时 |

## 字段约定

| 字段 | 说明 | 建议值 |
|------|------|--------|
| `turn_id` | 单轮对话唯一标识，推荐使用请求级 ID，而不是复用 `session_id` | 请求级唯一值 |
| `is_new_session` | `chat.session.resolve` 的解析结果，标识是否新建了 session | `true`, `false` |
| `close_reason` | `chat.response.stream` 的关闭归因 | `done`, `error`, `timeout`, `client_disconnect`, `bot_disconnect`, `write_error` |
| `cancel_reason` | `chat.cancel` 的取消归因 | `client_disconnect`, `timeout` |
| `error_code` | 错误路径上的业务错误码（`model.Code*` 常量） | 整数值 |
| `message_type` | `bot.message.receive` 的消息分类 | `ping`, `notification`, `result`, `error` |
| `method` | Bot JSON-RPC 消息的原始 method | `session/chunk`, `session/thinking`, `session/done` 等 |
| `event_type` | `chat.message.deliver` 投递的事件类型 | `chunk`, `thinking`, `done`, `error`, `media` 等 |
| `inbox_exists` | Chat Inbox 是否存在活跃的 SSE 消费者 | `true`, `false` |
| `found` | `session.get` 的查询结果，标识 session 是否存在且属于当前 token | `true`, `false` |
| `stream_duration_ms` | `chat.response.stream` 的持续时长（毫秒） | 整数值 |
| `client_addr` | `bot.connection.register` 记录的 Bot 客户端 IP | IP 地址字符串 |
| `user_input` | 用户输入内容（截断存储） | 文本内容 |
| `bot_reply` | Bot 回复内容（截断存储） | 文本内容 |