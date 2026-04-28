# OTel SDK Metrics Migration: Redis Exporter → OTLP gRPC Exporter

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the custom RedisMetricExporter (~500 lines) with the standard OTLP gRPC exporter (~20 lines), pushing metrics to the existing OTel Collector.

**Architecture:** The app currently exports OTel SDK metrics to Redis via a custom exporter, then renders Prometheus text on demand. After migration, the app pushes metrics via OTLP gRPC to the already-deployed OTel Collector, which exposes them on its Prometheus exporter (:4997). This eliminates the Redis middle layer, `reader.go`, `redis_exporter.go`, and the `/api/metrics` endpoint.

**Tech Stack:** Go, OTel SDK v1.42.0, `otlpmetricgrpc` exporter, gRPC, existing OTel Collector (DaemonSet)

---

### Task 1: Add `otlpmetricgrpc` dependency

**Files:**
- Modify: `backend/go.mod`

- [ ] **Step 1: Add the OTLP gRPC metric exporter dependency**

```bash
cd backend && go get go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc
```

- [ ] **Step 2: Tidy modules**

```bash
cd backend && go mod tidy
```

- [ ] **Step 3: Verify the dependency was added**

```bash
cd backend && grep otlpmetricgrpc go.mod
```

Expected: a line containing `go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc`

- [ ] **Step 4: Commit**

```bash
git add backend/go.mod backend/go.sum
git commit -m "chore: add otlpmetricgrpc dependency"
```

---

### Task 2: Update `OtlpConfig` to support OTLP endpoint

**Files:**
- Modify: `backend/internal/config/config.go`

- [ ] **Step 1: Add `Endpoint` and `Insecure` fields to `OtlpConfig`, remove `ExportIntervalMs`**

In `backend/internal/config/config.go`, replace the `OtlpConfig` struct:

```go
type OtlpConfig struct {
	Enabled        bool
	ServiceName    string
	Endpoint       string
	Insecure       bool
	MetricsEnabled bool
	TracesEnabled  bool
	LogsEnabled    bool
}
```

- [ ] **Step 2: Update `Load()` to read new env vars instead of `OTLP_EXPORT_INTERVAL_MS`**

In the `Load()` function, replace the `OTLP:` block:

```go
		OTLP: OtlpConfig{
			Enabled:        getEnvBool("OTLP_ENABLED", false),
			ServiceName:    getEnv("OTLP_SERVICE_NAME", "astron-claw"),
			Endpoint:       getEnv("OTLP_ENDPOINT", "localhost:4317"),
			Insecure:       getEnvBool("OTLP_INSECURE", true),
			MetricsEnabled: true,
			TracesEnabled:  false,
			LogsEnabled:    false,
		},
```

- [ ] **Step 3: Verify it compiles**

```bash
cd backend && go build ./internal/config/
```

