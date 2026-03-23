package router

import (
	"strconv"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/model"
)

func (app *App) listGroups(c *gin.Context) {
	ctx := c.Request.Context()
	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("page_size", "20"))
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 20
	}
	if pageSize > 100 {
		pageSize = 100
	}

	data, err := app.GroupMgr.List(ctx, page, pageSize)
	if err != nil {
		log.Error().Err(err).Msg("Failed to list groups")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}

	c.JSON(200, gin.H{
		"code":      0,
		"groups":    data.Items,
		"total":     data.Total,
		"page":      data.Page,
		"page_size": data.PageSize,
	})
}

func (app *App) createGroup(c *gin.Context) {
	var body struct {
		Name        string `json:"name"`
		Description string `json:"description"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		model.ErrorResponse(c, model.ErrGroupInvalidReq)
		return
	}

	group, err := app.GroupMgr.Create(c.Request.Context(), body.Name, body.Description)
	if err != nil {
		log.Error().Err(err).Msg("Failed to create group")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}

	c.JSON(200, gin.H{"code": 0, "group": group})
}

func (app *App) getGroup(c *gin.Context) {
	ctx := c.Request.Context()
	groupID := c.Param("groupId")

	group, err := app.GroupMgr.Get(ctx, groupID)
	if err != nil {
		log.Error().Err(err).Msg("Failed to get group")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}
	if group == nil {
		model.ErrorResponse(c, model.ErrGroupNotFound)
		return
	}

	agents, err := app.GroupMgr.GetAgents(ctx, groupID)
	if err != nil {
		log.Error().Err(err).Msg("Failed to get group agents")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}

	c.JSON(200, gin.H{"code": 0, "group": group, "agents": agents})
}

func (app *App) updateGroup(c *gin.Context) {
	groupID := c.Param("groupId")

	var body map[string]interface{}
	if err := c.ShouldBindJSON(&body); err != nil {
		model.ErrorResponse(c, model.ErrGroupInvalidReq)
		return
	}

	var name, description *string
	if v, ok := body["name"].(string); ok {
		name = &v
	}
	if v, ok := body["description"].(string); ok {
		description = &v
	}

	if err := app.GroupMgr.Update(c.Request.Context(), groupID, name, description); err != nil {
		if err.Error() == "not found" {
			model.ErrorResponse(c, model.ErrGroupNotFound)
			return
		}
		log.Error().Err(err).Msg("Failed to update group")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}

	c.JSON(200, gin.H{"code": 0})
}

func (app *App) deleteGroup(c *gin.Context) {
	groupID := c.Param("groupId")

	if err := app.GroupMgr.Delete(c.Request.Context(), groupID); err != nil {
		if err.Error() == "not found" {
			model.ErrorResponse(c, model.ErrGroupNotFound)
			return
		}
		log.Error().Err(err).Msg("Failed to delete group")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}

	log.Info().Str("group_id", groupID).Msg("Admin deleted group")
	c.JSON(200, gin.H{"code": 0})
}

func (app *App) addGroupAgent(c *gin.Context) {
	groupID := c.Param("groupId")

	var body struct {
		Token string `json:"token"`
		Role  string `json:"role"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || body.Token == "" {
		model.ErrorResponse(c, model.ErrGroupInvalidReq)
		return
	}

	if err := app.GroupMgr.AddAgent(c.Request.Context(), groupID, body.Token, body.Role); err != nil {
		switch err.Error() {
		case "group not found":
			model.ErrorResponse(c, model.ErrGroupNotFound)
		case "invalid token":
			model.ErrorResponse(c, model.ErrAuthInvalidToken)
		case "duplicate":
			model.ErrorResponse(c, model.ErrGroupAgentExists)
		case "invalid role":
			model.ErrorResponse(c, model.ErrGroupInvalidRole)
		default:
			log.Error().Err(err).Msg("Failed to add agent to group")
			c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		}
		return
	}

	c.JSON(200, gin.H{"code": 0})
}

func (app *App) removeGroupAgent(c *gin.Context) {
	groupID := c.Param("groupId")
	token := c.Param("token")

	if err := app.GroupMgr.RemoveAgent(c.Request.Context(), groupID, token); err != nil {
		if err.Error() == "not found" {
			model.ErrorResponse(c, model.ErrGroupAgentNotFound)
			return
		}
		log.Error().Err(err).Msg("Failed to remove agent from group")
		c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		return
	}

	c.JSON(200, gin.H{"code": 0})
}

func (app *App) updateGroupAgentRole(c *gin.Context) {
	groupID := c.Param("groupId")
	token := c.Param("token")

	var body struct {
		Role string `json:"role"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || body.Role == "" {
		model.ErrorResponse(c, model.ErrGroupInvalidReq)
		return
	}

	if err := app.GroupMgr.UpdateAgentRole(c.Request.Context(), groupID, token, body.Role); err != nil {
		switch err.Error() {
		case "not found":
			model.ErrorResponse(c, model.ErrGroupAgentNotFound)
		case "invalid role":
			model.ErrorResponse(c, model.ErrGroupInvalidRole)
		default:
			log.Error().Err(err).Msg("Failed to update agent role")
			c.JSON(500, gin.H{"code": 500, "error": "Internal server error"})
		}
		return
	}

	c.JSON(200, gin.H{"code": 0})
}
