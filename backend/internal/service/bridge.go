package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"astron-claw/backend/internal/model"
)

var bridgeTracer = otel.Tracer("astron-claw/service/bridge")

const (
	BotAliveKey        = "bridge:bot_alive"
	BotInboxPrefix     = "bridge:bot_inbox:"
	BotOwnerPrefix     = "bridge:bot_owner:"
	ChatInboxPrefix    = "bridge:chat_inbox:"
	ChatInboxIdxPrefix = "bridge:chat_inbox_idx:"
	CleanupLockKey     = "bridge:cleanup_lock"
	BotGenPrefix       = "bridge:bot_gen:"
	WorkerInboxPrefix  = "bridge:worker_inbox:"
	TraceCtxPrefix     = "bridge:trace_ctx:"

	BotTTL            = 30 * time.Second
	HeartbeatInterval = 10 * time.Second
	CleanupInterval   = 10 * time.Second
	CleanupLockTTL    = 2 * time.Minute
	ReconcileInterval = 5 * time.Second
	ConsumeBlockMs    = 5000 // keep as int for blockMs parameter
	WorkerInboxGroup  = "worker"

	DefaultWorkerInboxConsumers   = 4
	DefaultReconcileBatchSize     = 128
	DefaultArtifactCleanupWorkers = 2
)

var (
	extendCleanupLockScript = redis.NewScript(`
if redis.call("GET", KEYS[1]) == ARGV[1] then
	return redis.call("PEXPIRE", KEYS[1], ARGV[2])
end
return 0
`)
	releaseCleanupLockScript = redis.NewScript(`
if redis.call("GET", KEYS[1]) == ARGV[1] then
	return redis.call("DEL", KEYS[1])
end
return 0
`)
)

// BotConn wraps a websocket.Conn with a write mutex for thread safety.
type BotConn struct {
	Conn   *websocket.Conn
	mu     sync.Mutex
	Token  string
	closed sync.Once
}

// WriteJSON safely writes JSON to the WebSocket.
func (bc *BotConn) WriteJSON(v interface{}) error {
	bc.mu.Lock()
	defer bc.mu.Unlock()
	return bc.Conn.WriteJSON(v)
}

// WriteMessage safely writes a message to the WebSocket.
func (bc *BotConn) WriteMessage(messageType int, data []byte) error {
	bc.mu.Lock()
	defer bc.mu.Unlock()
	return bc.Conn.WriteMessage(messageType, data)
}

// Close safely closes the WebSocket with a close code and reason.
func (bc *BotConn) Close(code int, reason string) error {
	var err error
	bc.closed.Do(func() {
		bc.mu.Lock()
		defer bc.mu.Unlock()
		msg := websocket.FormatCloseMessage(code, reason)
		_ = bc.Conn.WriteControl(websocket.CloseMessage, msg, time.Now().Add(5*time.Second))
		err = bc.Conn.Close()
	})
	return err
}

// ConnectionBridge manages bot-to-chat message routing.
type ConnectionBridge struct {
	workerID     string
	bots         sync.Map // token -> *BotConn
	botGens      sync.Map // token -> int64
	rdb          redis.UniversalClient
	sessionStore *SessionStore
	queue        MessageQueue
	shuttingDown atomic.Bool
	regMu        sync.Mutex
	wg           sync.WaitGroup
	ctx          context.Context
	cancel       context.CancelFunc

	heartbeatInterval      time.Duration
	cleanupInterval        time.Duration
	cleanupLockTTL         time.Duration
	reconcileInterval      time.Duration
	reconcileBatchSize     int
	workerInboxConsumers   int
	artifactCleanupWorkers int

	reconcileMu       sync.Mutex
	reconcileTokens   []string
	reconcileTokenPos map[string]int
	reconcileCursor   int

	artifactCleanupCh chan string
}

