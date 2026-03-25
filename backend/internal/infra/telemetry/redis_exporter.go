package telemetry

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// Redis key constants (using {otlp} hash tag for Cluster slot co-location)
const (
	KeyCounters   = "{otlp}:counters"
	KeyHistograms = "{otlp}:histograms"
	KeyGaugePIDs  = "{otlp}:gauge_pids"
	KeyMeta       = "{otlp}:meta"
	KeyResource   = "{otlp}:resource"
)

// GaugeKey returns the Redis key for a worker's gauge data.
func GaugeKey(workerID string) string {
	return fmt.Sprintf("{otlp}:gauges:%s", workerID)
}

func workerID() string {
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "unknown"
	}
	return host + ":" + strconv.Itoa(os.Getpid())
}

func attrsKey(name string, attrs map[string]string) string {
	sortedJSON, _ := json.Marshal(attrs)
	return name + "|" + string(sortedJSON)
}

func attrSetToMap(set attribute.Set) map[string]string {
	m := make(map[string]string)
	iter := set.Iter()
	for iter.Next() {
		kv := iter.Attribute()
		m[string(kv.Key)] = kv.Value.Emit()
	}
	return m
}

// RedisMetricExporter exports OTel metrics to Redis.
type RedisMetricExporter struct {
	rdb         redis.UniversalClient
	workerID    string
	serviceName string
	gaugeTTL    time.Duration
}

// NewRedisMetricExporter creates a new RedisMetricExporter.
func NewRedisMetricExporter(rdb redis.UniversalClient, serviceName string, exportIntervalMs int) *RedisMetricExporter {
	return &RedisMetricExporter{
		rdb:         rdb,
		workerID:    workerID(),
		serviceName: serviceName,
		gaugeTTL:    time.Duration(exportIntervalMs*3) * time.Millisecond,
	}
}

// Temporality returns Delta for Counter/Histogram, Cumulative for UpDownCounter.
func (e *RedisMetricExporter) Temporality(kind sdkmetric.InstrumentKind) metricdata.Temporality {
	switch kind {
	case sdkmetric.InstrumentKindCounter, sdkmetric.InstrumentKindHistogram:
		return metricdata.DeltaTemporality
	default:
		return metricdata.CumulativeTemporality
	}
}

// Aggregation returns the default aggregation for the given instrument kind.
func (e *RedisMetricExporter) Aggregation(kind sdkmetric.InstrumentKind) sdkmetric.Aggregation {
	return sdkmetric.DefaultAggregationSelector(kind)
}

// Export writes metrics to Redis.
func (e *RedisMetricExporter) Export(ctx context.Context, rm *metricdata.ResourceMetrics) error {
	pipe := e.rdb.Pipeline()
	hasGauge := false
	gaugeFields := map[string]interface{}{}

	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			name := m.Name
			meta := e.buildMeta(m)
			if meta != nil {
				metaJSON, _ := json.Marshal(meta)
				pipe.HSet(ctx, KeyMeta, name, string(metaJSON))
			}

			switch data := m.Data.(type) {
			case metricdata.Sum[int64]:
				if data.IsMonotonic {
					for _, dp := range data.DataPoints {
						attrs := attrSetToMap(dp.Attributes)
						field := attrsKey(name, attrs)
						pipe.HIncrByFloat(ctx, KeyCounters, field, float64(dp.Value))
					}
				} else {
					hasGauge = true
					for _, dp := range data.DataPoints {
						attrs := attrSetToMap(dp.Attributes)
						field := attrsKey(name, attrs)
						gaugeFields[field] = strconv.FormatInt(dp.Value, 10)
					}
				}
			case metricdata.Sum[float64]:
				if data.IsMonotonic {
					for _, dp := range data.DataPoints {
						attrs := attrSetToMap(dp.Attributes)
						field := attrsKey(name, attrs)
						pipe.HIncrByFloat(ctx, KeyCounters, field, dp.Value)
					}
				} else {
					hasGauge = true
					for _, dp := range data.DataPoints {
						attrs := attrSetToMap(dp.Attributes)
						field := attrsKey(name, attrs)
						gaugeFields[field] = strconv.FormatFloat(dp.Value, 'f', -1, 64)
					}
				}
			case metricdata.Histogram[float64]:
				for _, dp := range data.DataPoints {
					attrs := attrSetToMap(dp.Attributes)
					base := attrsKey(name, attrs)
					pipe.HIncrByFloat(ctx, KeyHistograms, base+"|count", float64(dp.Count))
					pipe.HIncrByFloat(ctx, KeyHistograms, base+"|sum", dp.Sum)
					for i, bound := range dp.Bounds {
						pipe.HIncrByFloat(ctx, KeyHistograms,
							fmt.Sprintf("%s|bucket_%v", base, bound),
							float64(dp.BucketCounts[i]))
					}
					// +Inf bucket
					pipe.HIncrByFloat(ctx, KeyHistograms,
						base+"|bucket_+Inf",
						float64(dp.BucketCounts[len(dp.BucketCounts)-1]))
				}
			}
		}
	}

	// Write gauge data
	if hasGauge && len(gaugeFields) > 0 {
		gk := GaugeKey(e.workerID)
		pipe.HSet(ctx, gk, gaugeFields)
		pipe.PExpire(ctx, gk, e.gaugeTTL)
		pipe.SAdd(ctx, KeyGaugePIDs, e.workerID)
	}

	// Write resource info
	pipe.HSet(ctx, KeyResource, "service.name", e.serviceName)

	_, err := pipe.Exec(ctx)
	if err != nil {
		log.Warn().Err(err).Msg("RedisMetricExporter: export failed")
	}
	return err
}

// ForceFlush is a no-op.
func (e *RedisMetricExporter) ForceFlush(ctx context.Context) error {
	return nil
}

// Shutdown is a no-op since we share the Redis client.
func (e *RedisMetricExporter) Shutdown(ctx context.Context) error {
	return nil
}

func (e *RedisMetricExporter) buildMeta(m metricdata.Metrics) map[string]string {
	meta := map[string]string{
		"description": m.Description,
		"unit":        m.Unit,
	}
	switch data := m.Data.(type) {
	case metricdata.Sum[int64]:
		if data.IsMonotonic {
			meta["type"] = "counter"
		} else {
			meta["type"] = "up_down_counter"
		}
	case metricdata.Sum[float64]:
		if data.IsMonotonic {
			meta["type"] = "counter"
		} else {
			meta["type"] = "up_down_counter"
		}
	case metricdata.Histogram[float64]:
		meta["type"] = "histogram"
	default:
		return nil
	}
	return meta
}
