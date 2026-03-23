package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
	"gorm.io/gorm"

	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/pkg"
)

// GroupManager manages group CRUD with MySQL storage.
type GroupManager struct {
	db       *gorm.DB
	rdb      redis.UniversalClient
	tokenMgr *TokenManager
}

// NewGroupManager creates a new GroupManager.
func NewGroupManager(db *gorm.DB, rdb redis.UniversalClient, tokenMgr *TokenManager) *GroupManager {
	return &GroupManager{db: db, rdb: rdb, tokenMgr: tokenMgr}
}

// Create creates a new group.
func (m *GroupManager) Create(ctx context.Context, name, description string) (*model.Group, error) {
	now := time.Now().UTC()
	group := model.Group{
		GroupID:     uuid.New().String(),
		Name:        name,
		Description: description,
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := m.db.WithContext(ctx).Create(&group).Error; err != nil {
		return nil, fmt.Errorf("create group: %w", err)
	}
	log.Info().Str("group_id", group.GroupID).Str("name", name).Msg("Group created")
	return &group, nil
}

// Get returns a group by its group_id.
func (m *GroupManager) Get(ctx context.Context, groupID string) (*model.Group, error) {
	var group model.Group
	if err := m.db.WithContext(ctx).Where("group_id = ?", groupID).First(&group).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			return nil, nil
		}
		return nil, err
	}
	return &group, nil
}

// GroupListItem represents a group in list results with agent count.
type GroupListItem struct {
	GroupID     string  `json:"group_id"`
	Name        string  `json:"name"`
	Description string  `json:"description"`
	AgentCount  int     `json:"agent_count"`
	CreatedAt   float64 `json:"created_at"`
	UpdatedAt   float64 `json:"updated_at"`
}

// GroupListResult represents paginated group list results.
type GroupListResult struct {
	Items    []GroupListItem `json:"items"`
	Total    int             `json:"total"`
	Page     int             `json:"page"`
	PageSize int             `json:"page_size"`
}

// List returns a paginated list of groups with agent count.
func (m *GroupManager) List(ctx context.Context, page, pageSize int) (*GroupListResult, error) {
	var total int64
	if err := m.db.WithContext(ctx).Model(&model.Group{}).Count(&total).Error; err != nil {
		return nil, err
	}

	var rows []model.Group
	offset := (page - 1) * pageSize
	if err := m.db.WithContext(ctx).Order("created_at DESC").Limit(pageSize).Offset(offset).Find(&rows).Error; err != nil {
		return nil, err
	}

	items := make([]GroupListItem, len(rows))
	for i, row := range rows {
		var count int64
		m.db.WithContext(ctx).Model(&model.GroupAgent{}).Where("group_id = ?", row.GroupID).Count(&count)
		items[i] = GroupListItem{
			GroupID:     row.GroupID,
			Name:        row.Name,
			Description: row.Description,
			AgentCount:  int(count),
			CreatedAt:   toTimestamp(row.CreatedAt),
			UpdatedAt:   toTimestamp(row.UpdatedAt),
		}
	}

	return &GroupListResult{
		Items:    items,
		Total:    int(total),
		Page:     page,
		PageSize: pageSize,
	}, nil
}

// Update modifies a group's name and/or description.
func (m *GroupManager) Update(ctx context.Context, groupID string, name, description *string) error {
	var group model.Group
	if err := m.db.WithContext(ctx).Where("group_id = ?", groupID).First(&group).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			return fmt.Errorf("not found")
		}
		return err
	}

	updates := map[string]interface{}{"updated_at": time.Now().UTC()}
	if name != nil {
		updates["name"] = *name
	}
	if description != nil {
		updates["description"] = *description
	}

	if err := m.db.WithContext(ctx).Model(&group).Updates(updates).Error; err != nil {
		return err
	}
	log.Info().Str("group_id", groupID).Msg("Group updated")
	return nil
}

