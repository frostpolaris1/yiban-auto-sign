package main

import (
	"strings"
	"testing"
)

func TestRedactProbeTextMultipleURLs(t *testing.T) {
	in := "first https://a.example/p?token=A1 failed; second https://b.example/q?token=B2"
	got := redactProbeText(in)
	for _, secret := range []string{"A1", "B2"} {
		if strings.Contains(got, secret) {
			t.Errorf("redacted text still contains %q: %s", secret, got)
		}
	}
	if strings.Count(got, "token=***") != 2 {
		t.Errorf("want two redacted token values, got: %s", got)
	}
	for _, want := range []string{"https://a.example/p", "https://b.example/q"} {
		if !strings.Contains(got, want) {
			t.Errorf("lost %q: %s", want, got)
		}
	}
}

func TestRedactProbeURLDropsUserinfo(t *testing.T) {
	got := redactProbeText("https://alice:secret@example.com/path?token=SECRET")
	for _, secret := range []string{"alice", "secret", "SECRET"} {
		if strings.Contains(got, secret) {
			t.Errorf("redacted URL still contains %q: %s", secret, got)
		}
	}
	if !strings.HasPrefix(got, "https://example.com/path?") {
		t.Errorf("host/path lost: %s", got)
	}
	if !strings.Contains(got, "token=***") {
		t.Errorf("query value not redacted: %s", got)
	}
}

func TestRedactProbeURLEmptyQueryValues(t *testing.T) {
	got := redactProbeText("https://x/p?a=&b=&c=1")
	for _, want := range []string{"a=***", "b=***", "c=***"} {
		if !strings.Contains(got, want) {
			t.Errorf("want %q in %s", want, got)
		}
	}
	if strings.Contains(got, "=&") {
		t.Errorf("empty query value left unredacted: %s", got)
	}
}

func TestRedactProbeURLInvalid(t *testing.T) {
	got := redactProbeURL("http://[::1/bad?x=SECRET")
	if got != "[redacted-url]" {
		t.Fatalf("redactProbeURL(invalid) = %q, want [redacted-url]", got)
	}
	if strings.Contains(got, "SECRET") {
		t.Fatalf("invalid URL leaked secret: %s", got)
	}
}

func TestRedactProbeTextTrailingQuestionMark(t *testing.T) {
	t.Run("text keeps sentence question mark", func(t *testing.T) {
		in := `Post "https://x/y?": boom`
		if got := redactProbeText(in); got != in {
			t.Fatalf("redactProbeText(%q) = %q, want unchanged", in, got)
		}
	})
	t.Run("single URL drops empty query", func(t *testing.T) {
		if got := redactProbeURL("https://x/y?"); got != "https://x/y" {
			t.Fatalf("redactProbeURL(trailing ?) = %q, want https://x/y", got)
		}
	})
}
