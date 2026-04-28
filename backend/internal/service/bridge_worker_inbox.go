package service

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"

	"github.com/rs/zerolog/log"
)

type workerInboxMessage struct {
	Token      string      `json:"token"`
	RPCRequest interface{} `json:"rpc_request,omitempty"`
	Disconnect bool        `json:"disconnect,omitempty"`
}

func (b *ConnectionBridge) workerInboxName(workerID string) string {
	return WorkerInboxPrefix + workerID
}

func (b *ConnectionBridge) PublishToWorkerInbox(ctx context.Context, workerID, token string, rpcRequest interface{}) error {
	payload, err := json.Marshal(workerInboxMessage{
		Token:      token,
		RPCRequest: rpcRequest,
	})
	if err != nil {
		return fmt.Errorf("marshal worker inbox message: %w", err)
	}

	if _, err := b.queue.Publish(ctx, b.workerInboxName(workerID), string(payload)); err != nil {
		return fmt.Errorf("publish worker inbox: %w", err)
	}
	return nil
}

func (b *ConnectionBridge) PublishDisconnectToWorkerInbox(ctx context.Context, workerID, token string) error {
	payload, err := json.Marshal(workerInboxMessage{
		Token:      token,
		Disconnect: true,
	})
	if err != nil {
		return fmt.Errorf("marshal worker inbox disconnect: %w", err)
	}

	if _, err := b.queue.Publish(ctx, b.workerInboxName(workerID), string(payload)); err != nil {
		return fmt.Errorf("publish worker inbox disconnect: %w", err)
	}
	return nil
}

func (b *ConnectionBridge) startWorkerInboxConsumers() {
	inbox := b.workerInboxName(b.workerID)
	if err := b.queue.EnsureGroup(b.ctx, inbox, WorkerInboxGroup); err != nil {
		log.Error().Err(err).Str("worker", b.workerID).Msg("Failed to ensure worker inbox group")
	}

	for i := 0; i < b.workerInboxConsumers; i++ {
		consumerID := b.workerID + "-" + strconv.Itoa(i)
		go b.runWorkerInboxConsumer(inbox, consumerID)
	}
}

func (b *ConnectionBridge) runWorkerInboxConsumer(inbox, consumerID string) {
	defer b.wg.Done()

	for {
		select {
		case <-b.ctx.Done():
			return
		default:
		}

		if b.shuttingDown.Load() {
			return
		}

		result, err := b.queue.Consume(b.ctx, inbox, WorkerInboxGroup, consumerID, ConsumeBlockMs)
		if err != nil {
			if b.shuttingDown.Load() || b.ctx.Err() != nil {
				return
			}
			log.Error().Err(err).Str("worker", b.workerID).Str("consumer", consumerID).
				Msg("Worker inbox consume error")
			continue
		}
		if result == nil {
			continue
		}

		var msg workerInboxMessage
		if err := json.Unmarshal([]byte(result.Data), &msg); err != nil {
			_ = b.queue.Ack(b.ctx, inbox, WorkerInboxGroup, result.ID)
			log.Warn().Err(err).Str("worker", b.workerID).Msg("Invalid worker inbox payload")
			continue
		}

		_ = b.queue.Ack(b.ctx, inbox, WorkerInboxGroup, result.ID)

		if msg.Token == "" {
			log.Warn().Str("worker", b.workerID).Msg("Worker inbox message missing token")
			continue
		}

		if msg.Disconnect {
			b.evictLocal(msg.Token)
			continue
		}

		connI, ok := b.bots.Load(msg.Token)
		if !ok {
			log.Warn().Str("worker", b.workerID).Str("token", msg.Token).
				Msg("Worker inbox token not found locally")
			continue
		}

		conn := connI.(*BotConn)
		if msg.RPCRequest != nil {
			if err := conn.WriteJSON(msg.RPCRequest); err != nil {
				log.Warn().Err(err).Str("worker", b.workerID).Str("token", msg.Token).
					Msg("Failed to forward worker inbox payload")
			}
		}
	}
}
