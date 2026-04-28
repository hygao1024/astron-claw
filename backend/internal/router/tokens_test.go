package router

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"astron-claw/backend/internal/config"
)

func TestValidateTokenDoesNotDependOnRedisRateLimit(t *testing.T) {
	app := &App{
		Config: &config.AppConfig{},
	}
	engine := SetupRouter(app, "test-pod")

	for i := 0; i < 25; i++ {
		req := httptest.NewRequest(http.MethodPost, "/api/token/validate", bytes.NewBufferString("{"))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()

		engine.ServeHTTP(rec, req)

		if rec.Code != http.StatusBadRequest {
			t.Fatalf("request %d: status = %d, want %d; body=%s", i+1, rec.Code, http.StatusBadRequest, rec.Body.String())
		}
	}
}
