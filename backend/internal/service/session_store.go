package service

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"gorm.io/gorm"

	"astron-claw/backend/internal/model"
)

var sessionTracer = otel.Tracer("astron-claw/service/session")

const (
	sessionsPrefix  = "bridge:sessions:"
	sessionCacheTTL = 3600 * time.Second // 1 hour
)

// SessionStore manages chat sessions with MySQL persistence and Redis caching.
type SessionStore struct {
	db  *gorm.DB
	rdb redis.UniversalClient
}

// NewSessionStore creates a new SessionStore.
func NewSessionStore(db *gorm.DB, rdb redis.UniversalClient) *SessionStore {
	return &SessionStore{db: db, rdb: rdb}
}

// CreateSession persists a new session to MySQL and caches in Redis.
// New sessions start with session_number = 1.
func (s *SessionStore) CreateSession(ctx context.Context, token, sessionID string) (int, error) {
	ctx, span := sessionTracer.Start(ctx, "session.create", trace.WithSpanKind(trace.SpanKindInternal))
	defer span.End()
	span.SetAttributes(
		attribute.String("astron.token_id", token),
		attribute.String("astron.session_id", sessionID),
	)

	now := time.Now().UTC()
	const initialNumber = 1

	session := model.ChatSession{
		Token:         token,
		SessionID:     sessionID,
		SessionNumber: initialNumber,
		CreatedAt:     now,
	}
	if err := s.db.WithContext(ctx).Create(&session).Error; err != nil {
		span.SetStatus(codes.Error, "create session failed")
		span.RecordError(err)
		return 0, fmt.Errorf("create session: %w", err)
	}

	// Invalidate Redis cache so next GetSessions rebuilds from DB
	s.rdb.Del(ctx, sessionsPrefix+token)
	return initialNumber, nil
}

// RemoveSessions deletes all session data for a token.
func (s *SessionStore) RemoveSessions(ctx context.Context, token string) error {
	ctx, span := sessionTracer.Start(ctx, "session.remove", trace.WithSpanKind(trace.SpanKindInternal))
	defer span.End()
	span.SetAttributes(attribute.String("astron.token_id", token))
	if err := s.db.WithContext(ctx).Where("token = ?", token).Delete(&model.ChatSession{}).Error; err != nil {
		return fmt.Errorf("delete sessions: %w", err)
	}
	if err := s.rdb.Del(ctx, sessionsPrefix+token).Err(); err != nil {
		log.Warn().Err(err).Str("token", token).Msg("Redis cache delete failed for remove_sessions")
	}
	return nil
}

// GetSession returns (sessionID, sessionNumber) if it belongs to token.
func (s *SessionStore) GetSession(ctx context.Context, token, sessionID string) (string, int, bool) {
	ctx, span := sessionTracer.Start(ctx, "session.get", trace.WithSpanKind(trace.SpanKindInternal))
	defer span.End()
	span.SetAttributes(
		attribute.String("astron.token_id", token),
		attribute.String("astron.session_id", sessionID),
	)
	var session model.ChatSession
	err := s.db.WithContext(ctx).
		Where("token = ? AND session_id = ?", token, sessionID).
		First(&session).Error
	found := err == nil
	span.SetAttributes(attribute.Bool("astron.found", found))
	if err != nil {
		return "", 0, false
	}
	return session.SessionID, session.SessionNumber, true
}

// SessionInfo holds session list data.
type SessionInfo struct {
	ID     string `json:"id"`
	Number int    `json:"number"`
}

// GetSessions returns all sessions for a token.
func (s *SessionStore) GetSessions(ctx context.Context, token string) ([]SessionInfo, error) {
	ctx, span := sessionTracer.Start(ctx, "session.list", trace.WithSpanKind(trace.SpanKindInternal))
	defer span.End()
	span.SetAttributes(attribute.String("astron.token_id", token))
	// Try Redis first
	sessionsKey := sessionsPrefix + token
	cached, err := s.rdb.LRange(ctx, sessionsKey, 0, -1).Result()
	if err == nil && len(cached) > 0 {
		sessions := make([]SessionInfo, 0, len(cached))
		for i, sessionID := range cached {
			sessions = append(sessions, SessionInfo{
				ID:     sessionID,
				Number: i + 1,
			})
		}
		return sessions, nil
	}

	// Cache miss — query MySQL
	var rows []model.ChatSession
	err = s.db.WithContext(ctx).
		Where("token = ?", token).
		Order("session_number").
		Find(&rows).Error
	if err != nil {
		return nil, fmt.Errorf("query sessions: %w", err)
	}

	if len(rows) == 0 {
		return nil, nil
	}

	sessions := make([]SessionInfo, len(rows))
	for i, r := range rows {
		sessions[i] = SessionInfo{ID: r.SessionID, Number: r.SessionNumber}
	}

	// Repopulate cache
	s.repopulateCache(ctx, token, rows)

	return sessions, nil
}

// CleanupOldSessions deletes sessions older than maxAgeSeconds.
func (s *SessionStore) CleanupOldSessions(ctx context.Context, maxAgeSeconds float64) (int, error) {
	cutoff := time.Now().UTC().Add(-time.Duration(maxAgeSeconds) * time.Second)

	// Get affected tokens first
	var oldSessions []model.ChatSession
	if err := s.db.WithContext(ctx).
		Where("created_at < ?", cutoff).
		Find(&oldSessions).Error; err != nil {
		return 0, err
	}
	if len(oldSessions) == 0 {
		return 0, nil
	}

	// Delete
	result := s.db.WithContext(ctx).Where("created_at < ?", cutoff).Delete(&model.ChatSession{})
	if result.Error != nil {
		return 0, result.Error
	}

	// Invalidate affected tokens' caches
	affected := make(map[string]struct{})
	for _, sess := range oldSessions {
		affected[sess.Token] = struct{}{}
	}
	for token := range affected {
		if err := s.rdb.Del(ctx, sessionsPrefix+token).Err(); err != nil {
			log.Warn().Err(err).Str("token", token).Msg("Redis cache invalidation failed during session cleanup")
		}
	}

	count := len(oldSessions)
	log.Info().Int("count", count).Time("cutoff", cutoff).Msg("Cleaned up old sessions")
	return count, nil
}

func (s *SessionStore) repopulateCache(ctx context.Context, token string, rows []model.ChatSession) {
	sessionsKey := sessionsPrefix + token
	pipe := s.rdb.Pipeline()
	pipe.Del(ctx, sessionsKey)
	if len(rows) > 0 {
		sids := make([]interface{}, len(rows))
		for i, r := range rows {
			sids[i] = r.SessionID
		}
		pipe.RPush(ctx, sessionsKey, sids...)
		pipe.Expire(ctx, sessionsKey, sessionCacheTTL)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		log.Warn().Err(err).Str("token", token).Msg("Redis cache repopulate failed")
	}
}
