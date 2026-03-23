package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
	"gorm.io/gorm"

	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/pkg"
)

// NeverExpires is the maximum MySQL DATETIME — used for tokens with no expiry.
var NeverExpires = time.Date(9999, 12, 31, 23, 59, 59, 0, time.UTC)

// TokenManager manages API tokens with MySQL storage.
type TokenManager struct {
	db  *gorm.DB
	rdb redis.UniversalClient
}

// NewTokenManager creates a new TokenManager.
func NewTokenManager(db *gorm.DB, rdb redis.UniversalClient) *TokenManager {
	return &TokenManager{db: db, rdb: rdb}
}

// Generate creates a new token with the given name and expiry.
func (m *TokenManager) Generate(ctx context.Context, name string, expiresIn int) (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate random: %w", err)
	}
	tokenValue := "sk-" + hex.EncodeToString(b)
	now := time.Now().UTC()
	expiresAt := NeverExpires
	if expiresIn > 0 {
		expiresAt = now.Add(time.Duration(expiresIn) * time.Second)
	}

	token := model.Token{
		Token:     tokenValue,
		CreatedAt: now,
		Name:      name,
		ExpiresAt: expiresAt,
	}
	if err := m.db.WithContext(ctx).Create(&token).Error; err != nil {
		return "", fmt.Errorf("create token: %w", err)
	}

	log.Info().Str("token", pkg.SafePrefix(tokenValue, 16)).Str("name", name).Int("expires_in", expiresIn).Msg("Token generated")
	return tokenValue, nil
}

// Validate checks if a token is valid and not expired.
func (m *TokenManager) Validate(ctx context.Context, token string) bool {
	if token == "" {
		return false
	}

	var count int64
	if err := m.db.WithContext(ctx).Model(&model.Token{}).
		Where("token = ? AND expires_at >= ?", token, time.Now().UTC()).
		Count(&count).Error; err != nil {
		log.Error().Err(err).Msg("Token validation DB error")
		return false
	}

	if count > 0 {
		log.Debug().Str("token", pkg.SafePrefix(token, 10)).Msg("Token validated")
		return true
	}
	log.Debug().Str("token", pkg.SafePrefix(token, 10)).Msg("Token validation failed")
	return false
}

// Update modifies a token's name and/or expiry.
func (m *TokenManager) Update(ctx context.Context, tokenValue string, name *string, expiresIn *int) (bool, error) {
	var token model.Token
	if err := m.db.WithContext(ctx).Where("token = ?", tokenValue).First(&token).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			log.Warn().Str("token", pkg.SafePrefix(tokenValue, 16)).Msg("Token update failed: not found")
			return false, nil
		}
		return false, err
	}

	updates := map[string]interface{}{}
	if name != nil {
		updates["name"] = *name
	}
	if expiresIn != nil {
		if *expiresIn == 0 {
			updates["expires_at"] = NeverExpires
		} else {
			updates["expires_at"] = time.Now().UTC().Add(time.Duration(*expiresIn) * time.Second)
		}
	}

	if len(updates) > 0 {
		if err := m.db.WithContext(ctx).Model(&token).Updates(updates).Error; err != nil {
			return false, err
		}
	}
	return true, nil
}

// Remove deletes a token and invalidates its auth cache.
func (m *TokenManager) Remove(ctx context.Context, tokenValue string) error {
	if err := m.db.WithContext(ctx).Where("token = ?", tokenValue).Delete(&model.Token{}).Error; err != nil {
		return fmt.Errorf("delete token: %w", err)
	}
	// Invalidate token auth cache
	m.rdb.Del(ctx, "token_auth:"+tokenValue)
	log.Info().Str("token", pkg.SafePrefix(tokenValue, 16)).Msg("Token removed")
	return nil
}

// ListAll returns a paginated list of non-expired tokens.
func (m *TokenManager) ListAll(ctx context.Context, page, pageSize int, search string) (*TokenListResult, error) {
	now := time.Now().UTC()
	query := m.db.WithContext(ctx).Model(&model.Token{}).Where("expires_at >= ?", now)
	if search != "" {
		escaped := strings.NewReplacer("%", "\\%", "_", "\\_").Replace(search)
		query = query.Where("token LIKE ?", "%"+escaped+"%")
	}

	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, err
	}

	var rows []model.Token
	offset := (page - 1) * pageSize
	if err := query.Order("created_at DESC").Limit(pageSize).Offset(offset).Find(&rows).Error; err != nil {
		return nil, err
	}

	items := make([]TokenItem, len(rows))
	for i, row := range rows {
		items[i] = TokenItem{
			Token:     row.Token,
			CreatedAt: toTimestamp(row.CreatedAt),
			Name:      row.Name,
			ExpiresAt: toTimestamp(row.ExpiresAt),
		}
	}

	return &TokenListResult{
		Items:    items,
		Total:    int(total),
		Page:     page,
		PageSize: pageSize,
	}, nil
}

