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

	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/pkg"
)

const (
	BotAliveKey        = "bridge:bot_alive"
	BotInboxPrefix     = "bridge:bot_inbox:"
	ChatInboxPrefix    = "bridge:chat_inbox:"
	ChatInboxIdxPrefix = "bridge:chat_inbox_idx:"
	CleanupLockKey     = "bridge:cleanup_lock"
	BotGenPrefix       = "bridge:bot_gen:"

	BotTTL             = 30 * time.Second
	HeartbeatInterval  = 10 * time.Second
	ConsumeBlockMs     = 5000 // keep as int for blockMs parameter
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
	pollCancels  sync.Map // "bot:{token}" -> context.CancelFunc
	shuttingDown atomic.Bool
	regMu        sync.Mutex
	wg           sync.WaitGroup
	ctx          context.Context
	cancel       context.CancelFunc
	groupChat    *GroupChatManager // optional, nil-safe
	delegationTrackers sync.Map // groupID -> *delegationTracker
}

type delegationTracker struct {
	mu               sync.Mutex
	leaderToken      string
	delegated        map[string]bool
	completed        map[string]bool
	summaryTriggered bool
}

// NewConnectionBridge creates a new ConnectionBridge.
func NewConnectionBridge(rdb redis.UniversalClient, sessionStore *SessionStore, queue MessageQueue) *ConnectionBridge {
	ctx, cancel := context.WithCancel(context.Background())
	return &ConnectionBridge{
		workerID:     uuid.New().String()[:12],
		rdb:          rdb,
		sessionStore: sessionStore,
		queue:        queue,
		ctx:          ctx,
		cancel:       cancel,
	}
}

// Start begins the heartbeat goroutine.
func (b *ConnectionBridge) Start() {
	b.wg.Add(1)
	go b.runHeartbeat()
	log.Info().Str("worker", b.workerID).Msg("Bridge worker started")
}

func (b *ConnectionBridge) runHeartbeat() {
	defer b.wg.Done()
	ticker := time.NewTicker(HeartbeatInterval)
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

func (b *ConnectionBridge) doHeartbeat() {
	ctx := b.ctx
	now := time.Now().Unix()

	// Cross-worker eviction check
	b.botGens.Range(func(key, value interface{}) bool {
		token := key.(string)
		localGen := value.(int64)
		remoteGenRaw, err := b.rdb.Get(ctx, BotGenPrefix+token).Result()
		if err == nil {
			remoteGen, err := strconv.ParseInt(remoteGenRaw, 10, 64)
			if err == nil && remoteGen > localGen {
				log.Info().Int64("remote_gen", remoteGen).Int64("local_gen", localGen).
					Str("worker", b.workerID).Str("token", pkg.SafePrefix(token, 10)).
					Msg("Heartbeat eviction")
				b.evictLocal(token)
			}
		}
		return true
	})

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

	// Compete for cleanup lock
	acquired, _ := b.rdb.SetNX(ctx, CleanupLockKey, b.workerID, HeartbeatInterval).Result()
	if acquired {
		b.cleanupExpiredBots(ctx, float64(now))
	}
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
		b.queue.DeleteQueue(ctx, BotInboxPrefix+tok)
		b.cleanupChatInboxes(ctx, tok)
		b.rdb.Del(ctx, BotGenPrefix+tok)
	}
	b.rdb.ZRemRangeByScore(ctx, BotAliveKey, "-inf", fmt.Sprintf("%f", cutoff))
	log.Info().Int("count", len(expired)).Msg("Cleanup: removed expired bot(s)")
}

