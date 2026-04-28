package telemetry

import (
	"context"
	"encoding/json"
	"time"

	"go.opentelemetry.io/otel/log"
	otellog "go.opentelemetry.io/otel/log/global"
)

var ChatLogger log.Logger

func EnsureLogger() {
	ChatLogger = otellog.GetLoggerProvider().Logger("astron-claw/chat")
}

type ChatLogRecord struct {
	LogType     string  `json:"log_type"`
	AppID       string  `json:"appid"`
	SessionID   string  `json:"session_id"`
	FALR        float64 `json:"falr"`
	FAFR        float64 `json:"fafr"`
	Ret         string  `json:"Ret"`
	IP          string  `json:"ip"`
	TraceID     string  `json:"trace_id"`
	Func        string  `json:"func"`
	ServiceName string  `json:"service_name"`
}

func EmitChatLog(ctx context.Context, rec ChatLogRecord) {
	if rec.LogType == "" {
		rec.LogType = "server_log"
	}

	// Body: simple success/failed message
	data,_:=json.Marshal(rec)
	var r log.Record
	r.SetTimestamp(time.Now())
	r.SetBody(log.BytesValue(data))

	// All fields go to Attributes
	// r.AddAttributes(
	// 	log.String("log_type", rec.LogType),
	// 	log.String("appid", rec.AppID),
	// 	log.String("session_id", rec.SessionID),
	// 	log.Float64("falr", rec.FALR),
	// 	log.Float64("fafr", rec.FAFR),
	// 	log.Int("ret", rec.Ret),
	// 	log.String("ip", rec.IP),
	// 	log.String("trace_id", rec.TraceID),
	// 	log.String("func", rec.Func),
	// )

	ChatLogger.Emit(ctx, r)
}
