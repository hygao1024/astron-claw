package router

import (
	"context"
	"sort"
	"strconv"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/middleware"
	"astron-claw/backend/internal/model"
)

func (app *App) listTokens(c *gin.Context) {
	ctx := c.Request.Context()

	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("page_size", "20"))
	search := c.DefaultQuery("search", "")
	sortBy := c.DefaultQuery("sort_by", "created_at")
	sortOrder := c.DefaultQuery("sort_order", "desc")
	botStatus := c.DefaultQuery("bot_status", "")

	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 20
	}
	if pageSize > 100 {
		pageSize = 100
	}

	// bot_online sort is handled in-memory since it requires Redis data;
	// for created_at/name sort, push to DB directly.
	if sortBy == "bot_online" {
		app.listTokensWithBotSort(c, ctx, page, pageSize, search, sortOrder, botStatus)
		return
	}

	data, err := app.TokenMgr.ListPaged(ctx, page, pageSize, search, sortBy, sortOrder)
	if err != nil {
		log.Error().Err(err).Msg("Failed to list tokens")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	// Collect raw tokens for bulk online check
	rawTokens := make([]string, len(data.Items))
	for i, t := range data.Items {
		rawTokens[i] = t.Token
	}
	onlineMap, _ := app.TokenMgr.BulkCheckBotOnline(ctx, rawTokens)

	type tokenInfo struct {
		Token     string  `json:"token"`
		Name      string  `json:"name"`
		CreatedAt float64 `json:"created_at"`
		ExpiresAt float64 `json:"expires_at"`
		BotOnline bool    `json:"bot_online"`
	}

	pageItems := make([]tokenInfo, len(data.Items))
	for i, t := range data.Items {
		pageItems[i] = tokenInfo{
			Token:     t.Token,
			Name:      t.Name,
			CreatedAt: t.CreatedAt,
			ExpiresAt: t.ExpiresAt,
			BotOnline: onlineMap[t.Token],
		}
	}

	// Filter by bot status (post-query filter — only applies to current page)
	if botStatus == "online" {
		filtered := make([]tokenInfo, 0)
		for _, t := range pageItems {
			if t.BotOnline {
				filtered = append(filtered, t)
			}
		}
		pageItems = filtered
	}

	globalOnline := app.TokenMgr.CountOnlineBots(ctx)
	totalTokens := app.TokenMgr.CountAllActive(ctx)

	c.JSON(200, gin.H{
		"code":         0,
		"tokens":       pageItems,
		"total":        data.Total,
		"page":         page,
		"page_size":    pageSize,
		"online_bots":  globalOnline,
		"total_tokens": totalTokens,
	})
}

