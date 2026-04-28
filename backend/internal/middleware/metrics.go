package middleware

import (
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"

	"astron-claw/backend/internal/infra/telemetry"
	"astron-claw/backend/internal/model"
)

// MetricsMiddleware records HTTP request metrics for all endpoints.
// Must be registered before gin.Recovery() to capture panic-induced errors.
func MetricsMiddleware(podIP string) gin.HandlerFunc {
	return func(c *gin.Context) {
		// Skip WebSocket endpoint (long-lived connection, HTTP metrics don't apply)
		if c.FullPath() == "/bridge/bot" {
			c.Next()
			return
		}

		start := time.Now()
		funcName := c.FullPath()
		if funcName == "" {
			c.Next()
			return
		}

		// Inject metrics context for handlers
		c.Set("metrics_start", start)
		c.Set("metrics_func", funcName)
		c.Set("metrics_ip", podIP)

		// Default code=0 (success)
		code := "0"

		defer func() {
			// Read handler-set code if exists
			if handlerCode, exists := c.Get("metrics_code"); exists {
				code = handlerCode.(string)
			} else if c.Writer.Status() >= 400 {
				// Fallback: infer from HTTP status
				code = inferCodeFromStatus(c.Writer.Status())
			}

			duration := time.Since(start).Seconds()

			// Record requests counter
			telemetry.ChatRequestTotal.Add(c.Request.Context(), 1,
				metric.WithAttributes(
					attribute.String("func", funcName),
					attribute.String("ip", podIP),
					attribute.String("code", code),
				))

			// Record request.duration (skip if handler marked as SSE stream)
			if _, isSSE := c.Get("metrics_sse_stream"); !isSSE {
				telemetry.ChatRequestDuration.Record(c.Request.Context(), duration,
					metric.WithAttributes(
						attribute.String("func", funcName),
						attribute.String("ip", podIP),
						attribute.String("code", code),
					))
			}
		}()

		c.Next()
	}
}

// inferCodeFromStatus maps HTTP status to business error code (fallback only).
func inferCodeFromStatus(status int) string {
	switch status {
	case 401:
		return strconv.Itoa(model.CodeAuthInvalidToken)
	case 403:
		return strconv.Itoa(model.CodeAuthUnauthorized)
	case 404:
		return strconv.Itoa(model.CodeChatInternalError)
	case 500:
		return strconv.Itoa(model.CodeChatInternalError)
	default:
		return "0"
	}
}

// MetricsErrorResponse sets metrics_code and returns error response.
// Use this instead of model.ErrorResponse to ensure metrics are recorded.
func MetricsErrorResponse(c *gin.Context, err model.AppError, detail ...string) {
	c.Set("metrics_code", strconv.Itoa(err.Code))
	model.ErrorResponse(c, err, detail...)
}
