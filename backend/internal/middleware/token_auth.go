package middleware

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"

	"astron-claw/backend/internal/service"
)

const (
	cachePrefix = "token_auth:"
	cacheTTL    = 30 * time.Second
)

var protectedPrefixes = []string{"/bridge/", "/api/media/"}

// TokenAuth returns a Gin middleware that validates Bearer tokens on protected paths.
func TokenAuth(tokenMgr *service.TokenManager, rdb redis.UniversalClient) gin.HandlerFunc {
	return func(c *gin.Context) {
		if !isProtected(c.Request.URL.Path) {
			c.Next()
			return
		}

		// Extract token: X-Api-Key first, then Authorization Bearer
		raw := c.GetHeader("X-Api-Key")
		if raw == "" {
			raw = c.GetHeader("Authorization")
		}
		token := extractBearer(raw)

		if token == "" || !validateCached(c.Request.Context(), token, tokenMgr, rdb) {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"code":  http.StatusUnauthorized,
				"error": "Invalid or missing token",
			})
			return
		}

		c.Set("token", token)
		c.Next()
	}
}

// Paths that handle their own auth (e.g. WebSocket with query-param token).
var excludedPaths = map[string]bool{
	"/bridge/bot": true,
}

// Prefixes that handle their own auth (for dynamic path segments).
var excludedPrefixes = []string{
	"/bridge/group/",
}

func isProtected(path string) bool {
	if excludedPaths[path] {
		return false
	}
	for _, prefix := range excludedPrefixes {
		if strings.HasPrefix(path, prefix) {
			return false
		}
	}
	for _, prefix := range protectedPrefixes {
		if strings.HasPrefix(path, prefix) {
			return true
		}
	}
	return false
}

func extractBearer(raw string) string {
	if raw == "" {
		return ""
	}
	// If it starts with "Bearer ", extract the token
	if len(raw) >= 7 && strings.EqualFold(raw[:7], "bearer ") {
		token := strings.TrimSpace(raw[7:])
		return token
	}
	// Otherwise treat the whole value as the token (X-Api-Key style)
	return raw
}

func validateCached(ctx context.Context, token string, tokenMgr *service.TokenManager, rdb redis.UniversalClient) bool {
	key := cachePrefix + token

	// Fast path: cache hit
	cached, err := rdb.Get(ctx, key).Result()
	if err == nil && cached == "1" {
		return true
	}

	// Slow path: ask MySQL
	if tokenMgr.Validate(ctx, token) {
		rdb.Set(ctx, key, "1", cacheTTL)
		return true
	}
	return false
}

// InvalidateTokenCache removes a token from the auth cache.
func InvalidateTokenCache(ctx context.Context, rdb redis.UniversalClient, token string) {
	rdb.Del(ctx, cachePrefix+token)
}