// NewConnectionBridge creates a new ConnectionBridge.
func NewConnectionBridge(rdb redis.UniversalClient, sessionStore *SessionStore, queue MessageQueue) *ConnectionBridge {
	ctx, cancel := context.WithCancel(context.Background())
	return &ConnectionBridge{
		workerID:               uuid.New().String()[:12],
		rdb:                    rdb,
		sessionStore:           sessionStore,
		queue:                  queue,
		ctx:                    ctx,
		cancel:                 cancel,
		heartbeatInterval:      HeartbeatInterval,
		cleanupInterval:        CleanupInterval,
		cleanupLockTTL:         CleanupLockTTL,
		reconcileInterval:      ReconcileInterval,
		reconcileBatchSize:     DefaultReconcileBatchSize,
		workerInboxConsumers:   DefaultWorkerInboxConsumers,
		artifactCleanupWorkers: DefaultArtifactCleanupWorkers,
		reconcileTokenPos:      make(map[string]int),
		artifactCleanupCh:      make(chan string, 2048),
	}
}

// SetWorkerInboxConsumers overrides the default number of worker inbox consumers.
// Must be called before Start.
func (b *ConnectionBridge) SetWorkerInboxConsumers(n int) {
	if n > 0 {
		b.workerInboxConsumers = n
	}
}

// Start begins the heartbeat goroutine.
func (b *ConnectionBridge) Start() {
	b.wg.Add(3 + b.workerInboxConsumers + b.artifactCleanupWorkers)
	go b.runHeartbeat()
	go b.runCleanup()
	go b.runReconcile()
	b.startWorkerInboxConsumers()
	b.startArtifactCleanupWorkers()
	log.Info().Str("worker", b.workerID).Msg("Bridge worker started")
}

func (b *ConnectionBridge) runHeartbeat() {
	defer b.wg.Done()
	ticker := time.NewTicker(b.heartbeatInterval)
	defer ticker.Stop()

	for {
		select {
		case <-b.ctx.Done():
			return
		case <-ticker.C:
			if b.shuttingDown.Load() {
				return
			}
			b.doHeartbeat()
		}
	}
}

func (b *ConnectionBridge) runCleanup() {
	defer b.wg.Done()
	ticker := time.NewTicker(b.cleanupInterval)
	defer ticker.Stop()

	for {
		select {
		case <-b.ctx.Done():
			return
		case <-ticker.C:
			if b.shuttingDown.Load() {
				return
			}
			b.doCleanup()
		}
	}
}

func (b *ConnectionBridge) runReconcile() {
	defer b.wg.Done()
	ticker := time.NewTicker(b.reconcileInterval)
	defer ticker.Stop()

	for {
		select {
		case <-b.ctx.Done():
			return
		case <-ticker.C:
			if b.shuttingDown.Load() {
				return
			}
			b.doReconcile()
		}
	}
}

func (b *ConnectionBridge) doHeartbeat() {
	ctx := b.ctx
	now := time.Now().Unix()

	// Refresh ZSET scores
	mapping := make(map[string]float64)
	b.bots.Range(func(key, _ interface{}) bool {
		mapping[key.(string)] = float64(now)
		return true
	})
	if len(mapping) > 0 {
		members := make([]redis.Z, 0, len(mapping))
		for token, score := range mapping {
			members = append(members, redis.Z{Score: score, Member: token})
		}
		if err := b.rdb.ZAdd(ctx, BotAliveKey, members...).Err(); err != nil {
			log.Warn().Err(err).Msg("Failed to refresh bot heartbeat scores")
		}
	}
}

func (b *ConnectionBridge) doCleanup() {
	ctx := b.ctx
	now := time.Now().Unix()

	// Compete for cleanup lock
	acquired, _ := b.rdb.SetNX(ctx, CleanupLockKey, b.workerID, b.cleanupLockTTL).Result()
	if !acquired {
		return
	}

	stopRenew := make(chan struct{})
	renewDone := make(chan struct{})
	go b.renewCleanupLock(stopRenew, renewDone)
	defer func() {
		close(stopRenew)
		<-renewDone
		b.releaseCleanupLock()
	}()

	b.cleanupExpiredBots(ctx, float64(now))
}

