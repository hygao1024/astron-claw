package service

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/pkg"
)

const (
	GroupInboxPrefix   = "bridge:group_inbox:"
	GroupSessionPrefix = "bridge:group_session:"
	GroupSessionTTL    = 1 * time.Hour
)

// GroupWSConn wraps a WebSocket connection for group chat clients.
type GroupWSConn struct {
	ID   string
	Conn *websocket.Conn
	mu   sync.Mutex
}

// WriteJSON safely writes JSON to the WebSocket.
func (c *GroupWSConn) WriteJSON(v interface{}) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.Conn.WriteJSON(v)
}

// GroupChatManager manages group chat message routing via Redis Streams and WebSocket fan-out.
type GroupChatManager struct {
	bridge   *ConnectionBridge
	groupMgr *GroupManager
	tokenMgr *TokenManager
	queue    MessageQueue
	rdb      redis.UniversalClient

	conns         sync.Map // groupID -> *sync.Map (connID -> *GroupWSConn)
	consumers     sync.Map // groupID -> context.CancelFunc
	nameCache     sync.Map // token -> name (string)
	agentSessions sync.Map // "groupID\x00token" -> sessionID (string)
}

// NewGroupChatManager creates a new GroupChatManager.
func NewGroupChatManager(bridge *ConnectionBridge, groupMgr *GroupManager, tokenMgr *TokenManager, queue MessageQueue, rdb redis.UniversalClient) *GroupChatManager {
	return &GroupChatManager{
		bridge:   bridge,
		groupMgr: groupMgr,
		tokenMgr: tokenMgr,
		queue:    queue,
		rdb:      rdb,
	}
}

// RegisterGroupSession maps a session to a group in Redis.
func (gc *GroupChatManager) RegisterGroupSession(ctx context.Context, groupID, sessionID string) {
	key := GroupSessionPrefix + sessionID
	gc.rdb.Set(ctx, key, groupID, GroupSessionTTL)
	log.Debug().Str("session", sessionID).Str("group", groupID).Msg("Registered group session")
}

// LookupGroupForSession checks if a session belongs to a group.
func (gc *GroupChatManager) LookupGroupForSession(ctx context.Context, sessionID string) (string, bool) {
	key := GroupSessionPrefix + sessionID
	groupID, err := gc.rdb.Get(ctx, key).Result()
	if err != nil {
		return "", false
	}
	return groupID, true
}

// GetAgentName returns the name for a token, using an in-memory cache.
func (gc *GroupChatManager) GetAgentName(ctx context.Context, token string) string {
	if v, ok := gc.nameCache.Load(token); ok {
		return v.(string)
	}
	name := gc.tokenMgr.GetName(ctx, token)
	gc.nameCache.Store(token, name)
	return name
}

// BroadcastToGroup publishes an event to the group inbox stream.
func (gc *GroupChatManager) BroadcastToGroup(ctx context.Context, groupID string, event map[string]interface{}) {
	data, err := json.Marshal(event)
	if err != nil {
		log.Error().Err(err).Str("group", groupID).Msg("Failed to marshal group event")
		return
	}
	key := GroupInboxPrefix + groupID
	gc.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: key,
		MaxLen: 1000,
		Approx: true,
		Values: map[string]interface{}{"data": string(data)},
	})
}

// RegisterConn adds a WS connection to a group and starts a consumer if needed.
func (gc *GroupChatManager) RegisterConn(groupID string, conn *GroupWSConn) {
	actual, _ := gc.conns.LoadOrStore(groupID, &sync.Map{})
	connMap := actual.(*sync.Map)
	connMap.Store(conn.ID, conn)

	// Start consumer if not already running
	if _, loaded := gc.consumers.Load(groupID); !loaded {
		ctx, cancel := context.WithCancel(context.Background())
		if _, raced := gc.consumers.LoadOrStore(groupID, cancel); raced {
			cancel()
		} else {
			go gc.startConsumer(ctx, groupID)
		}
	}
	log.Debug().Str("group", groupID).Str("conn", conn.ID).Msg("Registered group WS conn")
}

