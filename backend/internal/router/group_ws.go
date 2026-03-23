package router

import (
	"context"
	"encoding/json"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/pkg"
	"astron-claw/backend/internal/service"
)

func (app *App) wsGroupChat(c *gin.Context) {
	groupID := c.Param("groupId")

	// Extract token from query or header
	token := c.Query("token")
	if token == "" {
		token = c.GetHeader("x-api-key")
	}
	if token == "" {
		raw := c.GetHeader("Authorization")
		if len(raw) >= 7 && (raw[:7] == "Bearer " || raw[:7] == "bearer ") {
			token = raw[7:]
		}
	}

	// Validate token
	if !app.TokenMgr.Validate(c.Request.Context(), token) {
		conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		msg := websocket.FormatCloseMessage(model.ErrWSInvalidToken.Code, model.ErrWSInvalidToken.Message)
		_ = conn.WriteMessage(websocket.CloseMessage, msg)
		conn.Close()
		return
	}

	// Validate group exists and has agents
	group, err := app.GroupMgr.Get(c.Request.Context(), groupID)
	if err != nil || group == nil {
		conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		msg := websocket.FormatCloseMessage(model.ErrWSGroupNotFound.Code, model.ErrWSGroupNotFound.Message)
		_ = conn.WriteMessage(websocket.CloseMessage, msg)
		conn.Close()
		return
	}

	agents, err := app.GroupMgr.GetAgents(c.Request.Context(), groupID)
	if err != nil || len(agents) == 0 {
		conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		msg := websocket.FormatCloseMessage(model.ErrWSGroupNoAgents.Code, model.ErrWSGroupNoAgents.Message)
		_ = conn.WriteMessage(websocket.CloseMessage, msg)
		conn.Close()
		return
	}

	// Upgrade
	conn, err := wsUpgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Error().Err(err).Msg("Group WS upgrade failed")
		return
	}

	connID := uuid.New().String()[:12]
	wsConn := &service.GroupWSConn{
		ID:   connID,
		Conn: conn,
	}

	app.GroupChat.RegisterConn(groupID, wsConn)
	defer app.GroupChat.UnregisterConn(groupID, connID)

	// Send session event with group info and agents
	agentList := make([]map[string]interface{}, len(agents))
	for i, a := range agents {
		agentList[i] = map[string]interface{}{
			"token": pkg.SafePrefix(a.Token, 10) + "...",
			"name":  a.Name,
			"role":  a.Role,
		}
	}

	_ = wsConn.WriteJSON(map[string]interface{}{
		"type":    "session",
		"groupId": groupID,
		"agents":  agentList,
	})

	log.Info().Str("group", groupID).Str("conn", connID).Msg("Group chat client connected")

	// Set up ping ticker for keepalive
	pingTicker := time.NewTicker(30 * time.Second)
	defer pingTicker.Stop()

	go func() {
		for range pingTicker.C {
			if err := wsConn.WriteJSON(map[string]interface{}{"type": "ping"}); err != nil {
				return
			}
		}
	}()

	// Read loop
	for {
		_, rawMsg, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				log.Info().Str("group", groupID).Str("conn", connID).Msg("Group chat client disconnected normally")
			} else {
				log.Debug().Err(err).Str("group", groupID).Str("conn", connID).Msg("Group chat client disconnected")
			}
			return
		}

		var msg map[string]interface{}
		if err := json.Unmarshal(rawMsg, &msg); err != nil {
			continue
		}

		msgType, _ := msg["type"].(string)
		if msgType == "pong" {
			continue
		}

		if msgType == "message" {
			content, _ := msg["content"].(string)
			if content == "" {
				continue
			}

			// Parse media
			var mediaURLs []map[string]interface{}
			if mediaRaw, ok := msg["media"].([]interface{}); ok {
				for _, m := range mediaRaw {
					if mMap, ok := m.(map[string]interface{}); ok {
						mediaURLs = append(mediaURLs, mMap)
					}
				}
			}

			// Send to all agents (sessions are created lazily inside SendMessageToGroup)
			if err := app.GroupChat.SendMessageToGroup(context.Background(), groupID, content, mediaURLs); err != nil {
				log.Error().Err(err).Str("group", groupID).Msg("Failed to send group message")
				_ = wsConn.WriteJSON(map[string]interface{}{
					"type":    "error",
					"content": "Failed to send message",
				})
			}
		}
	}
}
