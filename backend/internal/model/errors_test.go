package model

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestErrorCodes(t *testing.T) {
	tests := []struct {
		name       string
		err        AppError
		httpStatus int
		bizCode    int
	}{
		{"AuthInvalidToken", ErrAuthInvalidToken, http.StatusUnauthorized, CodeAuthInvalidToken},
		{"AuthMissingAuth", ErrAuthMissingAuth, http.StatusUnauthorized, CodeAuthMissingAuth},
		{"AuthInvalidSession", ErrAuthInvalidSession, http.StatusUnauthorized, CodeAuthInvalidSession},
		{"AuthUnauthorized", ErrAuthUnauthorized, http.StatusUnauthorized, CodeAuthUnauthorized},
		{"AuthWrongPassword", ErrAuthWrongPassword, http.StatusUnauthorized, CodeAuthWrongPassword},
		{"AdminPasswordExists", ErrAdminPasswordExists, http.StatusBadRequest, CodeAdminPasswordExists},
		{"AdminPasswordShort", ErrAdminPasswordShort, http.StatusBadRequest, CodeAdminPasswordShort},
		{"ChatEmptyMessage", ErrChatEmptyMessage, http.StatusBadRequest, CodeChatEmptyMessage},
		{"ChatNoBot", ErrChatNoBot, http.StatusBadRequest, CodeChatNoBot},
		{"ChatSendFailed", ErrChatSendFailed, http.StatusInternalServerError, CodeChatSendFailed},
		{"ChatStreamTimeout", ErrChatStreamTimeout, http.StatusInternalServerError, CodeChatStreamTimeout},
		{"ChatInternalError", ErrChatInternalError, http.StatusInternalServerError, CodeChatInternalError},
		{"MediaFileTooLarge", ErrMediaFileTooLarge, http.StatusRequestEntityTooLarge, CodeMediaFileTooLarge},
		{"MediaInvalidFile", ErrMediaInvalidFile, http.StatusBadRequest, CodeMediaInvalidFile},
		{"MediaBadURLScheme", ErrMediaBadURLScheme, http.StatusBadRequest, CodeMediaBadURLScheme},
		{"MediaUnsupportedType", ErrMediaUnsupportedType, http.StatusBadRequest, CodeMediaUnsupportedType},
		{"MediaTooMany", ErrMediaTooMany, http.StatusBadRequest, CodeMediaTooMany},
		{"SessionNotFound", ErrSessionNotFound, http.StatusNotFound, CodeSessionNotFound},
		{"TokenNotFound", ErrTokenNotFound, http.StatusNotFound, CodeTokenNotFound},
		{"WSInvalidToken", ErrWSInvalidToken, 4001, CodeWSInvalidToken},
		{"WSTokenDeleted", ErrWSTokenDeleted, 4003, CodeWSTokenDeleted},
		{"WSServerRestart", ErrWSServerRestart, 4000, CodeWSServerRestart},
		{"WSEvicted", ErrWSEvicted, 4005, CodeWSEvicted},
		{"BotUnknownError", ErrBotUnknownError, http.StatusInternalServerError, CodeBotUnknownError},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if tt.err.HTTPStatus != tt.httpStatus {
				t.Errorf("%s: HTTPStatus = %d, want %d", tt.name, tt.err.HTTPStatus, tt.httpStatus)
			}
			if tt.err.Code != tt.bizCode {
				t.Errorf("%s: Code = %d, want %d", tt.name, tt.err.Code, tt.bizCode)
			}
			if tt.err.Message == "" {
				t.Errorf("%s: message should not be empty", tt.name)
			}
		})
	}
}

func TestErrorResponse(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("standard error", func(t *testing.T) {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		ErrorResponse(c, ErrChatNoBot)

		if w.Code != http.StatusBadRequest {
			t.Errorf("status = %d, want %d", w.Code, http.StatusBadRequest)
		}
		var resp map[string]interface{}
		json.Unmarshal(w.Body.Bytes(), &resp)
		if resp["code"] != float64(CodeChatNoBot) {
			t.Errorf("code = %v, want %d", resp["code"], CodeChatNoBot)
		}
		if resp["error"] != "No bot connected" {
			t.Errorf("error = %v, want 'No bot connected'", resp["error"])
		}
	})

	t.Run("with detail", func(t *testing.T) {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		ErrorResponse(c, ErrSessionNotFound, "sess-abc")

		var resp map[string]interface{}
		json.Unmarshal(w.Body.Bytes(), &resp)
		errMsg, _ := resp["error"].(string)
		if errMsg != "Session not found: sess-abc" {
			t.Errorf("error = %q, want 'Session not found: sess-abc'", errMsg)
		}
	})

	t.Run("zero code defaults to 500", func(t *testing.T) {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		ErrorResponse(c, ErrChatStreamTimeout)

		if w.Code != http.StatusInternalServerError {
			t.Errorf("status = %d, want 500", w.Code)
		}
	})
}