func (b *ConnectionBridge) cleanupExpiredBots(ctx context.Context, now float64) {
	cutoff := now - BotTTL.Seconds()
	expired, _ := b.rdb.ZRangeByScore(ctx, BotAliveKey, &redis.ZRangeBy{
		Min: "-inf",
		Max: fmt.Sprintf("%f", cutoff),
	}).Result()
	if len(expired) == 0 {
		return
	}
	for _, tok := range expired {
		b.enqueueArtifactCleanup(tok)
		b.rdb.Del(ctx, BotOwnerPrefix+tok)
		b.rdb.Del(ctx, BotGenPrefix+tok)
	}
	b.rdb.ZRemRangeByScore(ctx, BotAliveKey, "-inf", fmt.Sprintf("%f", cutoff))
	log.Info().Int("count", len(expired)).Msg("Cleanup: removed expired bot(s)")
}

func (b *ConnectionBridge) renewCleanupLock(stop <-chan struct{}, done chan<- struct{}) {
	defer close(done)

	interval := b.cleanupLockTTL / 2
	if interval <= 0 {
		return
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-stop:
			return
		case <-b.ctx.Done():
			return
		case <-ticker.C:
			if err := extendCleanupLockScript.Run(
				b.ctx,
				b.rdb,
				[]string{CleanupLockKey},
				b.workerID,
				b.cleanupLockTTL.Milliseconds(),
			).Err(); err != nil && err != redis.Nil {
				log.Warn().Err(err).Str("worker", b.workerID).Msg("Failed to renew cleanup lock")
			}
		}
	}
}

func (b *ConnectionBridge) releaseCleanupLock() {
	releaseCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if err := releaseCleanupLockScript.Run(
		releaseCtx,
		b.rdb,
		[]string{CleanupLockKey},
		b.workerID,
	).Err(); err != nil && err != redis.Nil {
		log.Warn().Err(err).Str("worker", b.workerID).Msg("Failed to release cleanup lock")
	}
}

func (b *ConnectionBridge) cleanupChatInboxes(ctx context.Context, token string) {
	idxKey := ChatInboxIdxPrefix + token
	keys, err := b.rdb.SMembers(ctx, idxKey).Result()
	if err != nil {
		log.Warn().Err(err).Str("token", token).Msg("Error reading chat inbox index")
		return
	}
	if len(keys) > 0 {
		log.Warn().Int("orphan_inboxes", len(keys)).Str("token", token).
			Msg("Cleaning up orphan chat inboxes")
	}
	for _, k := range keys {
		b.rdb.Del(ctx, k)
	}
	b.rdb.Del(ctx, idxKey)
}

// TrackChatInbox registers a chat inbox key in the per-token index Set.
func (b *ConnectionBridge) TrackChatInbox(ctx context.Context, token, inboxKey string) {
	b.rdb.SAdd(ctx, ChatInboxIdxPrefix+token, inboxKey)
}

// UntrackChatInbox removes a chat inbox key from the per-token index Set.
func (b *ConnectionBridge) UntrackChatInbox(ctx context.Context, token, inboxKey string) {
	b.rdb.SRem(ctx, ChatInboxIdxPrefix+token, inboxKey)
}

// RegisterBot registers a bot connection, evicting any existing holder.
func (b *ConnectionBridge) RegisterBot(ctx context.Context, token string, conn *BotConn) error {
	b.regMu.Lock()
	defer b.regMu.Unlock()

	if b.shuttingDown.Load() {
		return fmt.Errorf("bridge is shutting down")
	}

	// Same-worker eviction
	if _, loaded := b.bots.Load(token); loaded {
		b.evictLocal(token)
	}

	// Atomically claim a new generation
	gen, err := b.rdb.Incr(ctx, BotGenPrefix+token).Result()
	if err != nil {
		return fmt.Errorf("claim generation: %w", err)
	}

	// Update ZSET heartbeat
	b.rdb.ZAdd(ctx, BotAliveKey, redis.Z{Score: float64(time.Now().Unix()), Member: token})
	if err := b.SetBotOwner(ctx, token, b.workerID); err != nil {
		return fmt.Errorf("set bot owner: %w", err)
	}

	// Store locally
	b.bots.Store(token, conn)
	b.botGens.Store(token, gen)
	b.trackReconcileToken(token)

	log.Info().Str("worker", b.workerID).Int64("gen", gen).Str("token", token).
		Msg("Bot registered")
	return nil
}