// Delete removes a group and all its agents in a transaction.
func (m *GroupManager) Delete(ctx context.Context, groupID string) error {
	return m.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("group_id = ?", groupID).Delete(&model.GroupAgent{}).Error; err != nil {
			return err
		}
		result := tx.Where("group_id = ?", groupID).Delete(&model.Group{})
		if result.Error != nil {
			return result.Error
		}
		if result.RowsAffected == 0 {
			return fmt.Errorf("not found")
		}
		return nil
	})
}

// AddAgent adds a token to a group with an optional role (default "member").
func (m *GroupManager) AddAgent(ctx context.Context, groupID, token, role string) error {
	if role == "" {
		role = "member"
	}
	if role != "leader" && role != "member" {
		return fmt.Errorf("invalid role")
	}

	// Verify group exists
	var count int64
	if err := m.db.WithContext(ctx).Model(&model.Group{}).Where("group_id = ?", groupID).Count(&count).Error; err != nil {
		return err
	}
	if count == 0 {
		return fmt.Errorf("group not found")
	}

	// Verify token exists
	if !m.tokenMgr.Validate(ctx, token) {
		return fmt.Errorf("invalid token")
	}

	// If setting as leader, demote existing leader in a transaction
	if role == "leader" {
		if err := m.db.WithContext(ctx).Model(&model.GroupAgent{}).
			Where("group_id = ? AND role = ?", groupID, "leader").
			Update("role", "member").Error; err != nil {
			return err
		}
	}

	agent := model.GroupAgent{
		GroupID: groupID,
		Token:   token,
		Role:    role,
		AddedAt: time.Now().UTC(),
	}
	if err := m.db.WithContext(ctx).Create(&agent).Error; err != nil {
		if isDuplicateEntry(err) {
			return fmt.Errorf("duplicate")
		}
		return err
	}
	log.Info().Str("group_id", groupID).Str("token", pkg.SafePrefix(token, 10)).Str("role", role).Msg("Agent added to group")
	return nil
}

// UpdateAgentRole changes the role of an agent in a group.
// When setting to leader, any existing leader is demoted to member.
func (m *GroupManager) UpdateAgentRole(ctx context.Context, groupID, token, role string) error {
	if role != "leader" && role != "member" {
		return fmt.Errorf("invalid role")
	}

	return m.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		// Check agent exists in group
		var agent model.GroupAgent
		if err := tx.Where("group_id = ? AND token = ?", groupID, token).First(&agent).Error; err != nil {
			if err == gorm.ErrRecordNotFound {
				return fmt.Errorf("not found")
			}
			return err
		}

		// Demote existing leader if promoting
		if role == "leader" {
			if err := tx.Model(&model.GroupAgent{}).
				Where("group_id = ? AND role = ?", groupID, "leader").
				Update("role", "member").Error; err != nil {
				return err
			}
		}

		if err := tx.Model(&agent).Update("role", role).Error; err != nil {
			return err
		}

		log.Info().Str("group_id", groupID).Str("token", pkg.SafePrefix(token, 10)).Str("role", role).Msg("Agent role updated")
		return nil
	})
}

// GetLeaderToken returns the token of the leader agent in a group.
func (m *GroupManager) GetLeaderToken(ctx context.Context, groupID string) (string, error) {
	var agent model.GroupAgent
	if err := m.db.WithContext(ctx).
		Where("group_id = ? AND role = ?", groupID, "leader").
		First(&agent).Error; err != nil {
		if err == gorm.ErrRecordNotFound {
			return "", fmt.Errorf("no leader")
		}
		return "", err
	}
	return agent.Token, nil
}

// IsLeader checks if a token is the leader of a group.
func (m *GroupManager) IsLeader(ctx context.Context, groupID, token string) bool {
	var count int64
	m.db.WithContext(ctx).Model(&model.GroupAgent{}).
		Where("group_id = ? AND token = ? AND role = ?", groupID, token, "leader").
		Count(&count)
	return count > 0
}

