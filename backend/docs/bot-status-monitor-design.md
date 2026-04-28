# Bot Status Monitor 设计文档

## 概述

Bot Status Monitor 是一个用于监控 WebSocket 连接状态并通知 SSE 层的中间件系统。当 Bot 的 WebSocket 连接断开时，系统能够在 1 秒内检测到并优雅地关闭对应的 SSE 连接。

### 核心特性

- **实时监控**: 每秒检查一次所有活跃的 SSE 连接对应的 Bot 状态
- **快速检测**: 最大检测延迟 1 秒
- **优雅关闭**: 通过非阻塞 channel 通知 SSE 连接关闭
- **并发安全**: 使用 sync.Map 管理连接，支持高并发场景
- **资源高效**: 简单 ticker 实现，适合 <1000 并发连接场景

## 架构设计

### 系统组件

```
┌─────────────────┐
│   SSE Client    │
└────────┬────────┘
         │ HTTP/SSE
         ▼
┌─────────────────┐      ┌──────────────────┐
│   SSE Handler   │◄────►│ BotStatusMonitor │
└────────┬────────┘      └────────┬─────────┘
         │                        │
         │                        │ 每1秒检查
         ▼                        ▼
┌─────────────────┐      ┌──────────────────┐
│   Bot (WS)      │      │  Redis ZSET      │
└─────────────────┘      │  bridge:bot_alive│
                         └──────────────────┘
```

### 核心数据结构

```go
// BotStatusMonitor 监控器主结构
type BotStatusMonitor struct {
    rdb            redis.UniversalClient
    ctx            context.Context
    cancel         context.CancelFunc
    wg             sync.WaitGroup
    sseConnections sync.Map // inboxKey -> *SSEConnection
}

// SSEConnection SSE 连接信息
type SSEConnection struct {
    Token       string
    SessionID   string
    InboxKey    string
    DisconnectC chan struct{} // 断开通知 channel
}
```

## 工作流程

### 1. SSE 连接建立流程

```
客户端 → POST /bridge/chat
         ↓
      验证 token & 创建 session
         ↓
      创建 inbox (Redis Stream)
         ↓
   注册到 BotStatusMonitor
         ↓
   返回 SSEConnection (包含 DisconnectC)
         ↓
      开始 SSE 流式响应
```

### 2. 监控器检查流程（每 1 秒）

```
定时器触发
    ↓
遍历 sseConnections (sync.Map)
    ↓
收集唯一的 token 列表
    ↓
对每个 token:
    ├─ 从 Redis 读取 ZScore(bridge:bot_alive, token)
    ├─ 检查心跳时间戳
    └─ 如果超过 30 秒 → 调用 notifyBotDisconnected(token)
```

### 3. Bot 断开通知流程

```
notifyBotDisconnected(token)
    ↓
遍历所有 SSE 连接
    ↓
找到匹配 token 的连接
    ↓
向 DisconnectC channel 发送信号（非阻塞）
    ↓
SSE handler 收到信号
    ↓
发送错误事件给客户端
    ↓
关闭 SSE 连接
    ↓
defer 执行 UnregisterSSEConnection
```

### 时序图

```
时间轴    Bot         Bridge      Monitor      SSE Handler    Client
  |        |            |            |              |            |
  0s       |--连接----->|            |              |            |
  |        |            |--注册----->|              |            |
  |        |            |            |<--返回conn---|            |
  |        |            |            |              |--session-->|
  |        |            |            |              |            |
  5s       |<--心跳-----|            |              |            |
  |        |            |            |--检查bot---->Redis        |
  |        |            |            |<--在线-------|            |
  |        |            |            |              |            |
 10s       X 断开       |            |              |            |
  |                     |--清理----->Redis          |            |
  |                     |            |              |            |
 11s                    |            |--检查bot---->Redis        |
  |                     |            |<--不在线-----|            |
  |                     |            |              |            |
  |                     |            |--通知------->|            |
  |                     |            |        DisconnectC        |
  |                     |            |              |--error---->|
  |                     |            |              X 关闭       |
  |                     |            |<--注销-------|            |
```

## 核心逻辑