func (b *ConnectionBridge) evictLocal(token string) {
	connI, loaded := b.bots.LoadAndDelete(token)
	b.botGens.Delete(token)
	b.untrackReconcileToken(token)

	if loaded && connI != nil {
		conn := connI.(*BotConn)
		_ = conn.Close(model.ErrWSEvicted.HTTPStatus, model.ErrWSEvicted.Message)
	}
	b.NotifyBotDisconnected(token)
	log.Info().Str("worker", b.workerID).Str("token", token).Msg("Evicted local bot")
}

// UnregisterBot removes a bot and conditionally cleans Redis.
func (b *ConnectionBridge) UnregisterBot(ctx context.Context, token string, conn *BotConn) {
	b.regMu.Lock()
	// Same-worker guard
	currentI, _ := b.bots.Load(token)
	if conn != nil && currentI != nil && currentI.(*BotConn) != conn {
		b.regMu.Unlock()
		return
	}

	localGenI, _ := b.botGens.Load(token)
	var localGen int64
	if localGenI != nil {
		localGen = localGenI.(int64)
	}

	// Local cleanup
	b.bots.Delete(token)
	b.botGens.Delete(token)
	b.untrackReconcileToken(token)
	b.NotifyBotDisconnected(token)
	b.regMu.Unlock()

	// Cross-worker guard
	if b.shouldSkipSharedCleanup(ctx, token, localGen, "unregister") {
		return
	}

	// Clean Redis state
	b.cleanupSharedBotState(ctx, token, true)
	log.Info().Str("worker", b.workerID).Str("token", token).Msg("Bot unregistered")
}

// RemoveBotSessions destroys session data for a token (admin delete).
func (b *ConnectionBridge) RemoveBotSessions(ctx context.Context, token string) error {
	if err := b.sessionStore.RemoveSessions(ctx, token); err != nil {
		return err
	}

	if connI, loaded := b.bots.Load(token); loaded {
		conn := connI.(*BotConn)
		_ = conn.Close(model.ErrWSTokenDeleted.HTTPStatus, model.ErrWSTokenDeleted.Message)
		b.UnregisterBot(ctx, token, nil)
	} else {
		route, err := b.ResolveBotRoute(ctx, token)
		if err == nil {
			_ = b.PublishDisconnectToWorkerInbox(ctx, route.WorkerID, token)
		}
		b.rdb.ZRem(ctx, BotAliveKey, token)
		b.enqueueArtifactCleanup(token)
		b.rdb.Del(ctx, BotOwnerPrefix+token)
		b.rdb.Del(ctx, BotGenPrefix+token)
	}
	log.Info().Str("token", token).Msg("Bot sessions fully removed")
	return nil
}

// IsBotConnected returns true if a bot's heartbeat is fresh.
func (b *ConnectionBridge) IsBotConnected(ctx context.Context, token string) bool {
	score, err := b.rdb.ZScore(ctx, BotAliveKey, token).Result()
	if err != nil {
		return false
	}
	return (float64(time.Now().Unix()) - score) < BotTTL.Seconds()
}

// GetConnectionsSummary returns per-token bot online status.
func (b *ConnectionBridge) GetConnectionsSummary(ctx context.Context) map[string]bool {
	cutoff := float64(time.Now().Unix()) - BotTTL.Seconds()
	// "(" prefix = exclusive, so score == cutoff is excluded (aligned with BotStatusMonitor's >= check)
	alive, _ := b.rdb.ZRangeByScore(ctx, BotAliveKey, &redis.ZRangeBy{
		Min: "(" + fmt.Sprintf("%f", cutoff),
		Max: "+inf",
	}).Result()
	result := make(map[string]bool, len(alive))
	for _, t := range alive {
		result[t] = true
	}
	return result
}

