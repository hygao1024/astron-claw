package router

import (
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
	"gorm.io/gorm"

	"astron-claw/backend/internal/config"
	"astron-claw/backend/internal/infra/storage"
	"astron-claw/backend/internal/middleware"
	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/service"
)

// App holds all application dependencies.
type App struct {
	DB        *gorm.DB
	RDB       redis.UniversalClient
	TokenMgr  *service.TokenManager
	AdminAuth *service.AdminAuth
	MediaMgr  *service.MediaManager
	Bridge    *service.ConnectionBridge
	Queue     service.MessageQueue
	Storage   storage.ObjectStorage
	Config    *config.AppConfig
	GroupMgr  *service.GroupManager
	GroupChat *service.GroupChatManager
}

// SetupRouter configures all routes and middleware.
func SetupRouter(app *App) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())

	// Global body size limit (32 MB)
	r.MaxMultipartMemory = 32 << 20

	// CORS middleware
	r.Use(middleware.CORSMiddleware(app.Config.CORS))

	// Token auth middleware
	r.Use(middleware.TokenAuth(app.TokenMgr, app.RDB))

	// Health
	r.GET("/api/health", app.healthCheck)

	// Metrics
	r.GET("/api/metrics", app.getMetrics)
	r.DELETE("/api/metrics", app.deleteMetrics)

	// Tokens (public)
	r.POST("/api/token", app.createToken)
	r.POST("/api/token/validate", app.validateToken)

	// Admin auth
	r.GET("/api/admin/auth/status", app.adminAuthStatus)
	r.POST("/api/admin/auth/setup", app.adminAuthSetup)
	r.POST("/api/admin/auth/login", app.adminAuthLogin)
	r.POST("/api/admin/auth/logout", app.adminAuthLogout)

	// Admin (auth middleware applied to group)
	admin := r.Group("/api/admin")
	admin.Use(app.adminAuthMiddleware())
	{
		admin.GET("/tokens", app.listTokens)
		admin.POST("/tokens", app.adminCreateToken)
		admin.PATCH("/tokens/:token", app.adminUpdateToken)
		admin.DELETE("/tokens/:token", app.adminDeleteToken)
		admin.POST("/cleanup", app.adminCleanup)

		// Groups
		admin.GET("/groups", app.listGroups)
		admin.POST("/groups", app.createGroup)
		admin.GET("/groups/:groupId", app.getGroup)
		admin.PATCH("/groups/:groupId", app.updateGroup)
		admin.DELETE("/groups/:groupId", app.deleteGroup)
		admin.POST("/groups/:groupId/agents", app.addGroupAgent)
		admin.PATCH("/groups/:groupId/agents/:token", app.updateGroupAgentRole)
		admin.DELETE("/groups/:groupId/agents/:token", app.removeGroupAgent)
	}

	// Media
	r.POST("/api/media/upload", app.uploadMedia)

	// SSE (bridge)
	r.POST("/bridge/chat", app.chatSSE)
	r.GET("/bridge/chat/sessions", app.listSessions)
	r.POST("/bridge/chat/sessions", app.createSession)

	// WebSocket
	r.GET("/bridge/bot", app.wsBot)
	r.GET("/bridge/group/:groupId", app.wsGroupChat)

	return r
}

func (app *App) adminAuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		adminSession, _ := c.Cookie("admin_session")
		if !app.AdminAuth.ValidateSession(c.Request.Context(), adminSession) {
			log.Warn().Msg("Admin auth rejected: missing or invalid session cookie")
			model.ErrorResponse(c, model.ErrAuthUnauthorized)
			c.Abort()
			return
		}
		c.Next()
	}
}
