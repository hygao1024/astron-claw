package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/rs/zerolog/log"

	"astron-claw/backend/internal/config"
)

type S3Storage struct {
	cfg    config.StorageConfig
	client *s3.Client
	// httpClient is a test seam for exercising HTTPS upload behavior against local servers.
	httpClient aws.HTTPClient
}

func NewS3Storage(cfg config.StorageConfig) *S3Storage {
	return &S3Storage{cfg: cfg}
}

func (s *S3Storage) Start() error {
	resolver := aws.EndpointResolverWithOptionsFunc(
		func(service, region string, options ...interface{}) (aws.Endpoint, error) {
			return aws.Endpoint{
				URL:               s.cfg.Endpoint,
				HostnameImmutable: s.cfg.PathStyle,
			}, nil
		},
	)

	awsCfg, err := awsconfig.LoadDefaultConfig(context.Background(),
		awsconfig.WithRegion(s.cfg.Region),
		awsconfig.WithCredentialsProvider(
			credentials.NewStaticCredentialsProvider(s.cfg.AccessKey, s.cfg.SecretKey, ""),
		),
		awsconfig.WithEndpointResolverWithOptions(resolver),
		awsconfig.WithHTTPClient(s.httpClient),
	)
	if err != nil {
		return fmt.Errorf("load aws config: %w", err)
	}

	s.client = s3.NewFromConfig(awsCfg, func(o *s3.Options) {
		o.UsePathStyle = s.cfg.PathStyle
		// OSS-compatible endpoints reject the SDK's default HTTPS trailer checksum uploads.
		o.RequestChecksumCalculation = aws.RequestChecksumCalculationWhenRequired
	})

	log.Info().Str("endpoint", s.cfg.Endpoint).Bool("path_style", s.cfg.PathStyle).Msg("S3 client initialised")
	return nil
}

func (s *S3Storage) Close() error {
	log.Info().Msg("S3 client closed")
	return nil
}

func (s *S3Storage) EnsureBucket() error {
	ctx := context.Background()

	// Check if bucket exists
	_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{
		Bucket: &s.cfg.Bucket,
	})
	if err == nil {
		log.Info().Str("bucket", s.cfg.Bucket).Msg("S3 bucket already exists, skipping policy/lifecycle setup")
		return nil
	}
	log.Warn().Err(err).Str("bucket", s.cfg.Bucket).Msg("HeadBucket failed, attempting to create bucket")

	// Create bucket
	_, err = s.client.CreateBucket(ctx, &s3.CreateBucketInput{
		Bucket: &s.cfg.Bucket,
	})
	if err != nil {
		return fmt.Errorf("create bucket: %w", err)
	}
	log.Info().Str("bucket", s.cfg.Bucket).Msg("S3 bucket created")

	// Set public read policy
	if s.cfg.PublicRead {
		policy := map[string]interface{}{
			"Version": "2012-10-17",
			"Statement": []map[string]interface{}{
				{
					"Effect":    "Allow",
					"Principal": "*",
					"Action":    "s3:GetObject",
					"Resource":  fmt.Sprintf("arn:aws:s3:::%s/*", s.cfg.Bucket),
				},
			},
		}
		policyJSON, _ := json.Marshal(policy)
		policyStr := string(policyJSON)
		_, err = s.client.PutBucketPolicy(ctx, &s3.PutBucketPolicyInput{
			Bucket: &s.cfg.Bucket,
			Policy: &policyStr,
		})
		if err != nil {
			log.Warn().Err(err).Msg("Failed to set bucket policy")
		}
	} else {
		log.Warn().Str("bucket", s.cfg.Bucket).Msg("S3 bucket created without public-read policy (OSS_PUBLIC_READ=false)")
	}

	// Set 7-day lifecycle
	_, err = s.client.PutBucketLifecycleConfiguration(ctx, &s3.PutBucketLifecycleConfigurationInput{
		Bucket: &s.cfg.Bucket,
		LifecycleConfiguration: &types.BucketLifecycleConfiguration{
			Rules: []types.LifecycleRule{
				{
					ID:     aws.String("expire-media-7d"),
					Status: types.ExpirationStatusEnabled,
					Expiration: &types.LifecycleExpiration{
						Days: aws.Int32(7),
					},
					Filter: &types.LifecycleRuleFilter{Prefix: aws.String("")},
				},
			},
		},
	})
	if err != nil {
		log.Warn().Err(err).Msg("Failed to set bucket lifecycle")
	}

	log.Info().Str("bucket", s.cfg.Bucket).Msg("S3 bucket configured (public-read + 7d lifecycle)")
	return nil
}

func (s *S3Storage) PutObject(key string, body io.Reader, contentType string, contentLength int64) (string, error) {
	ctx := context.Background()

	// Ensure text types include charset
	if strings.HasPrefix(contentType, "text/") && !strings.Contains(contentType, "charset") {
		contentType = contentType + "; charset=utf-8"
	}

	input := &s3.PutObjectInput{
		Bucket:      &s.cfg.Bucket,
		Key:         &key,
		Body:        body,
		ContentType: &contentType,
	}
	if contentLength > 0 {
		input.ContentLength = &contentLength
	}

	t0 := time.Now()
	_, err := s.client.PutObject(ctx, input)
	elapsed := time.Since(t0)

	if err != nil {
		log.Error().Err(err).Str("key", key).Dur("took", elapsed).Msg("S3 put failed")
		return "", fmt.Errorf("s3 put: %w", err)
	}

	log.Info().Str("key", key).Str("type", contentType).Dur("took", elapsed).Msg("S3 put")

	// URL-encode the key for the download URL
	encodedKey := url.PathEscape(key)
	// Restore forward slashes (PathEscape encodes them)
	encodedKey = strings.ReplaceAll(encodedKey, "%2F", "/")
	return fmt.Sprintf("%s/%s/%s", s.cfg.PublicEndpoint, s.cfg.Bucket, encodedKey), nil
}

func (s *S3Storage) BucketName() string {
	return s.cfg.Bucket
}