// UnregisterConn removes a WS connection from a group. Stops consumer if no connections remain.
func (gc *GroupChatManager) UnregisterConn(groupID string, connID string) {
	val, ok := gc.conns.Load(groupID)
	if !ok {
		return
	}
	connMap := val.(*sync.Map)
	connMap.Delete(connID)

	// Check if any connections remain
	empty := true
	connMap.Range(func(_, _ interface{}) bool {
		empty = false
		return false
	})

	if empty {
		gc.conns.Delete(groupID)
		if cancel, ok := gc.consumers.LoadAndDelete(groupID); ok {
			cancel.(context.CancelFunc)()
		}
	}
	log.Debug().Str("group", groupID).Str("conn", connID).Msg("Unregistered group WS conn")
}

// SendMessageToGroup sends a message to the leader agent (if one exists) or to all agents as fallback.
// Each agent gets its own session ID (created lazily), all mapped to the group.
func (gc *GroupChatManager) SendMessageToGroup(ctx context.Context, groupID, content string, mediaURLs []map[string]interface{}) error {
	// Convert media maps to URL strings for SendToBot
	var urls []string
	for _, m := range mediaURLs {
		if u, ok := m["content"].(string); ok {
			urls = append(urls, u)
		}
	}

	// Clear delegation tracker for new round
	if gc.bridge != nil {
		gc.bridge.delegationTrackers.Delete(groupID)
	}

	// Try leader-only routing
	leaderToken, err := gc.groupMgr.GetLeaderToken(ctx, groupID)
	if err == nil && leaderToken != "" {
		sessionID := gc.getOrCreateAgentSession(ctx, groupID, leaderToken)
		if sessionID == "" {
			log.Error().Str("token", pkg.SafePrefix(leaderToken, 10)).Str("group", groupID).Msg("Failed to get/create leader session")
			return gc.sendToAllAgents(ctx, groupID, content, urls)
		}
		if _, err := gc.bridge.SendToBot(ctx, leaderToken, content, urls, sessionID); err != nil {
			log.Error().Err(err).Str("token", pkg.SafePrefix(leaderToken, 10)).Str("group", groupID).Msg("Failed to send group message to leader")
			return err
		}
		return nil
	}

	// No leader — fallback to all agents
	return gc.sendToAllAgents(ctx, groupID, content, urls)
}

// sendToAllAgents sends a message to all agents in a group (backward-compatible fallback).
func (gc *GroupChatManager) sendToAllAgents(ctx context.Context, groupID, content string, urls []string) error {
	tokens, err := gc.groupMgr.GetAgentTokens(ctx, groupID)
	if err != nil {
		return err
	}

	for _, token := range tokens {
		sessionID := gc.getOrCreateAgentSession(ctx, groupID, token)
		if sessionID == "" {
			log.Error().Str("token", pkg.SafePrefix(token, 10)).Str("group", groupID).Msg("Failed to get/create agent session")
			continue
		}
		if _, err := gc.bridge.SendToBot(ctx, token, content, urls, sessionID); err != nil {
			log.Error().Err(err).Str("token", pkg.SafePrefix(token, 10)).Str("group", groupID).Msg("Failed to send group message to bot")
		}
	}
	return nil
}

// DelegateToAgent sends a delegation message to a specific agent in a group.
// The target agent's response will flow through bridge → broadcastToGroupIfNeeded → group WS.
func (gc *GroupChatManager) DelegateToAgent(ctx context.Context, groupID, targetToken, content string) error {
	sessionID := gc.getOrCreateAgentSession(ctx, groupID, targetToken)
	if sessionID == "" {
		return fmt.Errorf("failed to get/create session for target agent")
	}
	if _, err := gc.bridge.SendToBot(ctx, targetToken, content, nil, sessionID); err != nil {
		return fmt.Errorf("delegate to agent: %w", err)
	}
	log.Info().Str("group", groupID).Str("target", pkg.SafePrefix(targetToken, 10)).Msg("Delegated message to agent")
	return nil
}

