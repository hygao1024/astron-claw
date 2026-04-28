package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/joho/godotenv"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/config"
	"astron-claw/backend/internal/infra"
	"astron-claw/backend/internal/infra/storage"
	"astron-claw/backend/internal/infra/telemetry"
	"astron-claw/backend/internal/router"
	"astron-claw/backend/internal/service"
)

func main() {
	_ = godotenv.Load()

	cfg := config.Load()
	infra.SetupLogger(cfg.Server.LogLevel)
	ctx := context.Background()

	// Initialize MySQL
	db, err := infra.InitDB(ctx, cfg.MySQL, cfg.DBPool)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to initialise MySQL")
	}

	// Initialize Redis
	rdb, err := infra.InitRedis(cfg.Redis)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to initialise Redis")
	}

	// Initialize OTLP telemetry
	if err := telemetry.Init(ctx, cfg.OTLP); err != nil {
		log.Fatal().Err(err).Msg("Failed to initialise OTLP telemetry")
	}
	telemetry.EnsureInstruments()
	telemetry.EnsureLogger()
	telemetry.SetRedisForMetrics(rdb)

	// Run database migrations
	if err := infra.RunMigrations(ctx, cfg.MySQL, rdb); err != nil {
		log.Fatal().Err(err).Msg("Failed to run database migrations")
	}

	// Initialize services
	tokenMgr := service.NewTokenManager(db, rdb)
	adminAuth := service.NewAdminAuth(db, rdb)

	// Initialize object storage
	store := storage.NewStorage(cfg.Storage)
	if err := store.Start(); err != nil {
		log.Fatal().Err(err).Msg("Failed to start object storage")
	}
	if err := store.EnsureBucket(); err != nil {
		log.Fatal().Err(err).Msg("Failed to ensure storage bucket")
	}
	mediaMgr := service.NewMediaManager(store)

	// Initialize queue
	queue, err := service.NewQueue(cfg.Queue.Type, rdb, cfg.Queue.MaxStreamLen)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to create message queue")
	}

	// Initialize session store and bridge
	sessionStore := service.NewSessionStore(db, rdb)
	bridge := service.NewConnectionBridge(rdb, sessionStore, queue)
	bridge.SetWorkerInboxConsumers(cfg.Server.WorkerInboxConsumers)
	bridge.Start()

	// Initialize bot status monitor
	botStatusMonitor := service.NewBotStatusMonitor(rdb)
	botStatusMonitor.Start()

	// Read Pod IP for metrics
	podIP := os.Getenv("POD_IP")
	if podIP == "" {
		podIP = "unknown"
	}

	// Build the app and router
	app := &router.App{
		DB:               db,
		RDB:              rdb,
		TokenMgr:         tokenMgr,
		AdminAuth:        adminAuth,
		MediaMgr:         mediaMgr,
		Bridge:           bridge,
		Queue:            queue,
		Storage:          store,
		Config:           cfg,
		BotStatusMonitor: botStatusMonitor,
	}
	r := router.SetupRouter(app, podIP)

	// Start HTTP server
	addr := fmt.Sprintf("%s:%d", cfg.Server.Host, cfg.Server.Port)
	srv := &http.Server{
		Addr:              addr,
		Handler:           r,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	// Graceful shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Info().Str("addr", addr).Msg("Astron Claw Bridge Server started")
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error().Err(err).Msg("Server listen error")
			quit <- syscall.SIGTERM
		}
	}()

	<-quit

	log.Info().Msg("Shutting down server...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error().Err(err).Msg("Server forced to shutdown")
	}

	bridge.Shutdown()
	botStatusMonitor.Stop()
	telemetry.Shutdown()

	if err := store.Close(); err != nil {
		log.Error().Err(err).Msg("Failed to close storage")
	}
	infra.CloseRedis(rdb)
	infra.CloseDB(db)

	log.Info().Msg("Astron Claw Bridge Server stopped")
}