func (b *ConnectionBridge) cleanupChatInboxes(ctx context.Context, token string) {
	idxKey := ChatInboxIdxPrefix + token
	keys, err := b.rdb.SMembers(ctx, idxKey).Result()
	if err != nil {
		log.Warn().Err(err).Str("token", pkg.SafePrefix(token, 10)).Msg("Error reading chat inbox index")
		return
	}
	if len(keys) > 0 {
		log.Warn().Int("orphan_inboxes", len(keys)).Str("token", pkg.SafePrefix(token, 10)).
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

	// Store locally
	b.bots.Store(token, conn)
	b.botGens.Store(token, gen)

	// Ensure consumer group and start poll (outside critical section logic but still under defer)
	inbox := BotInboxPrefix + token
	b.queue.EnsureGroup(ctx, inbox, "bot")

	// Cancel old poll
	taskKey := "bot:" + token
	if cancelFn, loaded := b.pollCancels.LoadAndDelete(taskKey); loaded {
		cancelFn.(context.CancelFunc)()
	}

	// Start new poll
	pollCtx, pollCancel := context.WithCancel(b.ctx)
	b.pollCancels.Store(taskKey, pollCancel)
	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		b.pollBotInbox(pollCtx, token, gen)
	}()

	log.Info().Str("worker", b.workerID).Int64("gen", gen).Str("token", pkg.SafePrefix(token, 10)).
		Msg("Bot registered")
	return nil
}

func (b *ConnectionBridge) evictLocal(token string) {
	connI, loaded := b.bots.LoadAndDelete(token)
	b.botGens.Delete(token)

	taskKey := "bot:" + token
	if cancelFn, ok := b.pollCancels.LoadAndDelete(taskKey); ok {
		cancelFn.(context.CancelFunc)()
	}

	if loaded && connI != nil {
		conn := connI.(*BotConn)
		_ = conn.Close(model.ErrWSEvicted.Code, model.ErrWSEvicted.Message)
	}
	b.NotifyBotDisconnected(token)
	log.Info().Str("worker", b.workerID).Str("token", pkg.SafePrefix(token, 10)).Msg("Evicted local bot")
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
	taskKey := "bot:" + token
	if cancelFn, ok := b.pollCancels.LoadAndDelete(taskKey); ok {
		cancelFn.(context.CancelFunc)()
	}
	b.NotifyBotDisconnected(token)
	b.regMu.Unlock()

	// Cross-worker guard
	remoteGenRaw, err := b.rdb.Get(ctx, BotGenPrefix+token).Result()
	if err == nil {
		remoteGen, err := strconv.ParseInt(remoteGenRaw, 10, 64)
		if err == nil && remoteGen > localGen {
			log.Info().Int64("remote_gen", remoteGen).Int64("local_gen", localGen).
				Str("token", pkg.SafePrefix(token, 10)).Msg("Skip Redis cleanup: newer gen exists")
			return
		}
	}

	// Clean Redis state
	b.rdb.ZRem(ctx, BotAliveKey, token)
	b.queue.DeleteQueue(ctx, BotInboxPrefix+token)
	b.cleanupChatInboxes(ctx, token)
	log.Info().Str("worker", b.workerID).Str("token", pkg.SafePrefix(token, 10)).Msg("Bot unregistered")
}

