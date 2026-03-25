package telemetry

import (
	"context"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

type fakePipeline struct {
	redis.Pipeliner
	hsetCalls []fakeHSetCall
	saddCalls []fakeSAddCall
}

type fakeHSetCall struct {
	key    string
	values []interface{}
}

type fakeSAddCall struct {
	key     string
	members []interface{}
}

func (p *fakePipeline) HSet(ctx context.Context, key string, values ...interface{}) *redis.IntCmd {
	p.hsetCalls = append(p.hsetCalls, fakeHSetCall{key: key, values: values})
	return redis.NewIntResult(1, nil)
}

func (p *fakePipeline) PExpire(ctx context.Context, key string, expiration time.Duration) *redis.BoolCmd {
	return redis.NewBoolResult(true, nil)
}

func (p *fakePipeline) SAdd(ctx context.Context, key string, members ...interface{}) *redis.IntCmd {
	p.saddCalls = append(p.saddCalls, fakeSAddCall{key: key, members: members})
	return redis.NewIntResult(int64(len(members)), nil)
}

func (p *fakePipeline) Exec(ctx context.Context) ([]redis.Cmder, error) {
	return nil, nil
}

type fakeTelemetryRedis struct {
	redis.UniversalClient
	pipe *fakePipeline
}

func (r *fakeTelemetryRedis) Pipeline() redis.Pipeliner {
	return r.pipe
}

func TestWorkerIDIncludesHostnameAndPID(t *testing.T) {
	host, err := os.Hostname()
	if err != nil {
		t.Fatalf("hostname: %v", err)
	}

	wid := workerID()
	pid := strconv.Itoa(os.Getpid())

	if wid == pid {
		t.Fatalf("workerID should not fall back to pid only: %q", wid)
	}
	if !strings.HasPrefix(wid, host+":") {
		t.Fatalf("workerID should start with hostname, got %q want prefix %q", wid, host+":")
	}
	if !strings.HasSuffix(wid, ":"+pid) && wid != host+":"+pid {
		t.Fatalf("workerID should end with pid, got %q want suffix %q", wid, ":"+pid)
	}
}

func TestExportUsesWorkerIDForGaugeIsolation(t *testing.T) {
	pipe := &fakePipeline{}
	exporter := NewRedisMetricExporter(&fakeTelemetryRedis{pipe: pipe}, "astron-claw", 1000)

	rm := &metricdata.ResourceMetrics{
		ScopeMetrics: []metricdata.ScopeMetrics{
			{
				Metrics: []metricdata.Metrics{
					{
						Name: "chat.active_streams",
						Data: metricdata.Sum[int64]{
							IsMonotonic: false,
							DataPoints: []metricdata.DataPoint[int64]{
								{
									Attributes: attribute.NewSet(attribute.String("route", "/bridge/chat")),
									Value:      7,
								},
							},
						},
					},
				},
			},
		},
	}

	if err := exporter.Export(context.Background(), rm); err != nil {
		t.Fatalf("export failed: %v", err)
	}

	var sawGaugeKey bool
	for _, call := range pipe.hsetCalls {
		if call.key == GaugeKey(exporter.workerID) {
			sawGaugeKey = true
			break
		}
	}
	if !sawGaugeKey {
		t.Fatalf("expected gauge HSET for worker key %q, got %#v", GaugeKey(exporter.workerID), pipe.hsetCalls)
	}

	if len(pipe.saddCalls) == 0 {
		t.Fatalf("expected SADD call for gauge worker set")
	}
	member, ok := pipe.saddCalls[0].members[0].(string)
	if !ok {
		t.Fatalf("expected string worker member, got %#v", pipe.saddCalls[0].members)
	}
	if member != exporter.workerID {
		t.Fatalf("expected workerID member %q, got %q", exporter.workerID, member)
	}
	if member == strconv.Itoa(os.Getpid()) {
		t.Fatalf("expected cluster-unique workerID, got pid-only member %q", member)
	}
}
