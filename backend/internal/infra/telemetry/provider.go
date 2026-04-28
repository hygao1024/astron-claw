package telemetry

import (
	"context"
	"time"

	"github.com/rs/zerolog/log"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	otellog "go.opentelemetry.io/otel/log/global"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"

	"astron-claw/backend/internal/config"
)

var (
	meterProvider  *sdkmetric.MeterProvider
	tracerProvider *sdktrace.TracerProvider
	loggerProvider *sdklog.LoggerProvider
)

// Init initializes OTel MeterProvider and/or TracerProvider with OTLP gRPC exporter.
func Init(ctx context.Context, otlpCfg config.OtlpConfig) error {
	if !otlpCfg.Enabled {
		log.Info().Msg("OTLP telemetry disabled (OTLP_ENABLED=false)")
		return nil
	}

	res := resource.NewWithAttributes(
		semconv.SchemaURL,
		semconv.ServiceNameKey.String(otlpCfg.ServiceName),
	)

	if otlpCfg.MetricsEnabled {
		if err := initMetrics(ctx, otlpCfg, res); err != nil {
			return err
		}
	}

	if otlpCfg.TracesEnabled {
		if err := initTraces(ctx, otlpCfg, res); err != nil {
			return err
		}
	}

	if otlpCfg.LogsEnabled {
		if err := initLogs(ctx, otlpCfg, res); err != nil {
			return err
		}
	}

	return nil
}

func initMetrics(ctx context.Context, otlpCfg config.OtlpConfig, res *resource.Resource) error {
	opts := []otlpmetricgrpc.Option{
		otlpmetricgrpc.WithEndpoint(otlpCfg.Endpoint),
	}
	if otlpCfg.Insecure {
		opts = append(opts, otlpmetricgrpc.WithInsecure())
	}

	exporter, err := otlpmetricgrpc.New(ctx, opts...)
	if err != nil {
		return err
	}

	// Custom bucket boundaries
	requestDurationView := sdkmetric.NewView(
		sdkmetric.Instrument{Name: "bridge.chat.request.duration"},
		sdkmetric.Stream{
			Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
				Boundaries: RequestDurationBuckets,
			},
		},
	)
	streamDurationView := sdkmetric.NewView(
		sdkmetric.Instrument{Name: "bridge.chat.stream.duration"},
		sdkmetric.Stream{
			Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
				Boundaries: StreamDurationBuckets,
			},
		},
	)

	meterProvider = sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(
			sdkmetric.NewPeriodicReader(exporter,
				sdkmetric.WithInterval(10*time.Second),
			),
		),
		sdkmetric.WithView(requestDurationView, streamDurationView),
	)
	otel.SetMeterProvider(meterProvider)

	log.Info().
		Str("service", otlpCfg.ServiceName).
		Str("endpoint", otlpCfg.Endpoint).
		Msg("OTLP metrics exporter initialised (gRPC)")

	return nil
}

// Shutdown gracefully shuts down all providers.
func Shutdown() {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if meterProvider != nil {
		if err := meterProvider.Shutdown(ctx); err != nil {
			log.Error().Err(err).Msg("OTLP metrics shutdown error")
		}
		meterProvider = nil
	}

	if tracerProvider != nil {
		if err := tracerProvider.Shutdown(ctx); err != nil {
			log.Error().Err(err).Msg("OTLP traces shutdown error")
		}
		tracerProvider = nil
	}

	if loggerProvider != nil {
		if err := loggerProvider.Shutdown(ctx); err != nil {
			log.Error().Err(err).Msg("OTLP logs shutdown error")
		}
		loggerProvider = nil
	}

	log.Info().Msg("OTLP telemetry shut down")
}

func initTraces(ctx context.Context, otlpCfg config.OtlpConfig, res *resource.Resource) error {
	opts := []otlptracegrpc.Option{
		otlptracegrpc.WithEndpoint(otlpCfg.Endpoint),
	}
	if otlpCfg.Insecure {
		opts = append(opts, otlptracegrpc.WithInsecure())
	}

	exporter, err := otlptracegrpc.New(ctx, opts...)
	if err != nil {
		return err
	}

	tracerProvider = sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tracerProvider)
	otel.SetTextMapPropagator(propagation.TraceContext{})

	log.Info().
		Str("service", otlpCfg.ServiceName).
		Str("endpoint", otlpCfg.Endpoint).
		Msg("OTLP traces exporter initialised (gRPC)")

	return nil
}

func initLogs(ctx context.Context, otlpCfg config.OtlpConfig, res *resource.Resource) error {
	opts := []otlploggrpc.Option{
		otlploggrpc.WithEndpoint(otlpCfg.Endpoint),
	}
	if otlpCfg.Insecure {
		opts = append(opts, otlploggrpc.WithInsecure())
	}

	exporter, err := otlploggrpc.New(ctx, opts...)
	if err != nil {
		return err
	}

	loggerProvider = sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)),
		sdklog.WithResource(res),
	)
	otellog.SetLoggerProvider(loggerProvider)

	log.Info().
		Str("service", otlpCfg.ServiceName).
		Str("endpoint", otlpCfg.Endpoint).
		Msg("OTLP logs exporter initialised (gRPC)")

	return nil
}