```go
// 每1秒执行一次
func (m *BotStatusMonitor) checkAllBots() {
    // 1. 收集所有唯一的 token
    tokens := make(map[string]bool)
    m.sseConnections.Range(func(key, value interface{}) bool {
        conn := value.(*SSEConnection)
        tokens[conn.Token] = true
        return true
    })

    // 2. 检查每个 token 的 bot 状态
    for token := range tokens {
        score, err := m.rdb.ZScore(ctx, BotAliveKey, token).Result()
        if err != nil || (time.Now().Unix() - score) >= 30 {
            m.notifyBotDisconnected(token)
        }
    }
}
```

## 实现文件

| 文件 | 说明 |
|------|------|
| `internal/service/bot_status_monitor.go` | 监控器核心实现 |
| `internal/router/sse.go` | SSE 集成（注册/监听） |
| `cmd/server/main.go` | 初始化与停止 |
| `internal/router/router.go` | App 结构体字段 |

## 使用方法

### 初始化（main.go）

```go
botStatusMonitor := service.NewBotStatusMonitor(rdb)
botStatusMonitor.Start()
defer botStatusMonitor.Stop()
```

### SSE 集成（sse.go）

```go
// 注册连接
var sseConn *service.SSEConnection
if app.BotStatusMonitor != nil {
    sseConn = app.BotStatusMonitor.RegisterSSEConnection(tokenStr, sessionID, inbox)
    defer app.BotStatusMonitor.UnregisterSSEConnection(inbox)
}

// 准备 bot 断开监听 channel
var botDisconnectC <-chan struct{}
if sseConn != nil {
    botDisconnectC = sseConn.DisconnectC
} else {
    neverC := make(chan struct{})
    botDisconnectC = neverC
}

// 监听 bot 断开事件
select {
case <-c.Request.Context().Done():
    // 客户端断开
case <-botDisconnectC:
    // Bot 断开
    errEvent := pkg.FormatSSEEvent("error", map[string]interface{}{
        "content": model.ErrChatNoBot.Message,
    })
    c.Writer.WriteString(errEvent)
    return
default:
}
```

## 配置参数

```go
const (
    tickInterval = 1 * time.Second // 检查频率
    botTTL       = 30.0            // bot 心跳 TTL（秒）
)
```

## 性能特性

| 指标 | 值 |
|------|---|
| 检测延迟 | 最多 1 秒 |
| Redis 查询 | N 次/秒（N = 唯一 token 数）|
| 适用规模 | < 1000 并发连接 |
| 内存开销 | 低（只存储连接映射）|

### 资源消耗参考

| 连接数 | Redis 查询/秒 | CPU 开销 |
|--------|--------------|---------|
| 100    | ~100         | 低      |
| 1000   | ~1000        | 中      |
| 10000  | ~10000       | 高      |

## 关键设计点

1. **检测延迟**: 最多 1 秒（下一次定时器触发）
2. **通知机制**: 非阻塞 channel（buffer=1），避免死锁
3. **资源清理**: defer 自动注销，防止内存泄漏
4. **并发安全**: sync.Map 保证线程安全
5. **InboxKey 格式**: `bridge:chat_inbox:token:sessionID`（SplitN 参数为 4）

## 监控日志

| 日志 | 级别 | 说明 |
|------|------|------|
| `Bot status monitor started` | INFO | 监控器启动 |
| `Bot status monitor stopped` | INFO | 监控器停止 |
| `SSE connection registered in monitor` | DEBUG | SSE 连接注册 |
| `SSE connection unregistered from monitor` | DEBUG | SSE 连接注销 |
| `Bot disconnected detected by monitor` | INFO | 检测到 bot 断开 |
| `Notified SSE connection of bot disconnect` | DEBUG | 通知 SSE 连接 |

### 典型日志示例

```
16:34:15 INF Bot disconnected unexpectedly
16:34:16 INF Bot disconnected detected by monitor token=sk-5f413f0...
16:34:16 DBG Notified SSE connection of bot disconnect session=4b23536f... token=sk-5f413f0...
16:34:16 INF SSE: bot disconnected token=sk-5f413f0...
16:34:16 DBG SSE connection unregistered from monitor session=4b23536f... token=sk-5f413f0...
```

检测延迟：1 秒

## 扩展建议

如果并发连接数超过 1000，建议改用时间轮方案：

- 60 个槽位，每秒前进一格
- 每个 token 每 60 秒检查一次
- Redis 查询降低到 N/60 次/秒
- 检测延迟增加到 30-60 秒
