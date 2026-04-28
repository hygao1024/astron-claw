package client

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/hygao1024/astron-claw/probe/model"
)

// ChatResult holds the parsed result of a chat SSE stream.
type ChatResult struct {
	SessionID string
	Content   string // final content from the "done" event
	HasError  bool
	ErrorMsg  string
	TTFB      time.Duration // time to first byte (first SSE data line)
}

// Chat sends a message to /bridge/chat and reads the SSE stream until done or error.
func Chat(ctx context.Context, baseURL, token, message string) (*ChatResult, error) {
	body, _ := json.Marshal(map[string]string{"content": message})
	req, err := http.NewRequestWithContext(ctx, "POST", baseURL+"/bridge/chat", bytes.NewReader(body))
	if err != nil {
		return nil, model.NewCodeError(-1, "new request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)

	requestStart := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, model.NewCodeError(-1, "request failed: %v", err)
	}
	defer resp.Body.Close()

	// Non-SSE error response (JSON body)
	if resp.StatusCode != http.StatusOK {
		var cr model.CodeResponse
		json.NewDecoder(resp.Body).Decode(&cr)
		code := cr.Code
		if code == 0 {
			code = resp.StatusCode
		}
		return nil, model.NewCodeError(code, "http %d: %s", resp.StatusCode, cr.Error)
	}

	result := &ChatResult{}
	scanner := bufio.NewScanner(resp.Body)
	var eventType string
	firstData := true

	for scanner.Scan() {
		line := scanner.Text()

		if strings.HasPrefix(line, "event: ") {
			eventType = strings.TrimPrefix(line, "event: ")
			continue
		}

		if strings.HasPrefix(line, "data: ") {
			if firstData {
				result.TTFB = time.Since(requestStart)
				firstData = false
			}
			data := strings.TrimPrefix(line, "data: ")

			switch eventType {
			case "session":
				var se model.SessionEvent
				json.Unmarshal([]byte(data), &se)
				result.SessionID = se.SessionID

			case "done":
				var ce model.ContentEvent
				json.Unmarshal([]byte(data), &ce)
				result.Content = ce.Content
				return result, nil

			case "error":
				var ce model.ContentEvent
				json.Unmarshal([]byte(data), &ce)
				result.HasError = true
				result.ErrorMsg = ce.Content
				return result, nil
			}

			eventType = ""
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, model.NewCodeError(-1, "read stream: %v", err)
	}

	// Stream ended without done/error
	if result.SessionID == "" {
		return nil, model.NewCodeError(-1, "stream ended without session event")
	}
	return nil, model.NewCodeError(-1, "stream ended without done or error event")
}
