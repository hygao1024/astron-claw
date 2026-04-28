package router

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog/log"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/service"
)

var wsTracer = otel.Tracer("astron-claw/router/websocket")

var wsUpgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

func (app *App) wsBot(c *gin.Context) {
	botToken := c.Query("token")
	if botToken == "" {
		botToken = c.GetHeader("x-astron-bot-token")
	}

	if !app.TokenMgr.Validate(c.Request.Context(), botToken) {
		// Accept then close with error code
		conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			log.Error().Err(err).Msg("WS upgrade failed for invalid token")
			return
		}
		msg := websocket.FormatCloseMessage(model.ErrWSInvalidToken.HTTPStatus, model.ErrWSInvalidToken.Message)
		_ = conn.WriteMessage(websocket.CloseMessage, msg)
		conn.Close()
		log.Warn().Str("token", botToken).Msg("Bot connection rejected: invalid token")
		return
	}

	conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Error().Err(err).Msg("WS upgrade failed")
		return
	}

	clientAddr := c.ClientIP()
	botConn := &service.BotConn{
		Conn:  conn,
		Token: botToken,
	}

	ctx, regSpan := wsTracer.Start(c.Request.Context(), "bot.connection.register",
		trace.WithSpanKind(trace.SpanKindInternal))
	regSpan.SetAttributes(
		attribute.String("astron.token_id", botToken),
		attribute.String("astron.client_addr", clientAddr),
	)

	if err := app.Bridge.RegisterBot(ctx, botToken, botConn); err != nil {
		regSpan.End()
		log.Error().Err(err).Str("token", botToken).Msg("Failed to register bot")
		conn.Close()
		return
	}
	regSpan.End()

	log.Info().Str("token", botToken).Str("from", clientAddr).Msg("Bot connected")
	app.Bridge.NotifyBotConnected(botToken)

	defer func() {
		unregCtx, unregSpan := wsTracer.Start(context.Background(), "bot.connection.unregister",
			trace.WithSpanKind(trace.SpanKindInternal))
		unregSpan.SetAttributes(
			attribute.String("astron.token_id", botToken),
		)
		cleanupCtx, cancel := context.WithTimeout(unregCtx, 10*time.Second)
		defer cancel()
		app.Bridge.UnregisterBot(cleanupCtx, botToken, botConn)
		unregSpan.End()
	}()

	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Info().Str("token", botToken).Str("from", clientAddr).Msg("Bot disconnected normally")
			} else if websocket.IsUnexpectedCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Info().Str("token", botToken).Str("from", clientAddr).Err(err).Msg("Bot disconnected unexpectedly")
			} else {
				log.Error().Err(err).Str("token", botToken).Msg("Bot connection error")
			}
			return
		}

		app.Bridge.HandleBotMessage(context.Background(), botToken, string(message))
	}
}
