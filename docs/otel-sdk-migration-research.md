# OTel SDK 指标上报方式迁移调研

## 1. 当前架构分析

### 1.1 技术栈

- OTel Go SDK `v1.42.0`（`go.opentelemetry.io/otel/sdk/metric`）
- 自定义 `RedisMetricExporter` 实现 `sdkmetric.Exporter` 接口
- Redis 作为指标中转存储
- 自建 `RenderPrometheusExposition()` 将 Redis 数据渲染为 Prometheus text format

### 1.2 数据流

```
OTel SDK MeterProvider
  → PeriodicReader (每 10s)
    → RedisMetricExporter.Export()
      → Redis Hash (counters / histograms / gauges)
        → GET /api/admin/metrics → RenderPrometheusExposition() → Prometheus text
```

### 1.3 当前指标清单

| 指标名 | 类型 | 描述 | 属性 |
|--------|------|------|------|
| `bridge.chat.requests` | Int64Counter | /bridge/chat 请求总数 | `status`, `code` |
| `bridge.chat.request.duration` | Float64Histogram | /bridge/chat 首字节延迟 (s) | `status` |
| `bridge.chat.stream.duration` | Float64Histogram | SSE 流持续时间 (s) | `close_reason` |
| `bridge.chat.active_streams` | Int64UpDownCounter | 当前活跃 SSE 流数量 | — |

### 1.4 自定义 Histogram Bucket 边界

- `RequestDurationBuckets`: 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0
- `StreamDurationBuckets`: 1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0

### 1.5 当前架构的特点

优点：
- 多实例指标自动聚合（Counter 用 `HINCRBYFLOAT`，Gauge 按 PID 分片后求和）
- 无需额外基础设施（复用已有 Redis）
- 内置 Prometheus exposition endpoint

缺点：
- 自建 Redis 中转层 ~500 行代码，维护成本高
- Prometheus text 渲染逻辑手写，容易出错（cumulative 转换、label 转义等）
- Delta → Cumulative 转换在读取端完成，增加复杂度
- 不支持 Traces / Logs，扩展性差
- 非标准方案，新成员上手成本高

---

## 2. 现有基础设施

### 2.1 OTel Collector 集群

已在 K8s 集群内部署 OTel Collector（DaemonSet，6 个 Pod，namespace: `otlp`）。

```
ClusterIP Service:  otel-collector.otlp.svc.cluster.local
  - 4317/TCP  (gRPC)
  - 4318/TCP  (HTTP)
  - 4997/TCP  (Prometheus metrics endpoint)

NodePort Service:   otel-collector-nodeport
  - 4317 → 31417  (gRPC)
  - 4318 → 31418  (HTTP)
```

### 2.2 Collector Pipeline 配置

| Pipeline | Receivers | Processors | Exporters | 说明 |
|----------|-----------|------------|-----------|------|
| traces | otlp | batch | kafka (jaeger_proto) | Trace → 阿里云 Kafka |
| metrics | otlp | batch | prometheus (:4997) | Metrics → Collector 内置 Prometheus endpoint |
| logs | otlp | batch/log | kafka/log (raw) | Logs → 阿里云 Kafka |

### 2.3 Metrics 完整链路

```
App (OTel SDK)
  → OTLP gRPC push
    → otel-collector.otlp.svc.cluster.local:4317
      → Collector prometheus exporter (:4997)
        → Prometheus scrape Collector :4997
```

Collector 已配置 `prometheus` exporter（端口 4997，metric_expiration 10m），Prometheus 只需 scrape Collector 的 4997 端口即可获取所有上报的指标。

---

## 3. 标准 OTel SDK 导出方案

### 3.1 方案 A：Prometheus Exporter（Pull 模式）

应用内嵌 Prometheus HTTP handler，Prometheus 直接 scrape 应用。

```
App → Prometheus Exporter → /metrics → Prometheus scrape
```

包路径：`go.opentelemetry.io/otel/exporters/prometheus`

优点：零中间件，标准格式，删除 ~500 行代码
缺点：需要 Prometheus 能访问每个应用实例，丢失 Redis 聚合能力

### 3.2 方案 B：OTLP Exporter → OTel Collector（Push 模式）

应用通过 OTLP 协议推送指标到 OTel Collector，Collector 转发到 Prometheus。

```
App → OTLP gRPC Exporter → Collector (:4317) → Prometheus exporter (:4997) → Prometheus scrape
```

