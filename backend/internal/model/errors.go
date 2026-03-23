package model

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

type AppError struct {
	Code    int    // HTTP status code (0 for SSE/internal-only errors)
	Message string
}

var (
	// Auth (token)
	ErrAuthInvalidToken   = AppError{http.StatusUnauthorized, "Invalid or missing token"}
	ErrAuthMissingAuth    = AppError{http.StatusUnauthorized, "Missing authorization"}
	ErrAuthInvalidSession = AppError{http.StatusUnauthorized, "Invalid admin session"}
	ErrAuthUnauthorized   = AppError{http.StatusUnauthorized, "Unauthorized"}
	ErrAuthWrongPassword  = AppError{http.StatusUnauthorized, "Wrong password"}

	// Admin setup
	ErrAdminPasswordExists = AppError{http.StatusBadRequest, "Password already set"}
	ErrAdminPasswordShort  = AppError{http.StatusBadRequest, "Password too short"}

	// Chat / SSE
	ErrChatEmptyMessage  = AppError{http.StatusBadRequest, "Empty message"}
	ErrChatNoBot         = AppError{http.StatusBadRequest, "No bot connected"}
	ErrChatInvalidReq    = AppError{http.StatusBadRequest, "Invalid request"}
	ErrChatSendFailed    = AppError{http.StatusInternalServerError, "Failed to send message to bot"}
	ErrChatStreamTimeout = AppError{0, "Stream timeout"}
	ErrChatInternalError = AppError{0, "Internal server error"}

	// Media
	ErrMediaFileTooLarge    = AppError{http.StatusRequestEntityTooLarge, "File too large"}
	ErrMediaInvalidFile     = AppError{http.StatusBadRequest, "Invalid file or unsupported type"}
	ErrMediaBadURLScheme    = AppError{http.StatusBadRequest, "Invalid media URL scheme"}
	ErrMediaUnsupportedType = AppError{http.StatusBadRequest, "Unsupported media type"}
	ErrMediaTooMany         = AppError{http.StatusBadRequest, "Too many media items (max 10)"}

	// Session
	ErrSessionNotFound = AppError{http.StatusNotFound, "Session not found"}

	// Token (admin CRUD)
	ErrTokenNotFound = AppError{http.StatusNotFound, "Token not found"}

	// WebSocket
	ErrWSInvalidToken  = AppError{4001, "Invalid or missing bot token"}
	ErrWSTokenDeleted  = AppError{4003, "Token deleted"}
	ErrWSServerRestart = AppError{4000, "Server restarting"}
	ErrWSEvicted       = AppError{4005, "Evicted by newer connection"}

	// Group
	ErrGroupNotFound      = AppError{http.StatusNotFound, "Group not found"}
	ErrGroupAgentExists   = AppError{http.StatusConflict, "Agent already in group"}
	ErrGroupAgentNotFound = AppError{http.StatusNotFound, "Agent not in group"}
	ErrGroupInvalidReq    = AppError{http.StatusBadRequest, "Invalid group request"}
	ErrGroupNoLeader      = AppError{http.StatusNotFound, "No leader assigned in group"}
	ErrGroupInvalidRole   = AppError{http.StatusBadRequest, "Invalid role, must be leader or member"}

	// Group WebSocket
	ErrWSGroupNotFound = AppError{4010, "Group not found"}
	ErrWSGroupNoAgents = AppError{4011, "No agents in group"}

	// Bot (internal)
	ErrBotUnknownError = AppError{0, "Unknown error from bot"}
)

// ErrorResponse returns a JSON error response via gin.Context.
func ErrorResponse(c *gin.Context, err AppError, detail ...string) {
	msg := err.Message
	if len(detail) > 0 && detail[0] != "" {
		msg = msg + ": " + detail[0]
	}
	code := err.Code
	if code == 0 {
		code = http.StatusInternalServerError
	}
	c.JSON(code, gin.H{"code": code, "error": msg})
}
