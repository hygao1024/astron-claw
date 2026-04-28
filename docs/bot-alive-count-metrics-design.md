# Bot 存活数量指标设计方案

## 背景

当前系统通过 Redis ZSET `bridge:bot_alive` 维护 Bot 心跳状态，但缺少对存活 Bot 数量的可观测性。需要一个指标来实时监控 30 秒内存活的 Bot 数量，用于容量规划、异常检测和运维告警。

## 设计目标

1. **实时性**：指标更新周期 ≤ 10 秒
2. **准确性**：统计 30 秒内有心跳的 Bot 数量（与 `BotStatusMonitor` 判定逻辑一致）
3. **低开销**：复用现有 OTel 导出周期，无需额外定时器或 goroutine
4. **容错性**：Redis 不可用时不影响其他指标上报

## 方案设计

### 指标定义

| 属性 | 值 |
|------|-----|
| 指标名 | `bridge.bot.alive_count` |
| 类型 | Int64ObservableGauge |
| 说明 | 30 秒内存活的 Bot 数量 |
| 维度 | 无（全局指标） |
| 单位 | count |

### 实现机制

采用 **OTel Observable Gauge + Callback** 模式：

```
┌─────────────────────────────────────────────────────────┐
│  OTel SDK (每 10 秒触发一次 metrics 导出)                │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
         ┌───────────────────────────┐
         │ botAliveCountCallback()   │
         │  1. 计算 cutoff 时间戳    │
         │  2. ZCOUNT 查询 Redis     │
         │  3. observer.Observe()    │
         └───────────┬───────────────┘
                     │
                     ▼
         ┌───────────────────────────┐
         │ Redis ZSET                │
         │ bridge:bot_alive          │
         │ {token: unix_timestamp}   │
         └───────────────────────────┘
```

### 核心代码

#### 1. 指标注册 (metrics.go)

```go
BotAliveCount, err = meter.Int64ObservableGauge(
    "bridge.bot.alive_count",
    metric.WithDescription("Number of bots alive in the last 30 seconds"),
    metric.WithInt64Callback(botAliveCountCallback),
)
```

#### 2. 回调函数

```go
func botAliveCountCallback(ctx context.Context, observer metric.Int64Observer) error {
    if rdbForMetrics == nil {
        observer.Observe(0)
        return nil
    }

    now := float64(time.Now().Unix())
    cutoff := now - 30.0

    // "(" prefix = exclusive, so score == cutoff is excluded (aligned with BotStatusMonitor's >= check)
    count, err := rdbForMetrics.ZCount(ctx, botAliveKey, 
        "("+fmt.Sprintf("%f", cutoff), "+inf").Result()
    if err != nil {
        observer.Observe(0)  // Redis 故障时降级为 0，保持时序连续性
        return nil
    }

    observer.Observe(count)
    return nil
}
```

#### 3. Redis 注入 (main.go)

```go
telemetry.Init(ctx, cfg.OTLP)
telemetry.EnsureInstruments()
telemetry.SetRedisForMetrics(rdb)  // 注入 Redis 客户端
```

### 技术细节

#### 时间窗口对齐

- **Bot 心跳 TTL**: 30 秒（`bot_status_monitor.go:15`）
- **指标统计窗口**: 30 秒（`now - 30.0`）
- **边界处理**: 使用 ZCOUNT 开区间 `"(" + cutoff` 排除 `score == cutoff` 的情况
- **一致性保证**: 与 `BotStatusMonitor.checkAllBots()` 的 `now - score >= 30` 判定逻辑完全对齐

**判定逻辑对比**：

| 组件 | 判定条件 | score == cutoff 时 |
|------|---------|-------------------|
| BotStatusMonitor | `now - score >= 30` | 离线 |
| IsBotConnected | `now - score < 30` | 离线 |
| botAliveCountCallback | `ZCOUNT "(" + cutoff +inf` | 不计入（开区间） |
| CountOnlineBots | `ZCOUNT "(" + cutoff +inf` | 不计入（开区间） |

#### Redis 查询效率

- **命令**: `ZCOUNT bridge:bot_alive "(<cutoff>" +inf`（开区间语法）
- **复杂度**: O(log N)，N 为 ZSET 成员数
- **性能**: 单次查询，无需遍历所有成员

#### Redis 错误处理

- **callback 行为**: Redis 错误时返回 `observer.Observe(0); return nil`
- **语义**: 降级为 0 而非时序缺失，保持 Prometheus 时序连续性
- **对齐**: 与 `CountOnlineBots` 的错误处理行为一致（吞错返回 0）

#### 多实例行为

每个后端实例通过 `service.instance.id` 标识（取 `POD_IP` 或 hostname），独立上报相同的全局 Bot 数量：

```
Instance 10.0.1.5 ──▶ Prometheus: bridge.bot.alive_count{service_instance_id="10.0.1.5"} = 42
Instance 10.0.1.6 ──▶ Prometheus: bridge.bot.alive_count{service_instance_id="10.0.1.6"} = 42
Instance 10.0.1.7 ──▶ Prometheus: bridge.bot.alive_count{service_instance_id="10.0.1.7"} = 42
```

