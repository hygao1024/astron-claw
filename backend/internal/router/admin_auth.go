package router

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/middleware"
	"astron-claw/backend/internal/model"
)

func (app *App) adminAuthStatus(c *gin.Context) {
	ctx := c.Request.Context()
	adminSession, _ := c.Cookie("admin_session")

	needSetup := true
	isSet, err := app.AdminAuth.IsPasswordSet(ctx)
	if err == nil {
		needSetup = !isSet
	}

	authenticated := app.AdminAuth.ValidateSession(ctx, adminSession)

	c.JSON(200, gin.H{
		"code":          0,
		"need_setup":    needSetup,
		"authenticated": authenticated,
	})
}

func (app *App) adminAuthSetup(c *gin.Context) {
	ctx := c.Request.Context()

	isSet, err := app.AdminAuth.IsPasswordSet(ctx)
	if err != nil {
		log.Error().Err(err).Msg("Failed to check admin password status")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	if isSet {
		middleware.MetricsErrorResponse(c, model.ErrAdminPasswordExists)
		return
	}

	var body struct {
		Password string `json:"password"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		middleware.MetricsErrorResponse(c, model.ErrChatInvalidReq)
		return
	}
	if len(body.Password) < 8 {
		middleware.MetricsErrorResponse(c, model.ErrAdminPasswordShort)
		return
	}

	if err := app.AdminAuth.SetPassword(ctx, body.Password); err != nil {
		log.Error().Err(err).Msg("Failed to set admin password")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	session, err := app.AdminAuth.CreateSession(ctx)
	if err != nil {
		log.Error().Err(err).Msg("Failed to create admin session")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	log.Info().Msg("Admin password set up for the first time")
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie("admin_session", session, 86400, "/", "", app.Config.Server.SecureCookie, true)
	c.JSON(200, gin.H{"code": 0})
}

func (app *App) adminAuthLogin(c *gin.Context) {
	ctx := c.Request.Context()

	var body struct {
		Password string `json:"password"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		middleware.MetricsErrorResponse(c, model.ErrChatInvalidReq)
		return
	}

	valid, err := app.AdminAuth.VerifyPassword(ctx, body.Password)
	if err != nil {
		log.Error().Err(err).Msg("Failed to verify admin password")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}
	if !valid {
		log.Warn().Msg("Admin login failed — wrong password")
		middleware.MetricsErrorResponse(c, model.ErrAuthWrongPassword)
		return
	}

	session, err := app.AdminAuth.CreateSession(ctx)
	if err != nil {
		log.Error().Err(err).Msg("Failed to create admin session after login")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	log.Info().Msg("Admin logged in successfully")
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie("admin_session", session, 86400, "/", "", app.Config.Server.SecureCookie, true)
	c.JSON(200, gin.H{"code": 0})
}

func (app *App) adminAuthLogout(c *gin.Context) {
	adminSession, _ := c.Cookie("admin_session")
	app.AdminAuth.RemoveSession(c.Request.Context(), adminSession)
	log.Info().Msg("Admin logged out")
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie("admin_session", "", -1, "/", "", app.Config.Server.SecureCookie, true)
	c.JSON(200, gin.H{"code": 0})
}