// CreateSession creates a new session.
func (b *ConnectionBridge) CreateSession(ctx context.Context, token string) (string, int, error) {
	sessionID := uuid.New().String()
	num, err := b.sessionStore.CreateSession(ctx, token, sessionID)
	if err != nil {
		return "", 0, err
	}
	log.Info().Str("session", sessionID).Str("token", token).Msg("Session created")
	return sessionID, num, nil
}

// GetSession returns session info if it belongs to token.
func (b *ConnectionBridge) GetSession(ctx context.Context, token, sessionID string) (string, int, bool) {
	return b.sessionStore.GetSession(ctx, token, sessionID)
}

// GetSessions returns all sessions for a token.
func (b *ConnectionBridge) GetSessions(ctx context.Context, token string) ([]SessionInfo, error) {
	return b.sessionStore.GetSessions(ctx, token)
}

// CleanupOldSessions removes sessions older than maxAgeDays.
func (b *ConnectionBridge) CleanupOldSessions(ctx context.Context, maxAgeDays float64) (int, error) {
	return b.sessionStore.CleanupOldSessions(ctx, maxAgeDays*86400)
}

// SendCancelToBot sends a session/cancel JSON-RPC notification to the bot inbox.
func (b *ConnectionBridge) SendCancelToBot(ctx context.Context, token, sessionID string) error {
	rpcRequest := map[string]interface{}{
		"jsonrpc": "2.0",
		"method":  "session/cancel",
		"params":  map[string]interface{}{"sessionId": sessionID},
	}
	route, err := b.ResolveBotRoute(ctx, token)
	if err != nil {
		log.Error().Err(err).Str("token", token).Msg("Failed to resolve bot route for cancel")
		return err
	}
	if err := b.PublishToWorkerInbox(ctx, route.WorkerID, token, rpcRequest); err != nil {
		log.Error().Err(err).Str("token", token).Msg("Failed to send cancel to bot")
		return err
	}
	log.Info().Str("session", sessionID).Str("token", token).
		Msg("Sent cancel to bot")
	return nil
}

// SendToBot creates a JSON-RPC request and sends it to the bot inbox.
func (b *ConnectionBridge) SendToBot(ctx context.Context, token, userMessage string, mediaURLs []string, sessionID string) (string, error) {
	if sessionID == "" {
		log.Error().Str("token", token).Msg("send_to_bot called without session_id")
		return "", fmt.Errorf("missing session_id")
	}

	requestID := "req_" + uuid.New().String()[:12]

	// Build prompt content
	var contentItems []map[string]string
	if userMessage != "" {
		contentItems = append(contentItems, map[string]string{"type": "text", "content": userMessage})
	}
	for _, u := range mediaURLs {
		encodedURL := ensureEncodedURL(u)
		contentItems = append(contentItems, map[string]string{"type": "url", "content": encodedURL})
	}
	if len(contentItems) == 0 {
		log.Error().Str("token", token).Msg("send_to_bot called with empty content")
		return "", fmt.Errorf("empty content")
	}

	rpcRequest := map[string]interface{}{
		"jsonrpc": "2.0",
		"id":      requestID,
		"method":  "session/prompt",
		"params": map[string]interface{}{
			"sessionId": sessionID,
			"prompt": map[string]interface{}{
				"content": contentItems,
			},
		},
	}

	route, err := b.ResolveBotRoute(ctx, token)
	if err != nil {
		log.Error().Err(err).Str("token", token).Msg("Failed to resolve bot route")
		return "", err
	}

	// Store trace context for best-effort reverse link (Bot → Chat)
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	if data, err := json.Marshal(map[string]string(carrier)); err == nil {
		b.rdb.Set(ctx, TraceCtxPrefix+sessionID, data, 600*time.Second)
	}

	if err := b.PublishToWorkerInbox(ctx, route.WorkerID, token, rpcRequest); err != nil {
		log.Error().Err(err).Str("token", token).Msg("Failed to push to bot inbox")
		return "", err
	}

	log.Info().Str("req", requestID).Int("media", len(mediaURLs)).Str("token", token).
		Msg("Sent to bot (inbox)")
	return requestID, nil
}

