package router

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/pkg"
	"astron-claw/backend/internal/service"
)

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
		msg := websocket.FormatCloseMessage(model.ErrWSInvalidToken.Code, model.ErrWSInvalidToken.Message)
		_ = conn.WriteMessage(websocket.CloseMessage, msg)
		conn.Close()
		tp := pkg.SafePrefix(botToken, 10)
		log.Warn().Str("token", tp).Msg("Bot connection rejected: invalid token")
		return
	}

	if app.Bridge.IsDraining() {
		conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			log.Error().Err(err).Msg("WS upgrade failed for draining worker")
			return
		}
		msg := websocket.FormatCloseMessage(model.ErrWSServerRestart.Code, model.ErrWSServerRestart.Message)
		_ = conn.WriteMessage(websocket.CloseMessage, msg)
		conn.Close()
		tp := pkg.SafePrefix(botToken, 10)
		log.Info().Str("token", tp).Msg("Bot connection rejected: worker draining")
		return
	}

	conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Error().Err(err).Msg("WS upgrade failed")
		return
	}

	clientAddr := c.ClientIP()
	tp := pkg.SafePrefix(botToken, 10)
	botConn := &service.BotConn{
		Conn:  conn,
		Token: botToken,
	}

	ctx := c.Request.Context()
	if err := app.Bridge.RegisterBot(ctx, botToken, botConn); err != nil {
		log.Error().Err(err).Str("token", tp).Msg("Failed to register bot")
		conn.Close()
		return
	}

	log.Info().Str("token", tp).Str("from", clientAddr).Msg("Bot connected")
	app.Bridge.NotifyBotConnected(botToken)

	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		app.Bridge.UnregisterBot(cleanupCtx, botToken, botConn)
	}()

	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Info().Str("token", tp).Str("from", clientAddr).Msg("Bot disconnected normally")
			} else if websocket.IsUnexpectedCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Info().Str("token", tp).Str("from", clientAddr).Err(err).Msg("Bot disconnected unexpectedly")
			} else {
				log.Error().Err(err).Str("token", tp).Msg("Bot connection error")
			}
			return
		}

		app.Bridge.HandleBotMessage(context.Background(), botToken, string(message))
	}
}