包路径：
- `go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc`（gRPC）
- `go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp`（HTTP）

优点：Push 模式，复用已有 Collector 基础设施，天然支持 Traces + Logs 扩展
缺点：多一跳网络（App → Collector → Prometheus）

---

## 4. 方案对比

| 维度 | 当前方案 (Redis Exporter) | 方案 A (Prometheus Exporter) | 方案 B (OTLP → Collector) |
|------|--------------------------|------------------------------|---------------------------|
| 额外基础设施 | 无（复用 Redis） | 需 Prometheus 直连应用 | **已有 Collector** |
| 多实例聚合 | Redis 自动聚合 | Prometheus PromQL 聚合 | Collector 聚合 |
| 代码维护量 | ~500 行自建代码 | ~20 行 | **~20 行** |
| Traces/Logs 扩展 | 不支持 | 仅 Metrics | **全信号支持** |
| 标准化程度 | 非标准 | OTel 官方 | **OTel 官方** |
| 部署复杂度 | 低 | 中 | **低（Collector 已部署）** |
| 与现有架构契合度 | — | 需新增 scrape 配置 | **直接对接已有 Collector** |

---

## 5. 推荐方案：OTLP Exporter → Collector（方案 B）

### 5.1 推荐理由

1. **基础设施已就绪**：OTel Collector 已在集群内以 DaemonSet 部署，gRPC 4317 / HTTP 4318 端口已开放
2. **Metrics pipeline 已配置**：Collector 已有 `metrics` pipeline，接收 OTLP → 导出到 Prometheus exporter (:4997)
3. **全信号统一**：Traces 和 Logs pipeline 也已配置，未来可以在同一套 SDK 中同时上报三种信号
4. **Push 模式**：无需配置 Prometheus scrape target，应用主动推送，适合 K8s 动态扩缩容
5. **大幅简化代码**：删除 ~500 行 Redis 中转代码，替换为 ~20 行标准 OTLP exporter 初始化

### 5.2 迁移后数据流

```
astron-claw (OTel SDK)
  → PeriodicReader (每 10s)
    → OTLP gRPC Exporter
      → otel-collector.otlp.svc.cluster.local:4317
        → Collector metrics pipeline
          → prometheus exporter (:4997, metric_expiration: 10m)
            → Prometheus scrape
```

### 5.3 迁移影响范围

需要修改的文件：

| 文件 | 变更 |
|------|------|
| `internal/infra/telemetry/provider.go` | 替换 RedisMetricExporter 为 OTLP gRPC Exporter |
| `internal/infra/telemetry/redis_exporter.go` | **删除** |
| `internal/infra/telemetry/reader.go` | **删除** |
| `internal/router/metrics.go` | **删除**（不再需要自建 /metrics 端点） |
| `internal/config/config.go` | 新增 `OTLPEndpoint` 配置项，移除 `ExportIntervalMs` |
| `cmd/server/main.go` | 简化初始化（Init 不再需要 rdb 参数） |
| `go.mod` | 新增 `go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc` |

不需要修改的文件：

| 文件 | 原因 |
|------|------|
| `internal/infra/telemetry/metrics.go` | 指标定义不变，OTel API 层无变化 |
| `internal/router/sse.go` | 业务埋点代码不变 |
| `internal/infra/telemetry/metrics_test.go` | 测试仍然有效 |

### 5.4 迁移后 provider.go 示意