// HandleBotMessage parses a JSON-RPC message from the bot and forwards to chat.
func (b *ConnectionBridge) HandleBotMessage(ctx context.Context, token, raw string) {
	var msg map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &msg); err != nil {
		log.Warn().Str("token", token).Msg("Invalid JSON from bot")
		return
	}

	// Ping/pong
	msgType := ""
	if t, _ := msg["type"].(string); t == "ping" {
		msgType = "ping"
		if connI, ok := b.bots.Load(token); ok {
			conn := connI.(*BotConn)
			conn.WriteMessage(websocket.TextMessage, []byte("pong"))
		}
		return
	}

	method, _ := msg["method"].(string)
	params, _ := msg["params"].(map[string]interface{})

	// Determine message_type for span attribute
	if method != "" {
		msgType = "notification"
	} else if _, hasID := msg["id"]; hasID {
		if _, hasResult := msg["result"]; hasResult {
			msgType = "result"
		} else if _, hasErr := msg["error"]; hasErr {
			msgType = "error"
		}
	}

	// Extract sessionId early for trace context restoration
	var topSessionID string
	if method != "" {
		topSessionID, _ = getNestedString(params, "sessionId")
	} else if _, hasID := msg["id"]; hasID {
		if errObj, hasErr := msg["error"]; hasErr {
			if errMap, ok := errObj.(map[string]interface{}); ok && errMap != nil {
				if dataObj, ok := errMap["data"].(map[string]interface{}); ok {
					topSessionID, _ = dataObj["sessionId"].(string)
				}
			}
		}
		if topSessionID == "" {
			topSessionID, _ = msg["sessionId"].(string)
		}
	}

	// Restore trace context from Redis (best-effort, covers all paths)
	if topSessionID != "" {
		if raw, err := b.rdb.Get(context.Background(), TraceCtxPrefix+topSessionID).Result(); err == nil {
			var m map[string]string
			if json.Unmarshal([]byte(raw), &m) == nil {
				ctx = otel.GetTextMapPropagator().Extract(context.Background(), propagation.MapCarrier(m))
			}
		}
	}

	if method != "" {
		chatEvent := TranslateBotEvent(method, params)
		sessionID, _ := getNestedString(params, "sessionId")
		requestID, _ := getNestedString(params, "requestId")

		// Create bot.message.receive span
		ctx, msgSpan := bridgeTracer.Start(ctx, "bot.message.receive",
			trace.WithSpanKind(trace.SpanKindInternal))
		msgSpan.SetAttributes(
			attribute.String("astron.token_id", token),
			attribute.String("astron.method", method),
			attribute.String("astron.message_type", msgType),
		)
		if sessionID != "" {
			msgSpan.SetAttributes(attribute.String("astron.session_id", sessionID))
		}
		if requestID != "" {
			msgSpan.SetAttributes(attribute.String("astron.turn_id", requestID))
		}

		if sessionID == "" {
			log.Warn().Str("method", method).Str("token", token).
				Msg("Bot notification missing sessionId")
		}

		if chatEvent != nil {
			eventType, _ := chatEvent["type"].(string)
			if eventType == "chunk" || eventType == "thinking" {
				log.Debug().Str("method", method).Str("type", eventType).
					Str("token", token).Msg("Bot event")
			} else {
				log.Info().Str("method", method).Str("token", token).Msg("Bot event")
			}
			if sessionID != "" {
				b.sendToSession(ctx, token, sessionID, chatEvent)
			}
		} else {
			log.Warn().Str("method", method).Str("token", token).
				Msg("Bot event dropped: untranslatable")
		}

		msgSpan.End()
	}

	// Result / Error
	if _, hasID := msg["id"]; hasID {
		if _, hasResult := msg["result"]; hasResult {
			reqID, _ := msg["id"].(string)
			log.Info().Str("req", reqID).Str("token", token).Msg("Bot result")
		} else if errObj, hasErr := msg["error"]; hasErr {
			errMap, _ := errObj.(map[string]interface{})
			sessionID := ""
			if errMap != nil {
				if dataObj, ok := errMap["data"].(map[string]interface{}); ok {
					sessionID, _ = dataObj["sessionId"].(string)
				}
			}
			if sessionID == "" {
				sessionID, _ = msg["sessionId"].(string)
			}
			errMsg := model.ErrBotUnknownError.Message
			if errMap != nil {
				if m, ok := errMap["message"].(string); ok {
					errMsg = m
				}
			}
			log.Error().Str("error", errMsg).Str("token", token).Msg("Bot JSON-RPC error")
			if sessionID != "" {
				b.sendToSession(ctx, token, sessionID, map[string]interface{}{
					"type": "error", "content": errMsg,
				})
			}
		}
	}
}