// GetAgentTokenByName resolves an agent name to its token within a group.
func (m *GroupManager) GetAgentTokenByName(ctx context.Context, groupID, name string) (string, error) {
	type row struct {
		Token string
	}
	var r row
	err := m.db.WithContext(ctx).
		Table("group_agents").
		Select("group_agents.token").
		Joins("LEFT JOIN tokens ON group_agents.token = tokens.token COLLATE utf8mb4_unicode_ci").
		Where("group_agents.group_id = ? AND tokens.name = ?", groupID, name).
		Scan(&r).Error
	if err != nil {
		return "", err
	}
	if r.Token == "" {
		return "", fmt.Errorf("agent not found by name: %s", name)
	}
	return r.Token, nil
}

// RemoveAgent removes a token from a group.
func (m *GroupManager) RemoveAgent(ctx context.Context, groupID, token string) error {
	result := m.db.WithContext(ctx).Where("group_id = ? AND token = ?", groupID, token).Delete(&model.GroupAgent{})
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected == 0 {
		return fmt.Errorf("not found")
	}
	log.Info().Str("group_id", groupID).Str("token", pkg.SafePrefix(token, 10)).Msg("Agent removed from group")
	return nil
}

// GroupAgentInfo represents an agent in a group with online status.
type GroupAgentInfo struct {
	Token     string  `json:"token"`
	Name      string  `json:"name"`
	Role      string  `json:"role"`
	BotOnline bool    `json:"bot_online"`
	AddedAt   float64 `json:"added_at"`
}

// GetAgents returns all agents in a group with name and online status.
func (m *GroupManager) GetAgents(ctx context.Context, groupID string) ([]GroupAgentInfo, error) {
	type row struct {
		Token   string
		Name    string
		Role    string
		AddedAt time.Time
	}
	var rows []row
	err := m.db.WithContext(ctx).
		Table("group_agents").
		Select("group_agents.token, tokens.name, group_agents.role, group_agents.added_at").
		Joins("LEFT JOIN tokens ON group_agents.token = tokens.token COLLATE utf8mb4_unicode_ci").
		Where("group_agents.group_id = ?", groupID).
		Order("group_agents.added_at ASC").
		Scan(&rows).Error
	if err != nil {
		return nil, err
	}

	tokens := make([]string, len(rows))
	for i, r := range rows {
		tokens[i] = r.Token
	}
	onlineMap, _ := m.tokenMgr.BulkCheckBotOnline(ctx, tokens)

	agents := make([]GroupAgentInfo, len(rows))
	for i, r := range rows {
		agents[i] = GroupAgentInfo{
			Token:     r.Token,
			Name:      r.Name,
			Role:      r.Role,
			BotOnline: onlineMap[r.Token],
			AddedAt:   toTimestamp(r.AddedAt),
		}
	}
	return agents, nil
}

// GetAgentTokens returns only the token strings for a group (used by message routing).
func (m *GroupManager) GetAgentTokens(ctx context.Context, groupID string) ([]string, error) {
	var tokens []string
	err := m.db.WithContext(ctx).Model(&model.GroupAgent{}).
		Where("group_id = ?", groupID).
		Pluck("token", &tokens).Error
	return tokens, err
}

// RemoveAgentByToken removes a token from all groups (cascade on token delete).
func (m *GroupManager) RemoveAgentByToken(ctx context.Context, token string) error {
	result := m.db.WithContext(ctx).Where("token = ?", token).Delete(&model.GroupAgent{})
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected > 0 {
		log.Info().Str("token", pkg.SafePrefix(token, 10)).Int64("count", result.RowsAffected).Msg("Agent removed from all groups")
	}
	return nil
}

// isDuplicateEntry checks if a GORM error is a MySQL duplicate entry error (code 1062).
func isDuplicateEntry(err error) bool {
	var mysqlErr *mysql.MySQLError
	if errors.As(err, &mysqlErr) {
		return mysqlErr.Number == 1062
	}
	return false
}