// listTokensWithBotSort handles the special case of sorting by bot_online status.
// This requires loading all tokens to merge with Redis data.
func (app *App) listTokensWithBotSort(c *gin.Context, ctx context.Context, page, pageSize int, search, sortOrder, botStatus string) {
	data, err := app.TokenMgr.ListPaged(ctx, 1, 5000, search, "created_at", "desc")
	if err != nil {
		log.Error().Err(err).Msg("Failed to list tokens for bot sort")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	rawTokens := make([]string, len(data.Items))
	for i, t := range data.Items {
		rawTokens[i] = t.Token
	}
	onlineMap, _ := app.TokenMgr.BulkCheckBotOnline(ctx, rawTokens)

	type tokenInfo struct {
		Token     string  `json:"token"`
		Name      string  `json:"name"`
		CreatedAt float64 `json:"created_at"`
		ExpiresAt float64 `json:"expires_at"`
		BotOnline bool    `json:"bot_online"`
	}

	allTokens := make([]tokenInfo, len(data.Items))
	for i, t := range data.Items {
		allTokens[i] = tokenInfo{
			Token:     t.Token,
			Name:      t.Name,
			CreatedAt: t.CreatedAt,
			ExpiresAt: t.ExpiresAt,
			BotOnline: onlineMap[t.Token],
		}
	}

	globalOnline := 0
	for _, t := range allTokens {
		if t.BotOnline {
			globalOnline++
		}
	}

	filtered := allTokens
	if botStatus == "online" {
		f := make([]tokenInfo, 0)
		for _, t := range filtered {
			if t.BotOnline {
				f = append(f, t)
			}
		}
		filtered = f
	}

	reverse := sortOrder == "desc"
	sort.Slice(filtered, func(i, j int) bool {
		if filtered[i].BotOnline != filtered[j].BotOnline {
			if reverse {
				return filtered[i].BotOnline
			}
			return filtered[j].BotOnline
		}
		if reverse {
			return filtered[i].CreatedAt > filtered[j].CreatedAt
		}
		return filtered[i].CreatedAt < filtered[j].CreatedAt
	})

	total := len(filtered)
	offset := (page - 1) * pageSize
	end := offset + pageSize
	if offset > total {
		offset = total
	}
	if end > total {
		end = total
	}
	pageItems := filtered[offset:end]

	c.JSON(200, gin.H{
		"code":         0,
		"tokens":       pageItems,
		"total":        total,
		"page":         page,
		"page_size":    pageSize,
		"online_bots":  globalOnline,
		"total_tokens": len(allTokens),
	})
}

func (app *App) adminCreateToken(c *gin.Context) {
	var body struct {
		Name      string `json:"name"`
		ExpiresIn int    `json:"expires_in"`
	}
	body.ExpiresIn = 86400 // default
	if err := c.ShouldBindJSON(&body); err != nil {
		// Allow empty body
	}

	token, err := app.TokenMgr.Generate(c.Request.Context(), body.Name, body.ExpiresIn)
	if err != nil {
		log.Error().Err(err).Msg("Failed to create token")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	log.Info().Str("token", token).Str("name", body.Name).Msg("Admin created token")
	c.JSON(200, gin.H{"code": 0, "token": token})
}

func (app *App) adminDeleteToken(c *gin.Context) {
	tokenValue := c.Param("token")

	if err := app.TokenMgr.Remove(c.Request.Context(), tokenValue); err != nil {
		log.Error().Err(err).Msg("Failed to delete token")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	if err := app.Bridge.RemoveBotSessions(c.Request.Context(), tokenValue); err != nil {
		log.Error().Err(err).Str("token", tokenValue).Msg("Failed to remove bot sessions")
		// Don't fail the delete - token is already removed
	}
	middleware.InvalidateTokenCache(c.Request.Context(), app.RDB, tokenValue)
	log.Info().Str("token", tokenValue).Msg("Admin deleted token")
	c.JSON(200, gin.H{"code": 0})
}

func (app *App) adminUpdateToken(c *gin.Context) {
	tokenValue := c.Param("token")

	var body map[string]interface{}
	if err := c.ShouldBindJSON(&body); err != nil {
		middleware.MetricsErrorResponse(c, model.ErrChatInvalidReq)
		return
	}

	var name *string
	var expiresIn *int
	if v, ok := body["name"].(string); ok {
		name = &v
	}
	if v, ok := body["expires_in"].(float64); ok {
		ei := int(v)
		expiresIn = &ei
	}

	found, err := app.TokenMgr.Update(c.Request.Context(), tokenValue, name, expiresIn)
	if err != nil {
		log.Error().Err(err).Msg("Failed to update token")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	if !found {
		middleware.MetricsErrorResponse(c, model.ErrTokenNotFound)
		return
	}
	log.Info().Str("token", tokenValue).Msg("Admin updated token")
	c.JSON(200, gin.H{"code": 0})
}

func (app *App) adminCleanup(c *gin.Context) {
	ctx := c.Request.Context()

	tokenCount, err := app.TokenMgr.CleanupExpired(ctx)
	if err != nil {
		log.Error().Err(err).Msg("Failed to cleanup expired tokens")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	sessionCount, err := app.Bridge.CleanupOldSessions(ctx, 30)
	if err != nil {
		log.Error().Err(err).Msg("Failed to cleanup old sessions")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	log.Info().Int("tokens", tokenCount).Int("sessions", sessionCount).Msg("Admin cleanup")
	c.JSON(200, gin.H{"code": 0, "removed_tokens": tokenCount, "removed_sessions": sessionCount})
}
