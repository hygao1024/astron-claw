package service

import (
	"strconv"
	"time"

	"github.com/rs/zerolog/log"
)

func (b *ConnectionBridge) trackReconcileToken(token string) {
	b.reconcileMu.Lock()
	defer b.reconcileMu.Unlock()

	if _, exists := b.reconcileTokenPos[token]; exists {
		return
	}

	b.reconcileTokenPos[token] = len(b.reconcileTokens)
	b.reconcileTokens = append(b.reconcileTokens, token)
}

func (b *ConnectionBridge) untrackReconcileToken(token string) {
	b.reconcileMu.Lock()
	defer b.reconcileMu.Unlock()

	idx, exists := b.reconcileTokenPos[token]
	if !exists {
		return
	}

	lastIdx := len(b.reconcileTokens) - 1
	lastToken := b.reconcileTokens[lastIdx]
	b.reconcileTokens[idx] = lastToken
	b.reconcileTokenPos[lastToken] = idx
	b.reconcileTokens = b.reconcileTokens[:lastIdx]
	delete(b.reconcileTokenPos, token)

	if len(b.reconcileTokens) == 0 {
		b.reconcileCursor = 0
		return
	}
	if b.reconcileCursor > idx && b.reconcileCursor > 0 {
		b.reconcileCursor--
	}
	if b.reconcileCursor >= len(b.reconcileTokens) {
		b.reconcileCursor = 0
	}
}

func (b *ConnectionBridge) nextReconcileBatch() []string {
	b.reconcileMu.Lock()
	defer b.reconcileMu.Unlock()

	if b.reconcileBatchSize <= 0 || len(b.reconcileTokens) == 0 {
		return nil
	}

	limit := b.reconcileBatchSize
	if limit > len(b.reconcileTokens) {
		limit = len(b.reconcileTokens)
	}

	out := make([]string, 0, limit)
	for i := 0; i < limit; i++ {
		if len(b.reconcileTokens) == 0 {
			break
		}
		if b.reconcileCursor >= len(b.reconcileTokens) {
			b.reconcileCursor = 0
		}
		out = append(out, b.reconcileTokens[b.reconcileCursor])
		b.reconcileCursor++
	}

	return out
}

func (b *ConnectionBridge) doReconcile() {
	ctx := b.ctx
	tokens := b.nextReconcileBatch()
	for _, token := range tokens {
		owner, err := b.GetBotOwner(ctx, token)
		if err == nil && owner != "" && owner != b.workerID {
			log.Info().Str("worker", b.workerID).Str("token", token).
				Str("remote_owner", owner).Msg("Reconcile evicted: owner changed")
			b.evictLocal(token)
			continue
		}

		localGenI, ok := b.botGens.Load(token)
		if !ok {
			continue
		}
		localGen := localGenI.(int64)

		remoteGenRaw, err := b.rdb.Get(ctx, BotGenPrefix+token).Result()
		if err != nil {
			continue
		}
		remoteGen, err := strconv.ParseInt(remoteGenRaw, 10, 64)
		if err != nil {
			continue
		}
		if remoteGen > localGen {
			log.Info().Int64("remote_gen", remoteGen).Int64("local_gen", localGen).
				Str("worker", b.workerID).Str("token", token).
				Msg("Reconcile evicted: newer generation exists")
			b.evictLocal(token)
		}
	}
}

func (b *ConnectionBridge) localBotCount() int {
	count := 0
	b.bots.Range(func(_, _ interface{}) bool {
		count++
		return true
	})
	return count
}

func (b *ConnectionBridge) waitUntilLocalBotCount(target int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if b.localBotCount() == target {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return b.localBotCount() == target
}
