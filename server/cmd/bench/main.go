// Command bench 对真库压测四项指标并输出可机读结果（P8-C）。
//
// # 功能
//
// 向 PostgreSQL（经 internal/adapters/pg 现有接入）灌合成账号，
// 单校或多校两种形状，然后测：
//
//  1. ClaimDue 吞吐：internal/adapters/pg/schedule.go 的
//     (*ScheduleRepo).ClaimDue，单 worker 循环领空。
//  2. 计划构建耗时：internal/app/schedule/service.go 的
//     (*Service).Build，每校任务一构建。判据 10k 账号全窗构建 ≤60s。
//  3. SSE 订阅扇出：internal/app/events/broker.go 的 (*Broker).Subscribe，
//     订阅 N 端＋发布一条到全部订阅端。
//  4. 内存峰值：runtime.ReadMemStats，判据 ≤1 GiB。//
// 本二进制只测量，不优化。发现判据达不到时如实报数字。
//
// # 输出契约
//
// 一行一个指标，空格分隔的 key=value；每行带 shape 与 accounts 口径字段。
// bench_meta 行带机器口径（CPU、核数、Go 版本、OS）与库形状。
//
// # 口径
//
//   - 造数：合成值运行期拼号。手机号＝"138"+8 位零填充序号，自
//     13800000000 起连续 100000 个（已登记 tests/synthetic-fixtures.json
//     的 phone_runtime_blocks）；邮箱＝bench-<序号>@example.com。
//   - Discoverer 用固定窗口替身（非平台实拍）：测的是仓库与构建路径，
//     不是平台发现。
//   - 内存峰值取阶段边界采样，不强制 GC；Sys 取末次采样。
//   - 认领吞吐＝单 worker 循环 ClaimDue 直到领空；行数/墙钟。
//   - 每次运行独占一个临时 schema，结束即删（--keep 保留）。
//
// # 用法
//
//	bench -dsn "$YIBAN_TEST_DATABASE_URL" -accounts 10000 -schools 1
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

func main() {
	var (
		dsn        = flag.String("dsn", "", "PostgreSQL DSN；缺省读 YIBAN_TEST_DATABASE_URL")
		accounts   = flag.Int("accounts", 10000, "合成账号数")
		schools    = flag.Int("schools", 1, "学校数；1=单校形状，>1=多校形状")
		claimBatch = flag.Int("claim-batch", 500, "单次 ClaimDue 的 limit")
		keep       = flag.Bool("keep", false, "保留压测 schema 不删除")
	)
	flag.Parse()
	env := config.Env{}
	if *dsn == "" {
		if v, ok := env.Lookup("YIBAN_TEST_DATABASE_URL"); ok {
			*dsn = v
		}
	}
	if *dsn == "" {
		fmt.Fprintln(os.Stderr, "bench: 需要 -dsn 或环境变量 YIBAN_TEST_DATABASE_URL")
		os.Exit(2)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	res, err := Run(ctx, Config{
		DSN:        *dsn,
		Accounts:   *accounts,
		Schools:    *schools,
		ClaimBatch: *claimBatch,
		KeepSchema: *keep,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "bench:", err)
		os.Exit(1)
	}
	for _, line := range Format(res) {
		fmt.Println(line)
	}
}