// NotifyBotConnected logs bot connection.
func (b *ConnectionBridge) NotifyBotConnected(token string) {
	log.Info().Str("token", token).Msg("Bot status -> connected")
}

// NotifyBotDisconnected logs bot disconnection.
func (b *ConnectionBridge) NotifyBotDisconnected(token string) {
	log.Info().Str("token", token).Msg("Bot status -> disconnected")
}

func (b *ConnectionBridge) shouldSkipSharedCleanup(ctx context.Context, token string, localGen int64, source string) bool {
	remoteGenRaw, err := b.rdb.Get(ctx, BotGenPrefix+token).Result()
	if err != nil {
		return false
	}

	remoteGen, err := strconv.ParseInt(remoteGenRaw, 10, 64)
	if err != nil {
		return false
	}

	if remoteGen > localGen {
		log.Info().
			Int64("remote_gen", remoteGen).
			Int64("local_gen", localGen).
			Str("worker", b.workerID).
			Str("source", source).
			Str("token", token).
			Msg("Skip Redis cleanup: newer gen exists")
		return true
	}

	return false
}

func (b *ConnectionBridge) cleanupSharedBotState(ctx context.Context, token string, deleteGen bool) {
	b.rdb.ZRem(ctx, BotAliveKey, token)
	b.rdb.Del(ctx, BotOwnerPrefix+token)
	b.enqueueArtifactCleanup(token)
	if deleteGen {
		b.rdb.Del(ctx, BotGenPrefix+token)
	}
}

func (b *ConnectionBridge) sendToSession(ctx context.Context, token, sessionID string, event map[string]interface{}) {
	ctx, span := bridgeTracer.Start(ctx, "chat.message.deliver", trace.WithSpanKind(trace.SpanKindProducer))
	defer span.End()

	eventType, _ := event["type"].(string)
	span.SetAttributes(
		attribute.String("astron.token_id", token),
		attribute.String("astron.session_id", sessionID),
		attribute.String("astron.event_type", eventType),
	)

	if b.shuttingDown.Load() {
		return
	}
	inbox := ChatInboxPrefix + token + ":" + sessionID
	exists, _ := b.rdb.Exists(ctx, inbox).Result()
	inboxExists := exists > 0
	span.SetAttributes(attribute.Bool("astron.inbox_exists", inboxExists))
	if !inboxExists {
		log.Debug().Str("token", token).Str("session", sessionID).
			Msg("No active SSE consumer, skipping event")
		return
	}
	data, _ := json.Marshal(event)
	if _, err := b.queue.Publish(ctx, inbox, string(data)); err != nil {
		span.SetStatus(codes.Error, "publish to chat inbox failed")
		span.RecordError(err)
		if !b.shuttingDown.Load() {
			log.Error().Err(err).Str("token", token).Str("session", sessionID).
				Msg("Failed to send to session inbox")
		}
	}
}

