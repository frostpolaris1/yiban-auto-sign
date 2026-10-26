package main

import (
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
)

// E-11：装配后的进程在真实 TCP 端口上应答存活探针。
func TestHealthzOnRealPort(t *testing.T) {
	cfg := &config.Config{Env: "test", HTTPAddr: "127.0.0.1:0", Timezone: "Asia/Shanghai"}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	srv, err := assembly.NewServer(cfg, logger)
	if err != nil {
		t.Fatalf("newServer: %v", err)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	go func() { _ = srv.Serve(ln) }()
	defer srv.Close()

	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://" + ln.Addr().String() + "/healthz")
	if err != nil {
		t.Fatalf("get healthz: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var body struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Status != "ok" {
		t.Fatalf("status = %q, want ok", body.Status)
	}
	if got := resp.Header.Get("Cache-Control"); got != "" {
		t.Fatalf("healthz cache-control = %q, want empty", got)
	}
}
