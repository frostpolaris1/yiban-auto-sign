package auth

// 投递文案与投递槽位的内部不变式。
//
// # 功能
// 钉住两条只有包内可见的性质：
//   1. 正文的有效期文案由 ResetTokenTTL 派生，不写死数字；
//   2. 投递槽位满时丢弃并留显性记录，调用方不被阻塞。
// 本文件**不**测 HTTP、**不**连数据库（那些由 auth_test 与 cmd/server 的 e2e 钉）。
//
// # 口径
// 槽位容量由测试直接指定，不依赖 maxConcurrentResetMails 的当前数值：
// 被测性质是「满即丢弃」，不是「容量是 4」。

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
)

// syncBuf 是并发安全的日志缓冲：投递在后台 goroutine 里写日志。
type syncBuf struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuf) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuf) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// 正文的有效期文案必须跟着令牌有效期走。
// 各档位都取非 1 小时值：写死「1 小时内有效」会在此红。
func TestResetTTLTextFollowsTokenTTL(t *testing.T) {
	cases := []struct {
		ttl  time.Duration
		want string
	}{
		{30 * time.Minute, "30 分钟内有效"},
		{2 * time.Hour, "2 小时内有效"},
		{45 * time.Second, "尽快使用"},
	}
	for _, tc := range cases {
		if got := resetTTLText(tc.ttl); got != tc.want {
			t.Fatalf("resetTTLText(%v) = %q, want %q", tc.ttl, got, tc.want)
		}
		body := resetMailText("https://app.example.test/reset-password?token=x", tc.ttl)
		if !strings.Contains(body, tc.want) {
			t.Fatalf("正文没有含 %q（ttl=%v）：\n%s", tc.want, tc.ttl, body)
		}
		if !strings.Contains(body, "https://app.example.test/reset-password?token=x") {
			t.Fatalf("正文没有含链接（ttl=%v）：\n%s", tc.ttl, body)
		}
	}
	// 当前令牌有效期落在整小时档，正文说法必须与它一致。
	if got := resetTTLText(ResetTokenTTL); !strings.Contains(resetMailText("L", ResetTokenTTL), got) {
		t.Fatalf("正文与 ResetTokenTTL=%v 的说法不一致：%q", ResetTokenTTL, got)
	}
}

// blockingSender 阻塞在 Send，直到 release 关闭。
// 它用 started 报告「已占用一个投递槽位」。
type blockingSender struct {
	started chan struct{}
	release chan struct{}
}

func (b *blockingSender) Send(context.Context, coremail.Message) error {
	select {
	case b.started <- struct{}{}:
	default:
	}
	<-b.release
	return nil
}

// 槽位满时丢弃第二封，并留一条可查的记录；调用方不被阻塞。
func TestDeliverResetDropsWhenSlotsAreFull(t *testing.T) {
	logs := &syncBuf{}
	sender := &blockingSender{started: make(chan struct{}, 2), release: make(chan struct{})}
	svc := &Service{
		log:       slog.New(slog.NewTextHandler(logs, nil)),
		mail:      sender,
		mailBase:  "https://app.example.test",
		mailSlots: make(chan struct{}, 1),
	}
	svc.deliverReset("first@example.com", "tok-1")
	select {
	case <-sender.started:
	case <-time.After(2 * time.Second):
		close(sender.release)
		t.Fatal("第一封没有进入投递")
	}

	// 槽位已满：第二次调用必须立即返回，并记录原因。
	done := make(chan struct{})
	go func() {
		svc.deliverReset("second@example.com", "tok-2")
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		close(sender.release)
		t.Fatal("槽位满时 deliverReset 阻塞了调用方")
	}
	if got := logs.String(); !strings.Contains(got, "delivery_queue_full") {
		close(sender.release)
		t.Fatalf("丢弃没有留下可查的记录：\n%s", got)
	}

	close(sender.release)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := svc.Close(ctx); err != nil {
		t.Fatalf("Close 没有等在途投递结束: %v", err)
	}
}