```go
package telemetry

import (
    "context"
    "time"

    "github.com/rs/zerolog/log"
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
    sdkmetric "go.opentelemetry.io/otel/sdk/metric"
    "go.opentelemetry.io/otel/sdk/resource"
    semconv "go.opentelemetry.io/otel/semconv/v1.26.0"

    "astron-claw/backend/internal/config"
)

var provider *sdkmetric.MeterProvider

func Init(ctx context.Context, otlpCfg config.OtlpConfig) error {
    if !otlpCfg.Enabled || !otlpCfg.MetricsEnabled {
        log.Info().Msg("Telemetry metrics disabled")
        return nil
    }

    opts := []otlpmetricgrpc.Option{
        otlpmetricgrpc.WithEndpoint(otlpCfg.Endpoint),
    }
    if otlpCfg.Insecure {
        opts = append(opts, otlpmetricgrpc.WithInsecure())
    }

    exporter, err := otlpmetricgrpc.New(ctx, opts...)
    if err != nil {
        return err
    }

    res := resource.NewWithAttributes(
        semconv.SchemaURL,
        semconv.ServiceNameKey.String(otlpCfg.ServiceName),
    )

    requestDurationView := sdkmetric.NewView(
        sdkmetric.Instrument{Name: "bridge.chat.request.duration"},
        sdkmetric.Stream{
            Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
                Boundaries: RequestDurationBuckets,
            },
        },
    )
    streamDurationView := sdkmetric.NewView(
        sdkmetric.Instrument{Name: "bridge.chat.stream.duration"},
        sdkmetric.Stream{
            Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
                Boundaries: StreamDurationBuckets,
            },
        },
    )

    provider = sdkmetric.NewMeterProvider(
        sdkmetric.WithResource(res),
        sdkmetric.WithReader(
            sdkmetric.NewPeriodicReader(exporter,
                sdkmetric.WithInterval(10*time.Second),
            ),
        ),
        sdkmetric.WithView(requestDurationView, streamDurationView),
    )
    otel.SetMeterProvider(provider)

    log.Info().
        Str("service", otlpCfg.ServiceName).
        Str("endpoint", otlpCfg.Endpoint).
        Msg("OTLP metrics exporter initialised (gRPC)")

    return nil
}

func Shutdown() {
    if provider != nil {
        ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
        defer cancel()
        if err := provider.Shutdown(ctx); err != nil {
            log.Error().Err(err).Msg("OTLP telemetry shutdown error")
        }
        log.Info().Msg("OTLP telemetry shut down")
        provider = nil
    }
}
```

### 5.5 环境变量变更

```env
# 迁移前
OTLP_ENABLED=true
OTLP_SERVICE_NAME=astron-claw
OTLP_EXPORT_INTERVAL_MS=10000

# 迁移后
OTLP_ENABLED=true
OTLP_SERVICE_NAME=astron-claw
OTLP_ENDPOINT=otel-collector.otlp.svc.cluster.local:4317   # 集群内 gRPC 地址
OTLP_INSECURE=true                                          # 集群内部无需 TLS
```

如果后续需要从集群外访问，只需改为：
```env
OTLP_ENDPOINT=<节点IP>:31417
```

### 5.6 注意事项

1. **多实例聚合**：当前 Redis 方案在服务端聚合所有实例指标。迁移后每个实例独立推送到 Collector，Collector 的 prometheus exporter 会保留 `service.name` 和 `service.instance.id` 等 resource 属性作为 label，Prometheus 端用 PromQL 聚合
2. **Gauge 语义**：`active_streams` 当前通过 Redis 跨实例求和。迁移后每个实例只报告自己的值，Prometheus 用 `sum(bridge_chat_active_streams)` 聚合
3. **指标重置**：当前 `DELETE /api/admin/metrics` 清空 Redis。迁移后此功能不再适用，指标随进程重启自动重置
4. **指标名映射**：Collector 的 prometheus exporter 会自动将 OTel 指标名中的 `.` 转为 `_`，并添加 `_total` 等后缀，与当前 `reader.go` 手动转换逻辑一致
5. **metric_expiration**：Collector 配置了 `metric_expiration: 10m`，如果某个指标 10 分钟内没有新数据点，会从 Prometheus endpoint 中移除

---

## 6. 未来扩展：Traces 和 Logs

Collector 已配置 traces 和 logs pipeline，未来可以在同一个 SDK 初始化中同时启用：

```go
// Traces → Collector → Kafka → 后端
traceExporter, _ := otlptracegrpc.New(ctx,
    otlptracegrpc.WithEndpoint("otel-collector.otlp.svc.cluster.local:4317"),
    otlptracegrpc.WithInsecure(),
)
tp := sdktrace.NewTracerProvider(
    sdktrace.WithBatcher(traceExporter),
    sdktrace.WithResource(res),
)
otel.SetTracerProvider(tp)
```

三种信号共用同一个 Collector endpoint，无需额外基础设施。

---

## 7. 参考资料

- [OTel Go OTLP Metric Exporter (gRPC)](https://pkg.go.dev/go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc)
- [OTel Go OTLP Metric Exporter (HTTP)](https://pkg.go.dev/go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp)
- [OTel Go Prometheus Exporter](https://pkg.go.dev/go.opentelemetry.io/otel/exporters/prometheus)
- [OpenTelemetry Collector Prometheus Exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/prometheusexporter/README.md)
- [OpenTelemetry Go SDK](https://github.com/open-telemetry/opentelemetry-go)
