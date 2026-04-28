package router

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/rs/zerolog/log"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"astron-claw/backend/internal/infra/telemetry"
	"astron-claw/backend/internal/middleware"
	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/pkg"
	"astron-claw/backend/internal/service"
)

var sseTracer = otel.Tracer("astron-claw/router/sse")

const (
	sseTimeout        = 600 // 10 minutes
	sseBlockMs        = 1000
	heartbeatInterval = 15.0 // seconds
)

type ChatRequest struct {
	Content   string      `json:"content"`
	SessionID *string     `json:"sessionId,omitempty"`
	Media     []MediaItem `json:"media,omitempty"`
}

type MediaItem struct {
	Type     string `json:"type"`
	Content  string `json:"content"`
	MimeType string `json:"mimeType,omitempty"`
}

func (app *App) chatSSE(c *gin.Context) {
	tokenStr := c.GetString("token")
	tp := telemetry.TokenPrefix(tokenStr)

	ctx, turnSpan := sseTracer.Start(c.Request.Context(), "chat.turn",
		trace.WithSpanKind(trace.SpanKindInternal))

	// Log state variables
	var (
		logSession string
		logTTFBMs  float64
		closeReason string
	)

	defer func() {
		if closeReason != "" {
			turnSpan.SetAttributes(attribute.String("astron.close_reason", closeReason))
		}
		turnSpan.End()
	}()

	// Emit OTel log on every return path
	defer func() {
		logCode := "0"
		if raw, exists := c.Get("metrics_code"); exists {
			if codeStr, ok := raw.(string); ok {
				logCode = codeStr
			}
		}

		var durationMs float64
		if raw, exists := c.Get("metrics_start"); exists {
			if startTime, ok := raw.(time.Time); ok {
				durationMs = float64(time.Since(startTime).Milliseconds())
			}
		}

		traceID := turnSpan.SpanContext().TraceID().String()

		telemetry.EmitChatLog(ctx, telemetry.ChatLogRecord{
			LogType:     "metrics_log",
			AppID:       tokenStr,
			SessionID:   logSession,
			FALR:        durationMs,
			FAFR:        logTTFBMs,
			Ret:         logCode,
			IP:          c.GetString("metrics_ip"),
			TraceID:     traceID,
			Func:        c.GetString("metrics_func"),
			ServiceName: app.Config.OTLP.ServiceName,
		})
	}()

	var body ChatRequest
	if err := c.ShouldBindJSON(&body); err != nil {
		c.Set("metrics_code", strconv.Itoa(model.CodeChatInvalidReq))
		model.ErrorResponse(c, model.ErrChatInvalidReq)
		return
	}

	// Validate media
	content := body.Content
	var mediaURLs []string

	if len(body.Media) > 10 {
		log.Warn().Str("token", tp).Msg("SSE: bad request — too many media items")
		c.Set("metrics_code", strconv.Itoa(model.CodeMediaTooMany))
		model.ErrorResponse(c, model.ErrMediaTooMany)
		return
	}

	if body.Media != nil {
		for _, item := range body.Media {
			if item.Type == "url" {
				if !strings.HasPrefix(item.Content, "http://") && !strings.HasPrefix(item.Content, "https://") {
					log.Warn().Str("url", item.Content).Str("token", tp).
						Msg("SSE: bad request — invalid media URL scheme")
					c.Set("metrics_code", strconv.Itoa(model.CodeMediaBadURLScheme))
					model.ErrorResponse(c, model.ErrMediaBadURLScheme)
					return
				}
				mediaURLs = append(mediaURLs, item.Content)
			} else {
				log.Warn().Str("type", item.Type).Str("token", tp).
					Msg("SSE: bad request — unsupported media type")
				c.Set("metrics_code", strconv.Itoa(model.CodeMediaUnsupportedType))
				model.ErrorResponse(c, model.ErrMediaUnsupportedType)
				return
			}
		}
	}

	if content == "" && len(mediaURLs) == 0 {
		log.Warn().Str("token", tp).Msg("SSE: bad request — empty message")
		c.Set("metrics_code", strconv.Itoa(model.CodeChatEmptyMessage))
		model.ErrorResponse(c, model.ErrChatEmptyMessage)
		return
	}

	// Check bot connected
	{
		_, availSpan := sseTracer.Start(ctx, "chat.bot.availability_check",
			trace.WithSpanKind(trace.SpanKindInternal))
		connected := app.Bridge.IsBotConnected(ctx, tokenStr)
		availSpan.SetAttributes(
			attribute.String("astron.token_id", tokenStr),
			attribute.Bool("astron.bot_available", connected),
		)
		if !connected {
			availSpan.SetStatus(codes.Error, "no bot connected")
			availSpan.SetAttributes(attribute.Int("astron.error_code", model.CodeChatNoBot))
			app.storeSpanContent(content, availSpan, "astron.user_input")
			availSpan.End()
			turnSpan.SetStatus(codes.Error, "no bot connected")
			log.Warn().Str("token", tp).Msg("SSE: no bot connected")
			c.Set("metrics_code", strconv.Itoa(model.CodeChatNoBot))
			model.ErrorResponse(c, model.ErrChatNoBot)
			return
		}
		availSpan.End()
	}

	// Resolve session
	var sessionID string
	var sessionNumber int
	isNewSession := false
	{
		resolveCtx, resolveSpan := sseTracer.Start(ctx, "chat.session.resolve",
			trace.WithSpanKind(trace.SpanKindInternal))
		if body.SessionID != nil && *body.SessionID != "" {
			sid, snum, found := app.Bridge.GetSession(resolveCtx, tokenStr, *body.SessionID)
			if !found {
				resolveSpan.SetStatus(codes.Error, "session not found")
				resolveSpan.SetAttributes(attribute.Int("astron.error_code", model.CodeSessionNotFound))
				app.storeSpanContent(content, resolveSpan, "astron.user_input")
				resolveSpan.End()
				turnSpan.SetStatus(codes.Error, "session not found")
				log.Warn().Str("session", *body.SessionID).Str("token", tp).
					Msg("SSE: session not found")
				c.Set("metrics_code", strconv.Itoa(model.CodeSessionNotFound))
				model.ErrorResponse(c, model.ErrSessionNotFound)
				return
			}
			sessionID = sid
			sessionNumber = snum
		} else {
			var err error
			sessionID, sessionNumber, err = app.Bridge.CreateSession(resolveCtx, tokenStr)
			if err != nil {
				resolveSpan.SetStatus(codes.Error, "create session failed")
				resolveSpan.SetAttributes(attribute.Int("astron.error_code", model.CodeSessionCreateFailed))
				resolveSpan.RecordError(err)
				app.storeSpanContent(content, resolveSpan, "astron.user_input")
				resolveSpan.End()
				turnSpan.SetStatus(codes.Error, "create session failed")
				log.Error().Err(err).Str("token", tp).Msg("SSE: failed to create session")
				c.Set("metrics_code", strconv.Itoa(model.CodeSessionCreateFailed))
				model.ErrorResponse(c, model.ErrSessionCreateFailed)
				return
			}
			isNewSession = true
		}
		resolveSpan.SetAttributes(
			attribute.String("astron.token_id", tokenStr),
			attribute.String("astron.session_id", sessionID),
			attribute.Bool("astron.is_new_session", isNewSession),
		)
		resolveSpan.End()
	}

	// Session resolved successfully
	logSession = sessionID

	// Clear stale events and reset consumer group
	inbox := service.ChatInboxPrefix + tokenStr + ":" + sessionID
	app.Queue.Purge(ctx, inbox)
	app.Queue.EnsureGroup(ctx, inbox, "sse")
	app.Bridge.TrackChatInbox(ctx, tokenStr, inbox)

	// Send message to bot
	var reqID string
	{
		_, dispatchSpan := sseTracer.Start(ctx, "chat.bot.dispatch",
			trace.WithSpanKind(trace.SpanKindProducer))
		var err error
		reqID, err = app.Bridge.SendToBot(ctx, tokenStr, content, mediaURLs, sessionID)
		if err != nil {
			dispatchSpan.SetStatus(codes.Error, "send to bot failed")
			dispatchSpan.SetAttributes(attribute.Int("astron.error_code", model.CodeChatSendFailed))
			dispatchSpan.RecordError(err)
			app.storeSpanContent(content, dispatchSpan, "astron.user_input")
			dispatchSpan.End()
			turnSpan.SetStatus(codes.Error, "send to bot failed")
			log.Error().Err(err).Str("token", tp).Msg("SSE: send_to_bot failed")
			c.Set("metrics_code", strconv.Itoa(model.CodeChatSendFailed))
			model.ErrorResponse(c, model.ErrChatSendFailed)
			return
		}
		dispatchSpan.SetAttributes(
			attribute.String("astron.token_id", tokenStr),
			attribute.String("astron.session_id", sessionID),
			attribute.String("astron.turn_id", reqID),
			attribute.Int("astron.message_size", len(content)),
			attribute.Int("astron.media_count", len(mediaURLs)),
		)
		dispatchSpan.End()
	}

	// Set turn span attributes
	turnSpan.SetAttributes(
		attribute.String("astron.token_id", tokenStr),
		attribute.String("astron.session_id", sessionID),
		attribute.String("astron.turn_id", reqID),
		attribute.Bool("astron.is_new_session", isNewSession),
	)

	// Record user input content on turn span
	app.storeSpanContent(content, turnSpan, "astron.user_input")

	// Check Flusher support BEFORE marking as SSE stream
	flusher, ok := c.Writer.(http.Flusher)
	if !ok {
		turnSpan.SetStatus(codes.Error, "stream unsupported")
		c.Set("metrics_code", strconv.Itoa(model.CodeChatStreamUnsupported))
		model.ErrorResponse(c, model.ErrChatStreamUnsupported)
		return
	}

	// Success — entering SSE stream
	// Mark as SSE stream to prevent middleware from recording request.duration
	c.Set("metrics_sse_stream", true)

	log.Info().Str("req", reqID).Str("session", sessionID).Str("token", tp).
		Msg("SSE: chat started")

	// Track active stream
	streamStart := time.Now()
	closeReason = "done"
	streamCode := "0"
	funcPath := c.GetString("metrics_func")
	podIP := c.GetString("metrics_ip")

	// Start response stream span
	var replyBuf strings.Builder
	_, streamSpan := sseTracer.Start(ctx, "chat.response.stream",
		trace.WithSpanKind(trace.SpanKindInternal))
	defer func() {
		if replyBuf.Len() > 0 {
			app.storeSpanContent(replyBuf.String(), streamSpan, "astron.bot_reply")
		}
		streamSpan.SetAttributes(
			attribute.String("astron.token_id", tokenStr),
			attribute.String("astron.session_id", sessionID),
			attribute.String("astron.turn_id", reqID),
			attribute.String("astron.close_reason", closeReason),
			attribute.Int64("astron.stream_duration_ms", time.Since(streamStart).Milliseconds()),
		)
		streamSpan.End()
	}()

	// Set SSE headers
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	c.Status(http.StatusOK)

	// 注册SSE连接到bot状态监控器
	var sseConn *service.SSEConnection
	if app.BotStatusMonitor != nil {
		sseConn = app.BotStatusMonitor.RegisterSSEConnection(tokenStr, sessionID, inbox)
		defer app.BotStatusMonitor.UnregisterSSEConnection(inbox)
	}

	defer func() {
		streamDuration := time.Since(streamStart).Seconds()
		telemetry.ChatStreamDuration.Record(context.Background(), streamDuration,
			metric.WithAttributes(
				attribute.String("func", funcPath),
				attribute.String("ip", podIP),
				attribute.String("code", streamCode),
				attribute.String("close_reason", closeReason),
			))
	}()

	// Stream events — use a deadline tracker instead of re-creating contexts
	deadline := time.Now().Add(sseTimeout * time.Second)

	// First event: session info
	sessionEvent := pkg.FormatSSEEvent("session", map[string]interface{}{
		"sessionId":     sessionID,
		"sessionNumber": sessionNumber,
	})
	if _, err := c.Writer.WriteString(sessionEvent); err != nil {
		closeReason = "write_error"
		streamCode = strconv.Itoa(model.CodeChatInternalError)
		c.Set("metrics_code", streamCode)
		streamSpan.SetStatus(codes.Error, "write error")
		turnSpan.SetStatus(codes.Error, "write error")
		return
	}
	flusher.Flush()

	// Record TTFB after first SSE event written
	if raw, exists := c.Get("metrics_start"); exists {
		if startTime, ok := raw.(time.Time); ok {
			logTTFBMs = float64(time.Since(startTime).Milliseconds())
		}
	}

	lastHeartbeat := time.Now()
	hasChunks := false
	firstResult := true

	// 准备 bot 断开监听 channel
	var botDisconnectC <-chan struct{}
	if sseConn != nil {
		botDisconnectC = sseConn.DisconnectC
	} else {
		// 创建一个永远不会触发的 channel（nil channel 在 select 中会被忽略）
		botDisconnectC = nil
	}

	for {
		// Check client disconnect and bot disconnect
		select {
		case <-c.Request.Context().Done():
			closeReason = "client_disconnect"
			log.Info().Str("token", tp).Msg("SSE: client disconnected")
			// Create cancel span in background context but preserve trace
			cancelCtx := trace.ContextWithSpan(context.Background(), trace.SpanFromContext(ctx))
			go func() {
				_, cancelSpan := sseTracer.Start(cancelCtx, "chat.cancel",
					trace.WithSpanKind(trace.SpanKindInternal))
				cancelSpan.SetAttributes(
					attribute.String("astron.token_id", tokenStr),
					attribute.String("astron.session_id", sessionID),
					attribute.String("astron.turn_id", reqID),
					attribute.String("astron.cancel_reason", "client_disconnect"),
				)
				defer cancelSpan.End()
				app.Bridge.SendCancelToBot(cancelCtx, tokenStr, sessionID)
			}()
			return
		case <-botDisconnectC:
			closeReason = "bot_disconnect"
			streamCode = strconv.Itoa(model.CodeChatNoBot)
			c.Set("metrics_code", streamCode)
			streamSpan.SetStatus(codes.Error, "bot disconnected")
			turnSpan.SetStatus(codes.Error, "bot disconnected")
			log.Info().Str("token", tp).Msg("SSE: bot disconnected")
			errEvent := pkg.FormatSSEEvent("error", map[string]interface{}{
				"content": model.ErrChatNoBot.Message,
			})
			_, _ = c.Writer.WriteString(errEvent)
			flusher.Flush()
			return
		default:
		}

		// Check timeout
		if time.Now().After(deadline) {
			closeReason = "timeout"
			streamCode = strconv.Itoa(model.CodeChatStreamTimeout)
			c.Set("metrics_code", streamCode)
			streamSpan.SetStatus(codes.Error, "stream timeout")
			turnSpan.SetStatus(codes.Error, "stream timeout")
			errEvent := pkg.FormatSSEEvent("error", map[string]interface{}{
				"content": model.ErrChatStreamTimeout.Message,
			})
			_, _ = c.Writer.WriteString(errEvent)
			flusher.Flush()
			return
		}

		result, err := app.Queue.Consume(c.Request.Context(), inbox, "sse", reqID, sseBlockMs)
		if err != nil {
			log.Error().Err(err).Str("token", tp).Msg("SSE: consume error")
			closeReason = "error"
			streamCode = strconv.Itoa(model.CodeChatInternalError)
			c.Set("metrics_code", streamCode)
			streamSpan.SetStatus(codes.Error, "consume error")
			streamSpan.RecordError(err)
			turnSpan.SetStatus(codes.Error, "consume error")
			errEvent := pkg.FormatSSEEvent("error", map[string]interface{}{
				"content": model.ErrChatInternalError.Message,
			})
			_, _ = c.Writer.WriteString(errEvent)
			flusher.Flush()
			return
		}

		if result == nil {
			// Check client disconnect and bot disconnect
			select {
			case <-c.Request.Context().Done():
				closeReason = "client_disconnect"
				cancelCtx := trace.ContextWithSpan(context.Background(), trace.SpanFromContext(ctx))
				go func() {
					_, cancelSpan := sseTracer.Start(cancelCtx, "chat.cancel",
						trace.WithSpanKind(trace.SpanKindInternal))
					cancelSpan.SetAttributes(
						attribute.String("astron.token_id", tokenStr),
						attribute.String("astron.session_id", sessionID),
						attribute.String("astron.turn_id", reqID),
						attribute.String("astron.cancel_reason", "client_disconnect"),
					)
					defer cancelSpan.End()
					app.Bridge.SendCancelToBot(cancelCtx, tokenStr, sessionID)
				}()
				return
			case <-botDisconnectC:
				closeReason = "bot_disconnect"
				streamCode = strconv.Itoa(model.CodeChatNoBot)
				c.Set("metrics_code", streamCode)
				streamSpan.SetStatus(codes.Error, "bot disconnected")
				turnSpan.SetStatus(codes.Error, "bot disconnected")
				log.Info().Str("token", tp).Msg("SSE: bot disconnected")
				errEvent := pkg.FormatSSEEvent("error", map[string]interface{}{
					"content": model.ErrChatNoBot.Message,
				})
				_, _ = c.Writer.WriteString(errEvent)
				flusher.Flush()
				return
			default:
			}

			// Heartbeat
			if time.Since(lastHeartbeat).Seconds() >= heartbeatInterval {
				_, _ = c.Writer.WriteString(pkg.FormatSSEComment())
				flusher.Flush()
				lastHeartbeat = time.Now()
			}
			continue
		}

		_ = app.Queue.Ack(context.Background(), inbox, "sse", result.ID)

		// Record time-to-first-redis-result
		if firstResult {
			firstResult = false
			if metricsStart, exists := c.Get("metrics_start"); exists {
				if startTime, ok := metricsStart.(time.Time); ok {
					telemetry.ChatRequestDuration.Record(ctx, time.Since(startTime).Seconds(),
						metric.WithAttributes(
							attribute.String("func", funcPath),
							attribute.String("ip", podIP),
							attribute.String("code", "0"),
						))
				}
			}
		}

		// Reset deadline on activity
		deadline = time.Now().Add(sseTimeout * time.Second)

		var event map[string]interface{}
		if err := json.Unmarshal([]byte(result.Data), &event); err != nil {
			log.Warn().Str("token", tp).Msg("SSE: invalid JSON in inbox")
			continue
		}

		eventType, _ := event["type"].(string)
		if eventType == "" {
			eventType = "message"
		}
		delete(event, "type")

		var eventData map[string]interface{}
		if eventType == "media" {
			if d, ok := event["data"].(map[string]interface{}); ok {
				eventData = d
			} else {
				log.Warn().Str("token", tp).Msg("SSE: media event missing data payload")
				continue
			}
		} else {
			eventData = event
		}

		if eventType == "chunk" {
			hasChunks = true
			if chunkContent, ok := eventData["content"].(string); ok {
				replyBuf.WriteString(chunkContent)
			}
		}

		// Auto-inject chunk before done if no preceding chunks
		if eventType == "done" && !hasChunks {
			if contentStr, ok := eventData["content"].(string); ok && contentStr != "" {
				replyBuf.WriteString(contentStr)
				chunkEvent := pkg.FormatSSEEvent("chunk", map[string]interface{}{
					"content": contentStr,
				})
				_, _ = c.Writer.WriteString(chunkEvent)
				flusher.Flush()
			}
		}

		sseEvent := pkg.FormatSSEEvent(eventType, eventData)
		_, _ = c.Writer.WriteString(sseEvent)
		flusher.Flush()

		// Terminal events
		if eventType == "done" || eventType == "error" {
			if eventType == "error" {
				closeReason = "error"
				streamCode = strconv.Itoa(model.CodeBotUnknownError)
				c.Set("metrics_code", streamCode)
				streamSpan.SetStatus(codes.Error, "bot error event")
				turnSpan.SetStatus(codes.Error, "bot error event")
			}
			return
		}
	}
}

