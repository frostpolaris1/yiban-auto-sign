package main

// bench 冒烟判据（P8-C，判据先行）。
//
// 测的是真库上的四项指标：造数、ClaimDue 吞吐、计划构建耗时、
// SSE 订阅扇出，外加内存峰值。计时断言只做宽松上限，
// 防机器差异假红；绝对判据（10k 构建≤60s、认领≥100 行/秒）
// 由 bench 二进制的 10k 实测数字对照，不在单测里钉死。
// 缺库必须显性 SKIP，不得静默显得绿（AGENTS §30）。

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

// dsnEnv 与 server/internal/adapters/pg 的 e2e 同名同源。
const dsnEnv = "YIBAN_TEST_DATABASE_URL"

func requireDSN(t *testing.T) string {
	t.Helper()
	dsn, _ := config.Env{}.Lookup(dsnEnv)
	dsn = strings.TrimSpace(dsn)
	if dsn == "" {
		t.Skip("SKIP: " + dsnEnv + " is not set. bench e2e did NOT run. " +
			"An unset database is never a green result.")
	}
	return dsn
}

func TestMain(m *testing.M) {
	dsn, _ := config.Env{}.Lookup(dsnEnv)
	if strings.TrimSpace(dsn) == "" {
		ci, _ := config.Env{}.Lookup("CI")
		if strings.TrimSpace(ci) != "" {
			os.Stderr.WriteString("FAIL: " + dsnEnv +
				" is not set in CI. bench e2e DID NOT RUN.\n")
			os.Exit(1)
		}
		os.Stdout.WriteString("\nSKIP: " + dsnEnv + " is not set. " +
			"bench e2e did NOT run. An unset database is never a green result.\n\n")
	}
	os.Exit(m.Run())
}

// buildCapSeconds 返回计划构建的宽松上限。
// 口径：判据 10k 账号全窗构建 ≤60s（P8 判据），按账号数线性缩放
// 再乘 10 倍松弛。小样本跑到上限即视为构建路径坏死。
func buildCapSeconds(accounts int) time.Duration {
	cap := 60 * float64(accounts) / 10000 * 10
	if cap < 5 {
		cap = 5
	}
	return time.Duration(cap * float64(time.Second))
}

// TestSmokeSingleSchool 单校形状小样本跑通一轮，输出可解析、各指标非零。
// 模拟的真实用户动作：管理员给一个学校任务铺当日计划，worker 认领到期任务。
// 断言的用户可见结果：全部账号被铺进计划并全部被认领。
func TestSmokeSingleSchool(t *testing.T) {
	dsn := requireDSN(t)
	ctx := context.Background()

	res, err := Run(ctx, Config{DSN: dsn, Accounts: 300, Schools: 1, ClaimBatch: 100})
	if err != nil {
		t.Fatalf("run bench: %v", err)
	}
	if res.SeedRows != 300 {
		t.Fatalf("seed rows = %d, want 300", res.SeedRows)
	}
	if res.ClaimRows != 300 {
		t.Fatalf("claim rows = %d, want 300（全部计划行应可认领）", res.ClaimRows)
	}
	if res.ClaimRowsPerSec() <= 0 {
		t.Fatalf("claim throughput = %v, want > 0", res.ClaimRowsPerSec())
	}
	if res.SeedSeconds <= 0 || res.ClaimSeconds <= 0 {
		t.Fatalf("seed/claim seconds must be positive, got %v/%v", res.SeedSeconds, res.ClaimSeconds)
	}
	if res.BuildSeconds <= 0 || res.BuildSeconds > buildCapSeconds(300).Seconds() {
		t.Fatalf("build seconds = %v, want (0, %v]", res.BuildSeconds, buildCapSeconds(300))
	}
	if res.SSESubscribers <= 0 || res.SSESubSeconds <= 0 {
		t.Fatalf("sse metrics must be non-zero, got subs=%d sub=%v",
			res.SSESubscribers, res.SSESubSeconds)
	}
	// fanout 是一次 1000 订阅者的内存发布，耗时小于本机时钟粒度，
	// 故经常测出 0 秒（2026-10-31 本机实测：最小非零间隔 543µs，
	// 连续两次 time.Now() 约 20 万对里有 199999 对相同；1000 通道扇出 20/20 次测出 0）。
	// 零不是"没跑到"——runSSE 逐个等订阅者收到，超时直接报错。
	// 判据保留上界：真的卡住会超过订阅超时（工单 5l1）。
	if res.SSEFanoutSeconds >= sseTimeout.Seconds() {
		t.Fatalf("sse fanout seconds = %v, must stay below the subscriber timeout %v",
			res.SSEFanoutSeconds, sseTimeout)
	}
	assertMemoryCap(t, res)
	assertFormat(t, res)
}

// TestSmokeMultiSchool 多校形状：三校分摊，计划按校构建，认领跨校聚合。
func TestSmokeMultiSchool(t *testing.T) {
	dsn := requireDSN(t)
	ctx := context.Background()

	res, err := Run(ctx, Config{DSN: dsn, Accounts: 60, Schools: 3, ClaimBatch: 50})
	if err != nil {
		t.Fatalf("run bench: %v", err)
	}
	if res.Shape != "multi" {
		t.Fatalf("shape = %q, want multi", res.Shape)
	}
	if res.SeedRows != 60 || res.ClaimRows != 60 {
		t.Fatalf("seed/claim rows = %d/%d, want 60/60", res.SeedRows, res.ClaimRows)
	}
	if res.BuildCount != 3 {
		t.Fatalf("build count = %d, want 3（每校一任务）", res.BuildCount)
	}
	assertMemoryCap(t, res)
}

func assertMemoryCap(t *testing.T, res Result) {
	t.Helper()
	const limit = 1 << 30 // 判据：内存峰值 ≤ 1 GiB
	if res.SysBytes > limit {
		t.Fatalf("sys bytes = %d, want <= %d", res.SysBytes, limit)
	}
	if res.HeapPeakBytes > limit {
		t.Fatalf("heap peak bytes = %d, want <= %d", res.HeapPeakBytes, limit)
	}
}

// assertFormat 钉输出契约：一行一个指标，键值对形状，口径字段在位。
func assertFormat(t *testing.T, res Result) {
	t.Helper()
	lines := Format(res)
	if len(lines) < 6 {
		t.Fatalf("format lines = %d, want >= 6（四指标＋内存＋元信息）", len(lines))
	}
	for i, line := range lines {
		if !strings.HasPrefix(line, "bench_") {
			t.Fatalf("line %d lacks bench_ prefix: %q", i, line)
		}
		fields := strings.Fields(line)
		if len(fields) < 4 {
			t.Fatalf("line %d has %d fields, want >= 4 key=value pairs: %q", i, len(fields), line)
		}
		for _, f := range fields {
			if !strings.Contains(f, "=") {
				t.Fatalf("line %d field %q is not key=value", i, f)
			}
		}
		if !strings.Contains(line, "shape=") || !strings.Contains(line, "accounts=") {
			t.Fatalf("line %d lacks caliber fields (shape=, accounts=): %q", i, line)
		}
	}
}