// RemoveBotSessions destroys session data for a token (admin delete).
func (b *ConnectionBridge) RemoveBotSessions(ctx context.Context, token string) error {
	if err := b.sessionStore.RemoveSessions(ctx, token); err != nil {
		return err
	}

	if connI, loaded := b.bots.Load(token); loaded {
		conn := connI.(*BotConn)
		_ = conn.Close(model.ErrWSTokenDeleted.Code, model.ErrWSTokenDeleted.Message)
		b.UnregisterBot(ctx, token, nil)
	} else {
		// Bot may be on remote worker — push disconnect command
		inbox := BotInboxPrefix + token
		b.queue.Publish(ctx, inbox, `{"_disconnect":true}`)
		b.rdb.ZRem(ctx, BotAliveKey, token)
		b.queue.DeleteQueue(ctx, BotInboxPrefix+token)
		b.cleanupChatInboxes(ctx, token)
		b.rdb.Del(ctx, BotGenPrefix+token)
	}
	log.Info().Str("token", pkg.SafePrefix(token, 10)).Msg("Bot sessions fully removed")
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
	alive, _ := b.rdb.ZRangeByScore(ctx, BotAliveKey, &redis.ZRangeBy{
		Min: fmt.Sprintf("%f", cutoff),
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
	log.Info().Str("session", pkg.SafePrefix(sessionID, 8)).Str("token", pkg.SafePrefix(token, 10)).Msg("Session created")
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

// SendToBot creates a JSON-RPC request and sends it to the bot inbox.
func (b *ConnectionBridge) SendToBot(ctx context.Context, token, userMessage string, mediaURLs []string, sessionID string) (string, error) {
	if sessionID == "" {
		log.Error().Str("token", pkg.SafePrefix(token, 10)).Msg("send_to_bot called without session_id")
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
		log.Error().Str("token", pkg.SafePrefix(token, 10)).Msg("send_to_bot called with empty content")
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

	inbox := BotInboxPrefix + token
	data, _ := json.Marshal(map[string]interface{}{"rpc_request": rpcRequest})
	if _, err := b.queue.Publish(ctx, inbox, string(data)); err != nil {
		log.Error().Err(err).Str("token", pkg.SafePrefix(token, 10)).Msg("Failed to push to bot inbox")
		return "", err
	}

	log.Info().Str("req", requestID).Int("media", len(mediaURLs)).Str("token", pkg.SafePrefix(token, 10)).
		Msg("Sent to bot (inbox)")
	return requestID, nil
}

// HandleBotMessage parses a JSON-RPC message from the bot and forwards to chat.
func (b *ConnectionBridge) HandleBotMessage(ctx context.Context, token, raw string) {
	var msg map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &msg); err != nil {
		log.Warn().Str("token", pkg.SafePrefix(token, 10)).Msg("Invalid JSON from bot")
		return
	}

	// Ping/pong
	if msgType, _ := msg["type"].(string); msgType == "ping" {
		if connI, ok := b.bots.Load(token); ok {
			conn := connI.(*BotConn)
			conn.WriteMessage(websocket.TextMessage, []byte("pong"))
		}
		return
	}

	method, _ := msg["method"].(string)
	params, _ := msg["params"].(map[string]interface{})

	if method != "" {
		chatEvent := TranslateBotEvent(method, params)
		sessionID, _ := getNestedString(params, "sessionId")

		if sessionID == "" {
			log.Warn().Str("method", method).Str("token", pkg.SafePrefix(token, 10)).
				Msg("Bot notification missing sessionId")
		}

		if chatEvent != nil {
			eventType, _ := chatEvent["type"].(string)
			if eventType == "chunk" || eventType == "thinking" {
				log.Debug().Str("method", method).Str("type", eventType).
					Str("token", pkg.SafePrefix(token, 10)).Msg("Bot event")
			} else {
				log.Info().Str("method", method).Str("token", pkg.SafePrefix(token, 10)).Msg("Bot event")
			}
			if sessionID != "" {
				b.sendToSession(ctx, token, sessionID, chatEvent)
				b.broadcastToGroupIfNeeded(ctx, token, sessionID, chatEvent)
			}
		} else {
			log.Warn().Str("method", method).Str("token", pkg.SafePrefix(token, 10)).
				Msg("Bot event dropped: untranslatable")
		}
	}

	// Result / Error
	if _, hasID := msg["id"]; hasID {
		if _, hasResult := msg["result"]; hasResult {
			reqID, _ := msg["id"].(string)
			log.Info().Str("req", reqID).Str("token", pkg.SafePrefix(token, 10)).Msg("Bot result")
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
			log.Error().Str("error", errMsg).Str("token", pkg.SafePrefix(token, 10)).Msg("Bot JSON-RPC error")
			if sessionID != "" {
				errEvent := map[string]interface{}{
					"type": "error", "content": errMsg,
				}
				b.sendToSession(ctx, token, sessionID, errEvent)
				b.broadcastToGroupIfNeeded(ctx, token, sessionID, errEvent)
			}
		}
	}
}

// NotifyBotConnected logs bot connection.
func (b *ConnectionBridge) NotifyBotConnected(token string) {
	log.Info().Str("token", pkg.SafePrefix(token, 10)).Msg("Bot status -> connected")
}

// SetGroupChat sets the GroupChatManager for group broadcast support.
func (b *ConnectionBridge) SetGroupChat(gc *GroupChatManager) {
	b.groupChat = gc
}

func (b *ConnectionBridge) trackDelegation(groupID, leaderToken, targetToken string) {
	val, _ := b.delegationTrackers.LoadOrStore(groupID, &delegationTracker{
		leaderToken: leaderToken,
		delegated:   make(map[string]bool),
		completed:   make(map[string]bool),
	})
	tracker := val.(*delegationTracker)
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	tracker.delegated[targetToken] = true
}

func (b *ConnectionBridge) checkDelegationCompletion(ctx context.Context, groupID, doneToken string) {
	val, ok := b.delegationTrackers.Load(groupID)
	if !ok {
		return
	}
	tracker := val.(*delegationTracker)
	tracker.mu.Lock()
	defer tracker.mu.Unlock()

	if !tracker.delegated[doneToken] {
		return
	}
	tracker.completed[doneToken] = true

	for token := range tracker.delegated {
		if !tracker.completed[token] {
			return
		}
	}

	if tracker.summaryTriggered {
		return
	}
	tracker.summaryTriggered = true

	// All delegated agents done
	if b.groupChat != nil {
		b.groupChat.BroadcastToGroup(ctx, groupID, map[string]interface{}{
			"type": "all_done",
		})
		go b.triggerBossSummary(ctx, groupID, tracker.leaderToken)
	}
}

func (b *ConnectionBridge) triggerBossSummary(ctx context.Context, groupID, leaderToken string) {
	if b.groupChat == nil {
		return
	}
	time.Sleep(500 * time.Millisecond)
	if err := b.groupChat.TriggerBossSummary(ctx, groupID, leaderToken); err != nil {
		log.Error().Err(err).Str("group", groupID).Msg("Failed to trigger boss summary")
	}
}

// NotifyBotDisconnected logs bot disconnection.
func (b *ConnectionBridge) NotifyBotDisconnected(token string) {
	log.Info().Str("token", pkg.SafePrefix(token, 10)).Msg("Bot status -> disconnected")
}

func (b *ConnectionBridge) sendToSession(ctx context.Context, token, sessionID string, event map[string]interface{}) {
	if b.shuttingDown.Load() {
		return
	}
	inbox := ChatInboxPrefix + token + ":" + sessionID
	exists, _ := b.rdb.Exists(ctx, inbox).Result()
	if exists == 0 {
		log.Debug().Str("token", pkg.SafePrefix(token, 10)).Str("session", pkg.SafePrefix(sessionID, 8)).
			Msg("No active SSE consumer, skipping event")
		return
	}
	data, _ := json.Marshal(event)
	if _, err := b.queue.Publish(ctx, inbox, string(data)); err != nil {
		if !b.shuttingDown.Load() {
			log.Error().Err(err).Str("token", pkg.SafePrefix(token, 10)).Str("session", pkg.SafePrefix(sessionID, 8)).
				Msg("Failed to send to session inbox")
		}
	}
}

// broadcastToGroupIfNeeded checks if a session belongs to a group and broadcasts the event.
func (b *ConnectionBridge) broadcastToGroupIfNeeded(ctx context.Context, token, sessionID string, chatEvent map[string]interface{}) {
	if b.groupChat == nil || sessionID == "" {
		return
	}
	groupID, ok := b.groupChat.LookupGroupForSession(ctx, sessionID)
	if !ok {
		return
	}
	agentEvent := copyMap(chatEvent)
	agentEvent["agent"] = map[string]interface{}{
		"token": token,
		"name":  b.groupChat.GetAgentName(ctx, token),
	}
	b.groupChat.BroadcastToGroup(ctx, groupID, agentEvent)

	// Check for delegation via tool_call
	eventType, _ := chatEvent["type"].(string)
	if eventType == "tool_call" {
		b.maybeHandleDelegation(ctx, groupID, token, chatEvent)
	}

	if eventType == "done" {
		b.checkDelegationCompletion(ctx, groupID, token)
	}
}

// maybeHandleDelegation intercepts tool_call events from a leader bot to delegate work.
func (b *ConnectionBridge) maybeHandleDelegation(ctx context.Context, groupID, token string, chatEvent map[string]interface{}) {
	if b.groupChat == nil {
		return
	}

	// Only leaders can delegate
	if !b.groupChat.groupMgr.IsLeader(ctx, groupID, token) {
		return
	}

	toolName, _ := chatEvent["name"].(string)
	if !isDelegationTool(toolName) {
		return
	}

	inputText, _ := chatEvent["input"].(string)
	if inputText == "" {
		return
	}

	targetName, content := parseDelegationArgs(inputText)
	if targetName == "" || content == "" {
		log.Debug().Str("tool", toolName).Str("group", groupID).Msg("Delegation: could not parse target/content")
		return
	}

	targetToken, err := b.groupChat.ResolveAgentByName(ctx, groupID, targetName)
	if err != nil {
		log.Warn().Err(err).Str("target", targetName).Str("group", groupID).Msg("Delegation: target agent not found")
		return
	}

	// Broadcast delegation event to group WS
	leaderName := b.groupChat.GetAgentName(ctx, token)
	delegationEvent := map[string]interface{}{
		"type":       "delegation",
		"from":       map[string]interface{}{"token": token, "name": leaderName},
		"to":         map[string]interface{}{"token": targetToken, "name": targetName},
		"content":    content,
	}
	b.groupChat.BroadcastToGroup(ctx, groupID, delegationEvent)

	// Delegate asynchronously
	go func() {
		if err := b.groupChat.DelegateToAgent(ctx, groupID, targetToken, content); err != nil {
			log.Error().Err(err).Str("target", targetName).Str("group", groupID).Msg("Delegation failed")
		}
	}()

	log.Info().Str("from", leaderName).Str("to", targetName).Str("group", groupID).Msg("Delegation triggered")
	b.trackDelegation(groupID, token, targetToken)
}

// isDelegationTool checks if a tool name matches a delegation pattern.
func isDelegationTool(name string) bool {
	lower := strings.ToLower(name)
	return strings.Contains(lower, "sessions_send") ||
		strings.Contains(lower, "sessions_spawn") ||
		strings.Contains(lower, "send_message") ||
		strings.Contains(lower, "sessions-send")
}

// parseDelegationArgs parses tool input JSON to extract target agent name and message content.
// Supports both sessions_send (sessionKey/message) and sessions_spawn (agentId/task) formats.
func parseDelegationArgs(input string) (targetName, content string) {
	var args map[string]interface{}
	if err := json.Unmarshal([]byte(input), &args); err != nil {
		return "", ""
	}

	// Try direct agent name fields first
	for _, key := range []string{"agentId", "agent_id", "target", "targetAgent", "target_agent", "to"} {
		if v, ok := args[key].(string); ok && v != "" {
			targetName = v
			break
		}
	}

	// If no direct agent name, try to extract from sessionKey
	// sessionKey format: "agent:<agentId>:<channel>:..." e.g. "agent:ainews:astron-claw:direct:xxx"
	if targetName == "" {
		for _, key := range []string{"sessionKey", "sessionId", "session_key"} {
			if v, ok := args[key].(string); ok && v != "" {
				targetName = extractAgentFromSessionKey(v)
				break
			}
		}
	}

	// Try various field names for content
	for _, key := range []string{"message", "content", "text", "msg", "task"} {
		if v, ok := args[key].(string); ok && v != "" {
			content = v
			break
		}
	}

	return targetName, content
}

// extractAgentFromSessionKey extracts an agent name from a session key like "agent:ainews:...".
func extractAgentFromSessionKey(key string) string {
	// Format: "agent:<agentId>:<channel>:..."
	if !strings.HasPrefix(key, "agent:") {
		return ""
	}
	parts := strings.SplitN(key, ":", 3)
	if len(parts) >= 2 {
		return parts[1]
	}
	return ""
}

// copyMap creates a shallow copy of a map.
func copyMap(m map[string]interface{}) map[string]interface{} {
	cp := make(map[string]interface{}, len(m))
	for k, v := range m {
		cp[k] = v
	}
	return cp
}

func (b *ConnectionBridge) pollBotInbox(ctx context.Context, token string, gen int64) {
	inbox := BotInboxPrefix + token

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		if b.shuttingDown.Load() {
			return
		}

		result, err := b.queue.Consume(ctx, inbox, "bot", "bot", ConsumeBlockMs)
		if err != nil {
			if !b.shuttingDown.Load() {
				log.Error().Err(err).Str("token", pkg.SafePrefix(token, 10)).Msg("Bot inbox consume error")
				select {
				case <-ctx.Done():
					return
				case <-time.After(1 * time.Second):
				}
			}
			continue
		}

		// Cross-worker eviction self-check
		if gen > 0 {
			remoteGenRaw, err := b.rdb.Get(ctx, BotGenPrefix+token).Result()
			if err == nil {
				remoteGen, err := strconv.ParseInt(remoteGenRaw, 10, 64)
				if err == nil && remoteGen > gen {
					log.Info().Int64("remote_gen", remoteGen).Int64("local_gen", gen).
						Str("token", pkg.SafePrefix(token, 10)).Msg("Poll task evicted")
					b.evictLocal(token)
					return
				}
			}
		}

		if result == nil {
			continue
		}

		b.queue.Ack(ctx, inbox, "bot", result.ID)

		var data map[string]interface{}
		if err := json.Unmarshal([]byte(result.Data), &data); err != nil {
			continue
		}

		// Handle disconnect command
		if disc, _ := data["_disconnect"].(bool); disc {
			if connI, loaded := b.bots.LoadAndDelete(token); loaded {
				conn := connI.(*BotConn)
				_ = conn.Close(model.ErrWSTokenDeleted.Code, model.ErrWSTokenDeleted.Message)
			}
			b.botGens.Delete(token)
			b.pollCancels.Delete("bot:" + token)
			b.NotifyBotDisconnected(token)
			log.Info().Str("token", pkg.SafePrefix(token, 10)).Msg("Inbox: received disconnect for bot")
			return
		}

		// Forward RPC to local WS
		if connI, ok := b.bots.Load(token); ok {
			conn := connI.(*BotConn)
			if rpcReq, ok := data["rpc_request"]; ok {
				if err := conn.WriteJSON(rpcReq); err != nil {
					log.Warn().Err(err).Str("token", pkg.SafePrefix(token, 10)).Msg("Failed to forward to bot WS")
				} else {
					log.Info().Str("token", pkg.SafePrefix(token, 10)).Msg("Inbox: forwarded to local bot")
				}
			}
		} else {
			log.Warn().Str("token", pkg.SafePrefix(token, 10)).Msg("Inbox: bot WS gone, message dropped")
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
		_ = conn.Close(model.ErrWSServerRestart.Code, model.ErrWSServerRestart.Message)
		b.rdb.ZRem(ctx, BotAliveKey, token)
		b.queue.DeleteQueue(ctx, BotInboxPrefix+token)
		b.cleanupChatInboxes(ctx, token)
		b.rdb.Del(ctx, BotGenPrefix+token)
		return true
	})

	// Cancel all poll tasks
	b.pollCancels.Range(func(key, value interface{}) bool {
		value.(context.CancelFunc)()
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