// Shutdown gracefully shuts down the bridge.
func (b *ConnectionBridge) Shutdown() {
	b.regMu.Lock()
	b.shuttingDown.Store(true)
	b.regMu.Unlock()
	log.Info().Str("worker", b.workerID).Msg("Bridge worker shutting down...")

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	// Close all bot connections and clean Redis
	b.bots.Range(func(key, value interface{}) bool {
		token := key.(string)
		conn := value.(*BotConn)
		localGenI, _ := b.botGens.Load(token)
		var localGen int64
		if localGenI != nil {
			localGen = localGenI.(int64)
		}

		_ = conn.Close(model.ErrWSServerRestart.HTTPStatus, model.ErrWSServerRestart.Message)
		if !b.shouldSkipSharedCleanup(ctx, token, localGen, "shutdown") {
			b.cleanupSharedBotState(ctx, token, true)
		}
		return true
	})

	b.cancel() // Cancel heartbeat
	b.wg.Wait()

	log.Info().Str("worker", b.workerID).Msg("Bridge worker shutdown complete")
}

// TranslateBotEvent converts a bot JSON-RPC notification to a chat event.
func TranslateBotEvent(method string, params map[string]interface{}) map[string]interface{} {
	if method != "session/update" {
		return nil
	}
	if params == nil {
		return nil
	}

	update, _ := params["update"].(map[string]interface{})
	if update == nil {
		return nil
	}

	updateType, _ := update["sessionUpdate"].(string)
	content, _ := update["content"].(map[string]interface{})

	switch updateType {
	case "agent_message_chunk":
		text, _ := getNestedString(content, "text")
		return map[string]interface{}{"type": "chunk", "content": text}

	case "agent_message_final":
		text, _ := getNestedString(content, "text")
		return map[string]interface{}{"type": "done", "content": text}

	case "tool_result":
		resultText := ""
		rawContent := update["content"]
		switch v := rawContent.(type) {
		case string:
			resultText = v
		case map[string]interface{}:
			if t, ok := v["text"].(string); ok {
				resultText = t
			}
		default:
			if v != nil {
				b, _ := json.Marshal(v)
				resultText = string(b)
			}
		}
		title, _ := update["title"].(string)
		if title == "" {
			title = "tool"
		}
		status, _ := update["status"].(string)
		if status == "" {
			status = "completed"
		}
		return map[string]interface{}{
			"type": "tool_result", "name": title,
			"status": status, "content": resultText,
		}

	case "agent_thought_chunk":
		text, _ := getNestedString(content, "text")
		return map[string]interface{}{"type": "thinking", "content": text}

	case "tool_call":
		title, _ := update["title"].(string)
		if title == "" {
			title = "tool"
		}
		inputText := ""
		rawInput := update["content"]
		switch v := rawInput.(type) {
		case string:
			inputText = v
		default:
			if v != nil {
				b, _ := json.Marshal(v)
				inputText = string(b)
			}
		}
		return map[string]interface{}{
			"type": "tool_call", "name": title, "input": inputText,
		}

	case "agent_media":
		if content == nil {
			return nil
		}
		media, _ := content["media"].(map[string]interface{})
		if media == nil {
			return nil
		}
		downloadURL, _ := media["downloadUrl"].(string)
		if downloadURL == "" {
			log.Warn().Msg("agent_media event missing downloadUrl")
			return nil
		}
		data := map[string]interface{}{"type": "url", "content": downloadURL}
		caption, _ := getNestedString(content, "text")
		if caption != "" {
			data["caption"] = caption
		}
		return map[string]interface{}{"type": "media", "data": data}

	default:
		// Fallback: if content has text, treat as chunk
		if content != nil {
			if text, ok := content["text"].(string); ok {
				log.Debug().Str("sessionUpdate", updateType).Msg("Bot event fallback to chunk")
				return map[string]interface{}{"type": "chunk", "content": text}
			}
		}
		log.Warn().Str("sessionUpdate", updateType).Msg("Bot event untranslatable")
		return nil
	}
}

func ensureEncodedURL(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	decoded, err := url.PathUnescape(parsed.Path)
	if err != nil {
		return rawURL
	}
	parsed.RawPath = url.PathEscape(decoded)
	// Restore forward slashes
	parsed.RawPath = strings.ReplaceAll(parsed.RawPath, "%2F", "/")
	return parsed.String()
}

func getNestedString(m map[string]interface{}, key string) (string, bool) {
	if m == nil {
		return "", false
	}
	v, ok := m[key]
	if !ok {
		return "", false
	}
	s, ok := v.(string)
	return s, ok
}
