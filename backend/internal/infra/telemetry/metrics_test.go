package telemetry

import (
	"testing"
)

func TestEnsureInstruments(t *testing.T) {
	// EnsureInstruments should not panic and should create non-nil instruments
	EnsureInstruments()

	if ChatRequestTotal == nil {
		t.Error("ChatRequestTotal is nil")
	}
	if ChatRequestDuration == nil {
		t.Error("ChatRequestDuration is nil")
	}
	if ChatStreamDuration == nil {
		t.Error("ChatStreamDuration is nil")
	}
}

func TestTokenPrefix(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"sk-abcdefghij1234", "sk-abcdefg..."},
		{"sk-abc", "sk-abc"},
		{"", ""},
		{"12345678901234567890", "1234567890..."},
	}
	for _, tt := range tests {
		result := TokenPrefix(tt.input)
		if result != tt.expected {
			t.Errorf("TokenPrefix(%q) = %q, want %q", tt.input, result, tt.expected)
		}
	}
}

func TestBucketBoundaries(t *testing.T) {
	if len(RequestDurationBuckets) == 0 {
		t.Error("RequestDurationBuckets should not be empty")
	}
	if len(StreamDurationBuckets) == 0 {
		t.Error("StreamDurationBuckets should not be empty")
	}

	// Verify sorted ascending
	for i := 1; i < len(RequestDurationBuckets); i++ {
		if RequestDurationBuckets[i] <= RequestDurationBuckets[i-1] {
			t.Errorf("RequestDurationBuckets not sorted at index %d", i)
		}
	}
	for i := 1; i < len(StreamDurationBuckets); i++ {
		if StreamDurationBuckets[i] <= StreamDurationBuckets[i-1] {
			t.Errorf("StreamDurationBuckets not sorted at index %d", i)
		}
	}
}
