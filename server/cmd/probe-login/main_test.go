package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

const probeSecretURL = "https://api.uyiban.com/base/c/auth/yiban?verifyRequest=SECRET&CSRF=TOKEN"

func TestRedactProbeText(t *testing.T) {
	in := fmt.Sprintf("Post %q: dial tcp: timeout", probeSecretURL)
	got := redactProbeText(in)
	for _, secret := range []string{"SECRET", "TOKEN"} {
		if strings.Contains(got, secret) {
			t.Fatalf("redacted text still contains %q: %s", secret, got)
		}
	}
	if !strings.Contains(got, "api.uyiban.com") {
		t.Errorf("host lost: %s", got)
	}
	if !strings.Contains(got, "/base/c/auth/yiban") {
		t.Errorf("path lost: %s", got)
	}
	if !strings.Contains(got, "verifyRequest=***") || !strings.Contains(got, "CSRF=***") {
		t.Errorf("query name/value shape lost: %s", got)
	}
}

func TestRedactProbeTextClearsFragment(t *testing.T) {
	got := redactProbeText(probeSecretURL + "#frag")
	if strings.Contains(got, "frag") {
		t.Fatalf("fragment not cleared: %s", got)
	}
	if !strings.Contains(got, "api.uyiban.com") {
		t.Errorf("host lost: %s", got)
	}
}

func TestRedactProbeTextWithoutURL(t *testing.T) {
	const in = "context deadline exceeded"
	if got := redactProbeText(in); got != in {
		t.Fatalf("plain text changed: %q", got)
	}
}

func TestRedactProbeTextKeepsTrailingPunctuation(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "parenthesized URL",
			in:   "see (https://x/y?a=1) failed",
			want: "see (https://x/y?a=***) failed",
		},
		{
			name: "fullwidth comma followed by chinese",
			in:   "见https://x?a=1，稍后重试",
			want: "见https://x?a=***，稍后重试",
		},
		{
			name: "sentence period",
			in:   "see https://x/y?a=1.",
			want: "see https://x/y?a=***.",
		},
		{
			name: "bracketed URL",
			in:   "[https://x/y?a=1]",
			want: "[https://x/y?a=***]",
		},
		{
			name: "comma before space",
			in:   "https://x/y?a=1, next",
			want: "https://x/y?a=***, next",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := redactProbeText(tc.in); got != tc.want {
				t.Fatalf("redactProbeText(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestRedactProbeTextUnicodeURL(t *testing.T) {
	in := "Get \"https://例.example/路径?token=SECRET\": boom"
	got := redactProbeText(in)
	if strings.Contains(got, "SECRET") {
		t.Fatalf("unicode URL leaked secret: %s", got)
	}
	if !strings.Contains(got, "例.example") {
		t.Errorf("unicode host lost: %s", got)
	}
	if !strings.Contains(got, "token=***") {
		t.Errorf("query shape lost: %s", got)
	}
}

func TestRedactProbeTextInvalidURL(t *testing.T) {
	in := "Get \"http://[::1/bad?x=SECRET\": boom"
	got := redactProbeText(in)
	if !strings.Contains(got, "[redacted-url]") {
		t.Fatalf("invalid URL not replaced: %s", got)
	}
	if strings.Contains(got, "SECRET") {
		t.Fatalf("invalid URL leaked secret: %s", got)
	}
}

func TestProbeSummary(t *testing.T) {
	t.Run("ok", func(t *testing.T) {
		if got := probeSummary(nil); got != "ok" {
			t.Fatalf("probeSummary(nil) = %q, want ok", got)
		}
	})
	t.Run("deadline", func(t *testing.T) {
		if got := probeSummary(context.DeadlineExceeded); got != "timeout" {
			t.Fatalf("probeSummary(deadline) = %q, want timeout", got)
		}
	})
	t.Run("wrapped deadline", func(t *testing.T) {
		err := fmt.Errorf("verify: %w", context.DeadlineExceeded)
		if got := probeSummary(err); got != "timeout" {
			t.Fatalf("probeSummary(wrapped deadline) = %q, want timeout", got)
		}
	})
	t.Run("plain error", func(t *testing.T) {
		if got := probeSummary(errors.New("boom")); got != "error: boom" {
			t.Fatalf("probeSummary(plain) = %q, want error: boom", got)
		}
	})
	t.Run("URL error", func(t *testing.T) {
		got := probeSummary(fmt.Errorf("Post %q: boom", probeSecretURL))
		for _, secret := range []string{"SECRET", "TOKEN"} {
			if strings.Contains(got, secret) {
				t.Errorf("summary still contains %q: %s", secret, got)
			}
		}
		if !strings.Contains(got, "api.uyiban.com") || !strings.Contains(got, "/base/c/auth/yiban") {
			t.Errorf("summary lost host/path: %s", got)
		}
		if !strings.Contains(got, "verifyRequest=***") || !strings.Contains(got, "CSRF=***") {
			t.Errorf("summary lost query shape: %s", got)
		}
	})
}

type failingRT struct {
	err error
}

func (f failingRT) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, f.err
}

func TestCaptureRTPreservesWireCaptureAndRedactsSummary(t *testing.T) {
	req, err := http.NewRequest(http.MethodPost, probeSecretURL, nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	innerErr := fmt.Errorf("Post %q: dial tcp: timeout", probeSecretURL)
	cap := &captureRT{inner: failingRT{err: innerErr}}

	if _, roundErr := cap.RoundTrip(req); roundErr != innerErr {
		t.Fatalf("capture changed RoundTrip error: %v", roundErr)
	}
	if len(cap.records) != 1 {
		t.Fatalf("records = %d, want 1", len(cap.records))
	}
	rec := cap.records[0]
	if rec.URL != probeSecretURL {
		t.Fatalf("wire URL = %q, want raw URL %q", rec.URL, probeSecretURL)
	}
	if rec.Err != innerErr.Error() {
		t.Fatalf("wire error = %q, want raw error %q", rec.Err, innerErr.Error())
	}

	payload, err := json.Marshal(rec)
	if err != nil {
		t.Fatalf("marshal wire record: %v", err)
	}
	var decoded struct {
		URL string `json:"url"`
		Err string `json:"error"`
	}
	if err := json.Unmarshal(payload, &decoded); err != nil {
		t.Fatalf("unmarshal wire record: %v", err)
	}
	if decoded.URL != probeSecretURL || !strings.Contains(decoded.URL, "verifyRequest=SECRET") || !strings.Contains(decoded.URL, "CSRF=TOKEN") {
		t.Fatalf("captured JSON URL is not raw: %q", decoded.URL)
	}
	if decoded.Err != innerErr.Error() {
		t.Fatalf("captured JSON error = %q, want raw error", decoded.Err)
	}

	summary := probeSummary(innerErr)
	for _, secret := range []string{"SECRET", "TOKEN"} {
		if strings.Contains(summary, secret) {
			t.Errorf("summary still contains %q: %s", secret, summary)
		}
	}
	if !strings.Contains(summary, "verifyRequest=***") || !strings.Contains(summary, "CSRF=***") {
		t.Errorf("summary query not redacted: %s", summary)
	}
	if !strings.Contains(summary, "api.uyiban.com") || !strings.Contains(summary, "/base/c/auth/yiban") {
		t.Errorf("summary lost host/path: %s", summary)
	}
}
