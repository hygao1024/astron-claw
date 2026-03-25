package service

import (
	"context"
	"github.com/redis/go-redis/v9"
	"testing"
)

type fakeBridgeRedis struct {
	redis.UniversalClient
	incrValues map[string]int64
	getValues  map[string]string
	zremCalls  []string
	delCalls   []string
}

func newFakeBridgeRedis() *fakeBridgeRedis {
	return &fakeBridgeRedis{
		incrValues: make(map[string]int64),
		getValues:  make(map[string]string),
	}
}

func (r *fakeBridgeRedis) Incr(ctx context.Context, key string) *redis.IntCmd {
	r.incrValues[key]++
	return redis.NewIntResult(r.incrValues[key], nil)
}

func (r *fakeBridgeRedis) ZAdd(ctx context.Context, key string, members ...redis.Z) *redis.IntCmd {
	return redis.NewIntResult(int64(len(members)), nil)
}

func (r *fakeBridgeRedis) Get(ctx context.Context, key string) *redis.StringCmd {
	val, ok := r.getValues[key]
	if !ok {
		return redis.NewStringResult("", redis.Nil)
	}
	return redis.NewStringResult(val, nil)
}

func (r *fakeBridgeRedis) ZRem(ctx context.Context, key string, members ...interface{}) *redis.IntCmd {
	for _, member := range members {
		if token, ok := member.(string); ok {
			r.zremCalls = append(r.zremCalls, token)
		}
	}
	return redis.NewIntResult(int64(len(members)), nil)
}

func (r *fakeBridgeRedis) SMembers(ctx context.Context, key string) *redis.StringSliceCmd {
	return redis.NewStringSliceResult(nil, nil)
}

func (r *fakeBridgeRedis) Del(ctx context.Context, keys ...string) *redis.IntCmd {
	r.delCalls = append(r.delCalls, keys...)
	return redis.NewIntResult(int64(len(keys)), nil)
}

type fakeBridgeQueue struct {
	deleteQueueCalls []string
}

func (q *fakeBridgeQueue) Publish(ctx context.Context, queueName, message string) (string, error) {
	return "", nil
}

func (q *fakeBridgeQueue) Consume(ctx context.Context, queueName, group, consumer string, blockMs int) (*QueueMessage, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func (q *fakeBridgeQueue) Ack(ctx context.Context, queueName, group, messageID string) error {
	return nil
}

func (q *fakeBridgeQueue) DeleteMessage(ctx context.Context, queueName, messageID string) error {
	return nil
}

func (q *fakeBridgeQueue) DeleteQueue(ctx context.Context, queueName string) error {
	q.deleteQueueCalls = append(q.deleteQueueCalls, queueName)
	return nil
}

func (q *fakeBridgeQueue) Purge(ctx context.Context, queueName string) error {
	return nil
}

func (q *fakeBridgeQueue) EnsureGroup(ctx context.Context, queueName, group string) error {
	return nil
}

func TestBeginDrainMarksBridgeUnavailable(t *testing.T) {
	bridge := NewConnectionBridge(newFakeBridgeRedis(), nil, &fakeBridgeQueue{})

	if bridge.IsDraining() {
		t.Fatal("bridge should not start in draining state")
	}

	bridge.BeginDrain()

	if !bridge.IsDraining() {
		t.Fatal("bridge should be draining after BeginDrain")
	}
}

func TestRegisterBotRejectsWhenDraining(t *testing.T) {
	bridge := NewConnectionBridge(newFakeBridgeRedis(), nil, &fakeBridgeQueue{})
	bridge.BeginDrain()

	err := bridge.RegisterBot(context.Background(), "tok-1", &BotConn{})
	if err == nil {
		t.Fatal("expected RegisterBot to reject new connections while draining")
	}
}

func TestShutdownSkipsRedisCleanupForNewerOwner(t *testing.T) {
	rdb := newFakeBridgeRedis()
	queue := &fakeBridgeQueue{}
	bridge := NewConnectionBridge(rdb, nil, queue)

	if err := bridge.RegisterBot(context.Background(), "tok-1", &BotConn{Token: "tok-1"}); err != nil {
		t.Fatalf("register bot: %v", err)
	}
	rdb.getValues[BotGenPrefix+"tok-1"] = "2"

	bridge.Shutdown()

	if !bridge.IsDraining() {
		t.Fatal("shutdown should mark bridge as draining")
	}
	if !bridge.shuttingDown.Load() {
		t.Fatal("shutdown should mark bridge as shutting down")
	}
	if containsString(queue.deleteQueueCalls, BotInboxPrefix+"tok-1") {
		t.Fatalf("shutdown should not delete queue for newer owner, calls=%v", queue.deleteQueueCalls)
	}
	if containsString(rdb.zremCalls, "tok-1") {
		t.Fatalf("shutdown should not zrem newer owner's token, calls=%v", rdb.zremCalls)
	}
}

func TestShutdownCleansRedisForCurrentOwner(t *testing.T) {
	rdb := newFakeBridgeRedis()
	queue := &fakeBridgeQueue{}
	bridge := NewConnectionBridge(rdb, nil, queue)

	if err := bridge.RegisterBot(context.Background(), "tok-1", &BotConn{Token: "tok-1"}); err != nil {
		t.Fatalf("register bot: %v", err)
	}
	rdb.getValues[BotGenPrefix+"tok-1"] = "1"

	bridge.Shutdown()

	if !containsString(queue.deleteQueueCalls, BotInboxPrefix+"tok-1") {
		t.Fatalf("expected queue cleanup for current owner, calls=%v", queue.deleteQueueCalls)
	}
	if !containsString(rdb.zremCalls, "tok-1") {
		t.Fatalf("expected zrem for current owner, calls=%v", rdb.zremCalls)
	}
}

func containsString(items []string, target string) bool {
	for _, item := range items {
		if item == target {
			return true
		}
	}
	return false
}