Expected: no errors (other files that reference `ExportIntervalMs` will break — that's expected and fixed in Task 3)

- [ ] **Step 4: Commit**

```bash
git add backend/internal/config/config.go
git commit -m "feat: update OtlpConfig for OTLP gRPC endpoint"
```

---

### Task 3: Rewrite `provider.go` to use OTLP gRPC exporter

**Files:**
- Modify: `backend/internal/infra/telemetry/provider.go`

- [ ] **Step 1: Replace the entire `provider.go` with OTLP gRPC exporter**

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

// Init initializes OTel MeterProvider with OTLP gRPC exporter.
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

// Shutdown gracefully shuts down all providers.
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

- [ ] **Step 2: Verify it compiles**

```bash
cd backend && go build ./internal/infra/telemetry/
```

Expected: errors about `redis_exporter.go` and `reader.go` referencing removed Redis imports — that's expected, fixed in Task 4.

- [ ] **Step 3: Commit**

```bash
git add backend/internal/infra/telemetry/provider.go
git commit -m "feat: replace RedisMetricExporter with OTLP gRPC exporter"
```

---

### Task 4: Delete `redis_exporter.go` and `reader.go`

**Files:**
- Delete: `backend/internal/infra/telemetry/redis_exporter.go`
- Delete: `backend/internal/infra/telemetry/reader.go`

- [ ] **Step 1: Delete the Redis exporter**

```bash
rm backend/internal/infra/telemetry/redis_exporter.go
```

- [ ] **Step 2: Delete the Prometheus reader/renderer**

```bash
rm backend/internal/infra/telemetry/reader.go
```

- [ ] **Step 3: Verify the telemetry package compiles**

```bash
cd backend && go build ./internal/infra/telemetry/
```

Expected: success (no more references to Redis in this package)

- [ ] **Step 4: Run existing tests**

```bash
cd backend && go test ./internal/infra/telemetry/ -v
```

Expected: `TestEnsureInstruments`, `TestTokenPrefix`, `TestBucketBoundaries` all PASS

- [ ] **Step 5: Commit**

```bash
git add -A backend/internal/infra/telemetry/
git commit -m "refactor: remove RedisMetricExporter and Prometheus reader"
```

---

### Task 5: Delete `/api/metrics` endpoint and route

**Files:**
- Delete: `backend/internal/router/metrics.go`
- Modify: `backend/internal/router/router.go`

- [ ] **Step 1: Delete the metrics handler file**

```bash
rm backend/internal/router/metrics.go
```

- [ ] **Step 2: Remove metrics routes from `router.go`**

In `backend/internal/router/router.go`, remove these two lines from `SetupRouter`:

```go
	r.GET("/api/metrics", app.getMetrics)
	r.DELETE("/api/metrics", app.deleteMetrics)
```

- [ ] **Step 3: Remove the unused `telemetry` import from `router.go`**

In `backend/internal/router/router.go`, remove this import line (if it becomes unused — check whether any other file in the `router` package still imports it):

```go
	"astron-claw/backend/internal/infra/telemetry"
```

Also remove `"net/http"` and `"strings"` if they become unused in `router.go` (they were only used in `metrics.go`, which is a separate file, so `router.go` likely doesn't import them — verify before removing).

- [ ] **Step 4: Verify the router package compiles**

```bash
cd backend && go build ./internal/router/
```

Expected: success

- [ ] **Step 5: Commit**

```bash
git add -A backend/internal/router/
git commit -m "refactor: remove /api/metrics endpoint (metrics now via Collector)"
```

---

### Task 6: Update `main.go` — change `telemetry.Init` call signature

**Files:**
- Modify: `backend/cmd/server/main.go`

- [ ] **Step 1: Update the `telemetry.Init` call**

In `backend/cmd/server/main.go`, replace:

```go
	// Initialize OTLP telemetry
	if err := telemetry.Init(cfg.OTLP, rdb); err != nil {
		log.Fatal().Err(err).Msg("Failed to initialise OTLP telemetry")
	}
```

with:

```go
	// Initialize OTLP telemetry
	if err := telemetry.Init(ctx, cfg.OTLP); err != nil {
		log.Fatal().Err(err).Msg("Failed to initialise OTLP telemetry")
	}
```

- [ ] **Step 2: Verify the entire project compiles**

```bash
cd backend && go build ./...
```

Expected: success

- [ ] **Step 3: Run all tests**

```bash
cd backend && go test ./... -count=1
```

Expected: all tests pass

- [ ] **Step 4: Commit**

```bash
git add backend/cmd/server/main.go
git commit -m "feat: update main.go for new telemetry.Init signature"
```

---

### Task 7: Update `.env.example` with new config vars

**Files:**
- Modify: `backend/.env.example`

- [ ] **Step 1: Replace the OTLP section in `.env.example`**

Replace:

```env
# ── OTLP 遥测（可选）─────────────────────────────────
OTLP_ENABLED=false                   # 是否启用遥测指标
# OTLP_SERVICE_NAME=astron-claw      # 服务标识
# OTLP_EXPORT_INTERVAL_MS=10000      # 指标刷新到 Redis 的周期（毫秒）
```

with:

```env
# ── OTLP 遥测（可选）─────────────────────────────────
OTLP_ENABLED=false                   # 是否启用遥测指标
# OTLP_SERVICE_NAME=astron-claw      # 服务标识
# OTLP_ENDPOINT=localhost:4317       # OTel Collector gRPC 地址
# OTLP_INSECURE=true                 # 是否禁用 TLS（集群内部通信）
```

- [ ] **Step 2: Commit**

```bash
git add backend/.env.example
git commit -m "docs: update .env.example for OTLP gRPC config"
```

---

### Task 8: Final verification

- [ ] **Step 1: Full build**

```bash
cd backend && go build ./...
```

Expected: success

- [ ] **Step 2: Full test suite**

```bash
cd backend && go test ./... -count=1 -v
```

Expected: all tests pass

- [ ] **Step 3: Verify deleted files are gone**

```bash
ls backend/internal/infra/telemetry/redis_exporter.go 2>&1
ls backend/internal/infra/telemetry/reader.go 2>&1
ls backend/internal/router/metrics.go 2>&1
```

Expected: all three report "No such file or directory"

- [ ] **Step 4: Verify no stale references remain**

```bash
cd backend && grep -r "RedisMetricExporter\|RenderPrometheusExposition\|ResetAllMetrics\|KeyCounters\|KeyHistograms\|KeyGaugePIDs\|KeyMeta\|KeyResource\|GaugeKey\|ExportIntervalMs" --include="*.go" .
```

Expected: no matches

- [ ] **Step 5: Squash or finalize commits**

```bash
git log --oneline -8
```

Review the commit history and confirm all changes are clean.