// TriggerBossSummary sends a synthetic message to the leader to summarize sub-agent results.
func (gc *GroupChatManager) TriggerBossSummary(ctx context.Context, groupID, leaderToken string) error {
	sessionID := gc.getOrCreateAgentSession(ctx, groupID, leaderToken)
	if sessionID == "" {
		return fmt.Errorf("no session for leader")
	}
	summaryPrompt := "所有团队成员已完成各自的任务。请综合以上所有成员的输出结果，为用户提供简洁的汇总总结。不要重复所有细节，只提炼关键要点。"
	if _, err := gc.bridge.SendToBot(ctx, leaderToken, summaryPrompt, nil, sessionID); err != nil {
		return fmt.Errorf("trigger boss summary: %w", err)
	}
	log.Info().Str("group", groupID).Str("leader", pkg.SafePrefix(leaderToken, 10)).Msg("Boss summary triggered")
	return nil
}

// ResolveAgentByName resolves an agent name to its token within a group.
func (gc *GroupChatManager) ResolveAgentByName(ctx context.Context, groupID, name string) (string, error) {
	return gc.groupMgr.GetAgentTokenByName(ctx, groupID, name)
}

// getOrCreateAgentSession returns a session ID for a specific agent in a group,
// creating one if it doesn't exist yet.
func (gc *GroupChatManager) getOrCreateAgentSession(ctx context.Context, groupID, token string) string {
	key := groupID + "\x00" + token
	if v, ok := gc.agentSessions.Load(key); ok {
		return v.(string)
	}

	// Create a new session via the bridge
	sessionID, _, err := gc.bridge.CreateSession(ctx, token)
	if err != nil {
		log.Error().Err(err).Str("token", pkg.SafePrefix(token, 10)).Str("group", groupID).Msg("Failed to create agent session for group")
		return ""
	}

	// Map this session to the group
	gc.RegisterGroupSession(ctx, groupID, sessionID)

	gc.agentSessions.Store(key, sessionID)
	log.Info().Str("group", groupID).Str("token", pkg.SafePrefix(token, 10)).Str("session", sessionID[:8]).Msg("Created agent session for group")
	return sessionID
}

// startConsumer reads from the group inbox stream and fans out to all WS connections.
func (gc *GroupChatManager) startConsumer(ctx context.Context, groupID string) {
	key := GroupInboxPrefix + groupID
	lastID := "$" // Only new messages

	log.Info().Str("group", groupID).Msg("Group consumer started")
	defer log.Info().Str("group", groupID).Msg("Group consumer stopped")

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		results, err := gc.rdb.XRead(ctx, &redis.XReadArgs{
			Streams: []string{key, lastID},
			Block:   5 * time.Second,
			Count:   50,
		}).Result()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if err == redis.Nil {
				continue
			}
			log.Error().Err(err).Str("group", groupID).Msg("Group consumer read error")
			time.Sleep(time.Second)
			continue
		}

		for _, stream := range results {
			for _, msg := range stream.Messages {
				lastID = msg.ID
				dataStr, _ := msg.Values["data"].(string)
				if dataStr == "" {
					continue
				}

				var event map[string]interface{}
				if err := json.Unmarshal([]byte(dataStr), &event); err != nil {
					continue
				}

				gc.fanOutToConns(groupID, event)
			}
		}
	}
}

// fanOutToConns sends an event to all WS connections for a group.
func (gc *GroupChatManager) fanOutToConns(groupID string, event map[string]interface{}) {
	val, ok := gc.conns.Load(groupID)
	if !ok {
		return
	}
	connMap := val.(*sync.Map)
	connMap.Range(func(_, v interface{}) bool {
		conn := v.(*GroupWSConn)
		if err := conn.WriteJSON(event); err != nil {
			log.Debug().Err(err).Str("group", groupID).Str("conn", conn.ID).Msg("Failed to write to group WS conn")
		}
		return true
	})
}

// Shutdown closes all consumers and connections.
func (gc *GroupChatManager) Shutdown() {
	gc.consumers.Range(func(key, value interface{}) bool {
		cancel := value.(context.CancelFunc)
		cancel()
		gc.consumers.Delete(key)
		return true
	})

	gc.conns.Range(func(key, value interface{}) bool {
		connMap := value.(*sync.Map)
		connMap.Range(func(_, v interface{}) bool {
			conn := v.(*GroupWSConn)
			_ = conn.Conn.Close()
			return true
		})
		gc.conns.Delete(key)
		return true
	})

	log.Info().Msg("GroupChatManager shut down")
}