func (app *App) listSessions(c *gin.Context) {
	tokenStr := c.GetString("token")

	sessions, err := app.Bridge.GetSessions(c.Request.Context(), tokenStr)
	if err != nil {
		log.Error().Err(err).Msg("Failed to list sessions")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	sessionList := make([]gin.H, len(sessions))
	for i, s := range sessions {
		sessionList[i] = gin.H{"id": s.ID, "number": s.Number}
	}

	c.JSON(200, gin.H{
		"code":     0,
		"sessions": sessionList,
	})
}

func (app *App) createSession(c *gin.Context) {
	tokenStr := c.GetString("token")
	ctx := c.Request.Context()

	sessionID, sessionNumber, err := app.Bridge.CreateSession(ctx, tokenStr)
	if err != nil {
		log.Error().Err(err).Msg("Failed to create session")
		middleware.MetricsErrorResponse(c, model.ErrSessionCreateFailed)
		return
	}

	sessions, err := app.Bridge.GetSessions(ctx, tokenStr)
	if err != nil {
		log.Error().Err(err).Msg("Failed to list sessions")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	sessionList := make([]gin.H, len(sessions))
	for i, s := range sessions {
		sessionList[i] = gin.H{"id": s.ID, "number": s.Number}
	}

	c.JSON(200, gin.H{
		"code":          0,
		"sessionId":     sessionID,
		"sessionNumber": sessionNumber,
		"sessions":      sessionList,
	})
}

// storeSpanContent sets a span attribute with content inline (<=1024 bytes)
// or uploads to S3 and stores the URL for larger content.
func (app *App) storeSpanContent(content string, span trace.Span, attrKey string) {
	if len(content) <= 1024 {
		span.SetAttributes(attribute.String(attrKey, content))
		return
	}
	key := "otel/" + uuid.New().String() + ".txt"
	url, err := app.Storage.PutObject(key, strings.NewReader(content), "text/plain", int64(len(content)))
	if err != nil {
		log.Warn().Err(err).Str("key", key).Msg("S3 upload failed for span content, truncating")
		span.SetAttributes(attribute.String(attrKey, content[:1024]+"...(truncated)"))
		return
	}
	span.SetAttributes(attribute.String(attrKey, url))
}
