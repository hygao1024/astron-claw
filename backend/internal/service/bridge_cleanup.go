package service

import (
	"context"
	"time"

	"github.com/rs/zerolog/log"
)

func (b *ConnectionBridge) startArtifactCleanupWorkers() {
	for i := 0; i < b.artifactCleanupWorkers; i++ {
		go b.runArtifactCleanupWorker()
	}
}

func (b *ConnectionBridge) runArtifactCleanupWorker() {
	defer b.wg.Done()

	for {
		select {
		case <-b.ctx.Done():
			return
		case token := <-b.artifactCleanupCh:
			b.doArtifactCleanup(token)
		}
	}
}

func (b *ConnectionBridge) enqueueArtifactCleanup(token string) {
	select {
	case b.artifactCleanupCh <- token:
	default:
		go b.doArtifactCleanup(token)
	}
}

func (b *ConnectionBridge) doArtifactCleanup(token string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := b.queue.DeleteQueue(ctx, BotInboxPrefix+token); err != nil {
		log.Warn().Err(err).Str("token", token).Msg("Failed to delete legacy bot inbox")
	}
	b.cleanupChatInboxes(ctx, token)
}