**查询时必须聚合**，因为每个实例都上报全局值（Redis ZSET 是共享状态）：

```promql
# 推荐：取任意实例的值（所有实例值相同）
max by (service_name) (bridge_bot_alive_count)

# 或者：平均值（多实例值相同时结果一致）
avg by (service_name) (bridge_bot_alive_count)
```

**注意**：直接查询 `bridge_bot_alive_count` 会返回多条时序（每个实例一条），Grafana 图表会叠加显示，导致数值翻倍。

## 实现清单

- [x] `backend/internal/infra/telemetry/metrics.go`
  - 添加 `BotAliveCount` 全局变量
  - 实现 `botAliveCountCallback()` 回调函数
  - 添加 `SetRedisForMetrics()` 注入方法
- [x] `backend/cmd/server/main.go`
  - 调用 `telemetry.SetRedisForMetrics(rdb)` 注入 Redis 客户端
- [x] `docs/metrics-reference.md`
  - 添加 `bridge.bot.alive_count` 指标说明

## 使用示例

### Prometheus 查询

```promql
# 当前存活 Bot 数量（多实例场景必须聚合）
max by (service_name) (bridge_bot_alive_count)

# 过去 5 分钟平均存活 Bot 数量
avg_over_time(max by (service_name) (bridge_bot_alive_count)[5m:10s])

# 存活 Bot 数量突降告警（5 分钟内下降超过 50%）
max by (service_name) (bridge_bot_alive_count)
  / max by (service_name) (bridge_bot_alive_count offset 5m) < 0.5
```

### Grafana 面板

```json
{
  "title": "Bot 存活数量",
  "targets": [
    {
      "expr": "max by (service_name) (bridge_bot_alive_count)",
      "legendFormat": "存活 Bot"
    }
  ],
  "type": "graph"
}
```

## 测试验证

### 单元测试

使用 miniredis 覆盖以下场景：

| 场景 | 预期 |
|------|------|
| `rdbForMetrics == nil` | 返回 0 |
| Redis 返回错误 | 返回 0（降级） |
| ZSET 为空 | 返回 0 |
| 3 个 bot 全部在 30 秒内 | 返回 3 |
| 1 个 bot 恰好在 cutoff 边界（`score == now-30`） | 返回 0（开区间排除） |
| 2 个在线 + 1 个过期 | 返回 2 |

### 集成测试

1. 启动后端服务 + OTel Collector + Prometheus
2. 连接 3 个 Bot，等待 15 秒
3. 查询 Prometheus: `bridge_bot_alive_count` 应为 3
4. 断开 2 个 Bot，等待 35 秒
5. 查询 Prometheus: `bridge_bot_alive_count` 应为 1

### 验证命令

```bash
# 查看 OTel Collector 接收到的指标
curl http://localhost:8889/metrics | grep bridge_bot_alive_count

# 查看 Prometheus 抓取的指标
curl http://localhost:9090/api/v1/query?query=bridge_bot_alive_count
```

## 运维考虑

### 告警规则

```yaml
groups:
  - name: bot_health
    rules:
      - alert: BotCountDropped
        expr: |
          max by (service_name) (bridge_bot_alive_count)
            / max by (service_name) (bridge_bot_alive_count offset 5m) < 0.5
        for: 2m
        annotations:
          summary: "Bot 存活数量突降 {{ $value | humanizePercentage }}"
          
      - alert: NoBotAlive
        # Redis 故障时指标降级为 0（而非时序缺失），此规则可正常触发
        expr: max by (service_name) (bridge_bot_alive_count) == 0
        for: 1m
        annotations:
          summary: "所有 Bot 已断开连接（或 Redis 不可用）"
```

### 容量规划

- **正常范围**: 根据业务规模设定基线（如 10-100）
- **扩容阈值**: 存活 Bot 数量持续接近上限时触发扩容
- **缩容阈值**: 存活 Bot 数量长期低于基线 50% 时考虑缩容

## 未来扩展

### 可选增强

1. **按 Token 分组统计**（需修改为 Counter + labels）
2. **Bot 连接/断开速率指标**（需额外 Counter）
3. **Bot 心跳延迟分布**（需 Histogram）

### 不推荐的方案

❌ **定时器轮询**: 需要额外 goroutine，增加复杂度  
❌ **事件驱动上报**: Bot 连接/断开时上报，无法反映真实存活状态  
❌ **Redis Pub/Sub**: 过度设计，OTel 回调机制已足够

## 参考资料

- [OTel Go Metrics API](https://opentelemetry.io/docs/languages/go/instrumentation/#metrics)
- [Redis ZCOUNT 命令](https://redis.io/commands/zcount/)
- [Prometheus Gauge 最佳实践](https://prometheus.io/docs/practices/instrumentation/#use-labels)
