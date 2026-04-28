package router

import (
	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/middleware"
	"astron-claw/backend/internal/model"
)

func (app *App) createToken(c *gin.Context) {
	var body struct {
		Name string `json:"name"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		// Allow empty body
	}

	token, err := app.TokenMgr.Generate(c.Request.Context(), body.Name, 0)
	if err != nil {
		log.Error().Err(err).Msg("Failed to generate token")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	log.Info().Str("token", token).Str("name", body.Name).Msg("Token created via public API")
	c.JSON(200, gin.H{"code": 0, "token": token})
}

func (app *App) validateToken(c *gin.Context) {
	var body struct {
		Token string `json:"token"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		middleware.MetricsErrorResponse(c, model.ErrChatInvalidReq)
		return
	}

	valid := app.TokenMgr.Validate(c.Request.Context(), body.Token)
	botConnected := false
	if valid {
		botConnected = app.Bridge.IsBotConnected(c.Request.Context(), body.Token)
	}

	log.Debug().Str("token", body.Token).Bool("valid", valid).Msg("Token validate")

	c.JSON(200, gin.H{
		"code":          0,
		"valid":         valid,
		"bot_connected": botConnected,
	})
}
