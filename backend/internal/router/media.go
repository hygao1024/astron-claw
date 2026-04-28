package router

import (
	"errors"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/middleware"
	"astron-claw/backend/internal/model"
	"astron-claw/backend/internal/service"
)

func (app *App) uploadMedia(c *gin.Context) {
	tokenStr := c.GetString("token")

	file, header, err := c.Request.FormFile("file")
	if err != nil {
		middleware.MetricsErrorResponse(c, model.ErrMediaInvalidFile)
		return
	}
	defer file.Close()

	sessionID := c.PostForm("sessionId")

	fileSize := header.Size
	if fileSize > service.MaxFileSize {
		log.Warn().Int64("size", fileSize).Msg("Media upload rejected: file too large")
		middleware.MetricsErrorResponse(c, model.ErrMediaFileTooLarge)
		return
	}

	mimeType := header.Header.Get("Content-Type")
	if mimeType == "" {
		mimeType = "application/octet-stream"
	}
	fileName := header.Filename
	if fileName == "" {
		fileName = "unnamed"
	}

	result, err := app.MediaMgr.Store(file, fileName, fileSize, mimeType, sessionID)
	if err != nil {
		if errors.Is(err, service.ErrFileTooLarge) {
			middleware.MetricsErrorResponse(c, model.ErrMediaFileTooLarge)
			return
		}
		if errors.Is(err, service.ErrFileEmpty) || errors.Is(err, service.ErrMIMERejected) {
			middleware.MetricsErrorResponse(c, model.ErrMediaInvalidFile)
			return
		}
		log.Error().Err(err).Msg("Media store failed")
		middleware.MetricsErrorResponse(c, model.ErrChatInternalError)
		return
	}

	log.Info().Str("name", fileName).Int64("size", fileSize).Str("token", tokenStr).
		Msg("Media uploaded")
	c.JSON(200, gin.H{
		"code":        0,
		"fileName":    result.FileName,
		"mimeType":    result.MimeType,
		"fileSize":    result.FileSize,
		"sessionId":   result.SessionID,
		"downloadUrl": result.DownloadURL,
	})
}