// ListPaged returns a paginated list of non-expired tokens with DB-level sort and pagination.
// sortBy must be one of: "created_at", "name". sortOrder must be "asc" or "desc".
func (m *TokenManager) ListPaged(ctx context.Context, page, pageSize int, search, sortBy, sortOrder string) (*TokenListResult, error) {
	now := time.Now().UTC()
	query := m.db.WithContext(ctx).Model(&model.Token{}).Where("expires_at >= ?", now)
	if search != "" {
		escaped := strings.NewReplacer("%", "\\%", "_", "\\_").Replace(search)
		query = query.Where("token LIKE ?", "%"+escaped+"%")
	}

	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, err
	}

	// Validate and apply sort
	orderCol := "created_at"
	if sortBy == "name" {
		orderCol = "name"
	}
	direction := "DESC"
	if sortOrder == "asc" {
		direction = "ASC"
	}
	orderClause := orderCol + " " + direction

	var rows []model.Token
	offset := (page - 1) * pageSize
	if err := query.Order(orderClause).Limit(pageSize).Offset(offset).Find(&rows).Error; err != nil {
		return nil, err
	}

	items := make([]TokenItem, len(rows))
	for i, row := range rows {
		items[i] = TokenItem{
			Token:     row.Token,
			CreatedAt: toTimestamp(row.CreatedAt),
			Name:      row.Name,
			ExpiresAt: toTimestamp(row.ExpiresAt),
		}
	}

	return &TokenListResult{
		Items:    items,
		Total:    int(total),
		Page:     page,
		PageSize: pageSize,
	}, nil
}

// BulkCheckBotOnline checks online status for multiple tokens using Pipeline ZScore.
func (m *TokenManager) BulkCheckBotOnline(ctx context.Context, tokens []string) (map[string]bool, error) {
	if len(tokens) == 0 {
		return make(map[string]bool), nil
	}
	cutoff := float64(time.Now().Unix()) - 30 // BotTTL = 30s

	pipe := m.rdb.Pipeline()
	cmds := make([]*redis.FloatCmd, len(tokens))
	for i, token := range tokens {
		cmds[i] = pipe.ZScore(ctx, "bridge:bot_alive", token)
	}
	_, _ = pipe.Exec(ctx) // individual cmds carry their own errors

	result := make(map[string]bool, len(tokens))
	for i, cmd := range cmds {
		score, err := cmd.Result()
		if err == nil && score > cutoff {
			result[tokens[i]] = true
		}
	}
	return result, nil
}

// CountOnlineBots returns the total number of online bots.
func (m *TokenManager) CountOnlineBots(ctx context.Context) int {
	cutoff := float64(time.Now().Unix()) - 30 // BotTTL = 30s
	count, err := m.rdb.ZCount(ctx, "bridge:bot_alive", fmt.Sprintf("%f", cutoff), "+inf").Result()
	if err != nil {
		log.Warn().Err(err).Msg("Failed to count online bots")
		return 0
	}
	return int(count)
}

// CountAllActive returns the total number of non-expired tokens.
func (m *TokenManager) CountAllActive(ctx context.Context) int {
	var count int64
	m.db.WithContext(ctx).Model(&model.Token{}).Where("expires_at >= ?", time.Now().UTC()).Count(&count)
	return int(count)
}

// GetName returns the name associated with a token.
func (m *TokenManager) GetName(ctx context.Context, token string) string {
	var t model.Token
	if err := m.db.WithContext(ctx).Select("name").Where("token = ?", token).First(&t).Error; err != nil {
		return ""
	}
	return t.Name
}

// CleanupExpired removes all expired tokens and returns the count.
func (m *TokenManager) CleanupExpired(ctx context.Context) (int, error) {
	result := m.db.WithContext(ctx).Where("expires_at < ?", time.Now().UTC()).Delete(&model.Token{})
	if result.Error != nil {
		return 0, result.Error
	}
	count := int(result.RowsAffected)
	if count > 0 {
		log.Info().Int("count", count).Msg("Cleaned up expired tokens")
	}
	return count, nil
}

// TokenItem represents a token in list results.
type TokenItem struct {
	Token     string  `json:"token"`
	CreatedAt float64 `json:"created_at"`
	Name      string  `json:"name"`
	ExpiresAt float64 `json:"expires_at"`
}

// TokenListResult represents paginated token list results.
type TokenListResult struct {
	Items    []TokenItem `json:"items"`
	Total    int         `json:"total"`
	Page     int         `json:"page"`
	PageSize int         `json:"page_size"`
}

func toTimestamp(t time.Time) float64 {
	if t.IsZero() {
		return 0
	}
	if t.Location() == nil || t.Location() == time.Local {
		t = t.UTC()
	}
	return float64(t.Unix()) + float64(t.Nanosecond())/1e9
}
