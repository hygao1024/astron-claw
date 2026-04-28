package infra

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/config"
)

func InitRedis(cfg config.RedisConfig) (redis.UniversalClient, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	var rdb redis.UniversalClient

	if cfg.IsCluster() {
		rdb = redis.NewClusterClient(&redis.ClusterOptions{
			Addrs:        cfg.Addrs,
			Password:     cfg.Password,
			PoolSize:     cfg.PoolSize,
			MinIdleConns: cfg.MinIdleConns,
		})
	} else {
		rdb = redis.NewClient(&redis.Options{
			Addr:         cfg.Addrs[0],
			Password:     cfg.Password,
			DB:           cfg.DB,
			PoolSize:     cfg.PoolSize,
			MinIdleConns: cfg.MinIdleConns,
		})
	}

	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis ping: %w", err)
	}

	mode := "standalone"
	addr := cfg.Addrs[0]
	if cfg.IsCluster() {
		mode = "cluster"
		addr = strings.Join(cfg.Addrs, ",")
	}

	log.Info().
		Str("mode", mode).
		Str("addr", addr).
		Msg("Redis connected")

	return rdb, nil
}

func CloseRedis(rdb redis.UniversalClient) {
	if rdb == nil {
		return
	}
	if err := rdb.Close(); err != nil {
		log.Error().Err(err).Msg("Failed to close Redis")
	} else {
		log.Info().Msg("Redis connection closed")
	}
}
