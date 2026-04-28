package service

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
)

const (
	tickInterval = 1 * time.Second // 每1秒检查一次
	botTTL       = 30.0             // bot 心跳 TTL（秒）
)

// BotStatusMonitor 监控 bot 连接状态并通知 SSE 层
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
	DisconnectC chan struct{}
}

// NewBotStatusMonitor 创建新的 bot 状态监控器
func NewBotStatusMonitor(rdb redis.UniversalClient) *BotStatusMonitor {
	ctx, cancel := context.WithCancel(context.Background())
	return &BotStatusMonitor{
		rdb:    rdb,
		ctx:    ctx,
		cancel: cancel,
	}
}

// Start 启动监控器
func (m *BotStatusMonitor) Start() {
	m.wg.Add(1)
	go m.run()
	log.Info().Msg("Bot status monitor started")
}

// Stop 停止监控器
func (m *BotStatusMonitor) Stop() {
	m.cancel()
	m.wg.Wait()
	log.Info().Msg("Bot status monitor stopped")
}

// RegisterSSEConnection 注册 SSE 连接
func (m *BotStatusMonitor) RegisterSSEConnection(token, sessionID, inboxKey string) *SSEConnection {
	conn := &SSEConnection{
		Token:       token,
		SessionID:   sessionID,
		InboxKey:    inboxKey,
		DisconnectC: make(chan struct{}, 1),
	}

	m.sseConnections.Store(inboxKey, conn)

	log.Debug().
		Str("token", token).
		Str("session", sessionID).
		Msg("SSE connection registered in monitor")

	return conn
}

// UnregisterSSEConnection 注销 SSE 连接
func (m *BotStatusMonitor) UnregisterSSEConnection(inboxKey string) {
	m.sseConnections.Delete(inboxKey)

	// inboxKey 格式: bridge:chat_inbox:token:sessionID
	parts := strings.SplitN(inboxKey, ":", 4)
	if len(parts) >= 4 {
		token := parts[2]
		sessionID := parts[3]
		log.Debug().
			Str("token", token).
			Str("session", sessionID).
			Msg("SSE connection unregistered from monitor")
	}
}

// GetSSEConnection 获取 SSE 连接
func (m *BotStatusMonitor) GetSSEConnection(inboxKey string) (*SSEConnection, bool) {
	if conn, ok := m.sseConnections.Load(inboxKey); ok {
		return conn.(*SSEConnection), true
	}
	return nil, false
}

// run 每1秒检查所有 SSE 连接对应的 bot 状态
func (m *BotStatusMonitor) run() {
	defer m.wg.Done()

	ticker := time.NewTicker(tickInterval)
	defer ticker.Stop()

	for {
		select {
		case <-m.ctx.Done():
			return
		case <-ticker.C:
			m.checkAllBots()
		}
	}
}

// checkAllBots 检查所有当前存活的 SSE 连接对应的 bot
func (m *BotStatusMonitor) checkAllBots() {
	// 收集所有唯一的 token
	tokens := make(map[string]bool)
	m.sseConnections.Range(func(key, value interface{}) bool {
		conn := value.(*SSEConnection)
		tokens[conn.Token] = true
		return true
	})

	if len(tokens) == 0 {
		return
	}

	// Pipeline 批量查询所有 token 的 bot 状态
	ctx, cancel := context.WithTimeout(m.ctx, 5*time.Second)
	defer cancel()

	tokenList := make([]string, 0, len(tokens))
	for token := range tokens {
		tokenList = append(tokenList, token)
	}

	pipe := m.rdb.Pipeline()
	cmds := make([]*redis.FloatCmd, len(tokenList))
	for i, token := range tokenList {
		cmds[i] = pipe.ZScore(ctx, BotAliveKey, token)
	}
	_, err := pipe.Exec(ctx)
	if err != nil && err != redis.Nil {
		log.Warn().Err(err).Msg("Failed to pipeline check bot status")
	}

	now := float64(time.Now().Unix())
	for i, cmd := range cmds {
		score, err := cmd.Result()
		if err == redis.Nil {
			// token 不存在于 ZSET，bot 确实断开
			m.notifyBotDisconnected(tokenList[i])
		} else if err != nil {
			// Redis 错误，跳过本次检查，避免误判
			log.Warn().Err(err).
				Str("token", tokenList[i]).
				Msg("Failed to check bot status: Redis error, skipping")
			continue
		} else if (now - score) >= botTTL {
			// 心跳超时，bot 断开
			m.notifyBotDisconnected(tokenList[i])
		}
	}
}

// notifyBotDisconnected 通知所有相关的 SSE 连接 bot 已断开
func (m *BotStatusMonitor) notifyBotDisconnected(token string) {
	log.Info().
		Str("token", token).
		Msg("Bot disconnected detected by monitor")

	m.sseConnections.Range(func(key, value interface{}) bool {
		conn := value.(*SSEConnection)
		if conn.Token == token {
			// 非阻塞通知
			select {
			case conn.DisconnectC <- struct{}{}:
				log.Debug().
					Str("token", token).
					Str("session", conn.SessionID).
					Msg("Notified SSE connection of bot disconnect")
			default:
				// channel 已满或已关闭，跳过
			}
		}
		return true
	})
}
