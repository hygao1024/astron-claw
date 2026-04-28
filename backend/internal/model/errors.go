package model

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

type AppError struct {
	Code       int    // Business error code (10000+)
	HTTPStatus int    // HTTP status code
	Message    string
}

// Error codes (10000+)
const (
	// Success
	CodeSuccess = 0

	// Auth errors (10001-10099)
	CodeAuthInvalidToken   = 10001
	CodeAuthMissingAuth    = 10002
	CodeAuthInvalidSession = 10003
	CodeAuthUnauthorized   = 10004
	CodeAuthWrongPassword  = 10005

	// Admin setup errors (10100-10199)
	CodeAdminPasswordExists = 10100
	CodeAdminPasswordShort  = 10101

	// Chat/SSE errors (10200-10299)
	CodeChatEmptyMessage     = 10200
	CodeChatNoBot            = 10201
	CodeChatInvalidReq       = 10202
	CodeChatSendFailed       = 10203
	CodeChatStreamTimeout    = 10204
	CodeChatInternalError    = 10205
	CodeChatStreamUnsupported = 10206

	// Media errors (10300-10399)
	CodeMediaFileTooLarge    = 10300
	CodeMediaInvalidFile     = 10301
	CodeMediaBadURLScheme    = 10302
	CodeMediaUnsupportedType = 10303
	CodeMediaTooMany         = 10304

	// Session errors (10400-10499)
	CodeSessionNotFound    = 10400
	CodeSessionCreateFailed = 10401

	// Token errors (10500-10599)
	CodeTokenNotFound = 10500

	// WebSocket errors (10600-10699)
	CodeWSInvalidToken  = 10600
	CodeWSTokenDeleted  = 10601
	CodeWSServerRestart = 10602
	CodeWSEvicted       = 10603

	// Bot errors (10700-10799)
	CodeBotUnknownError = 10700
)

var (
	// Auth (token)
	ErrAuthInvalidToken   = AppError{CodeAuthInvalidToken, http.StatusUnauthorized, "Invalid or missing token"}
	ErrAuthMissingAuth    = AppError{CodeAuthMissingAuth, http.StatusUnauthorized, "Missing authorization"}
	ErrAuthInvalidSession = AppError{CodeAuthInvalidSession, http.StatusUnauthorized, "Invalid admin session"}
	ErrAuthUnauthorized   = AppError{CodeAuthUnauthorized, http.StatusUnauthorized, "Unauthorized"}
	ErrAuthWrongPassword  = AppError{CodeAuthWrongPassword, http.StatusUnauthorized, "Wrong password"}

	// Admin setup
	ErrAdminPasswordExists = AppError{CodeAdminPasswordExists, http.StatusBadRequest, "Password already set"}
	ErrAdminPasswordShort  = AppError{CodeAdminPasswordShort, http.StatusBadRequest, "Password too short"}

	// Chat / SSE
	ErrChatEmptyMessage      = AppError{CodeChatEmptyMessage, http.StatusBadRequest, "Empty message"}
	ErrChatNoBot             = AppError{CodeChatNoBot, http.StatusBadRequest, "No bot connected"}
	ErrChatInvalidReq        = AppError{CodeChatInvalidReq, http.StatusBadRequest, "Invalid request"}
	ErrChatSendFailed        = AppError{CodeChatSendFailed, http.StatusInternalServerError, "Failed to send message to bot"}
	ErrChatStreamTimeout     = AppError{CodeChatStreamTimeout, http.StatusInternalServerError, "Stream timeout"}
	ErrChatInternalError     = AppError{CodeChatInternalError, http.StatusInternalServerError, "Internal server error"}
	ErrChatStreamUnsupported = AppError{CodeChatStreamUnsupported, http.StatusInternalServerError, "Streaming not supported"}

	// Media
	ErrMediaFileTooLarge    = AppError{CodeMediaFileTooLarge, http.StatusRequestEntityTooLarge, "File too large"}
	ErrMediaInvalidFile     = AppError{CodeMediaInvalidFile, http.StatusBadRequest, "Invalid file or unsupported type"}
	ErrMediaBadURLScheme    = AppError{CodeMediaBadURLScheme, http.StatusBadRequest, "Invalid media URL scheme"}
	ErrMediaUnsupportedType = AppError{CodeMediaUnsupportedType, http.StatusBadRequest, "Unsupported media type"}
	ErrMediaTooMany         = AppError{CodeMediaTooMany, http.StatusBadRequest, "Too many media items (max 10)"}

	// Session
	ErrSessionNotFound     = AppError{CodeSessionNotFound, http.StatusNotFound, "Session not found"}
	ErrSessionCreateFailed = AppError{CodeSessionCreateFailed, http.StatusInternalServerError, "Failed to create session"}

	// Token (admin CRUD)
	ErrTokenNotFound = AppError{CodeTokenNotFound, http.StatusNotFound, "Token not found"}

	// WebSocket
	ErrWSInvalidToken  = AppError{CodeWSInvalidToken, 4001, "Invalid or missing bot token"}
	ErrWSTokenDeleted  = AppError{CodeWSTokenDeleted, 4003, "Token deleted"}
	ErrWSServerRestart = AppError{CodeWSServerRestart, 4000, "Server restarting"}
	ErrWSEvicted       = AppError{CodeWSEvicted, 4005, "Evicted by newer connection"}

	// Bot (internal)
	ErrBotUnknownError = AppError{CodeBotUnknownError, http.StatusInternalServerError, "Unknown error from bot"}
)

// ErrorResponse returns a JSON error response via gin.Context.
func ErrorResponse(c *gin.Context, err AppError, detail ...string) {
	msg := err.Message
	if len(detail) > 0 && detail[0] != "" {
		msg = msg + ": " + detail[0]
	}
	httpStatus := err.HTTPStatus
	if httpStatus == 0 {
		httpStatus = http.StatusInternalServerError
	}
	c.JSON(httpStatus, gin.H{"code": err.Code, "error": msg})
}
