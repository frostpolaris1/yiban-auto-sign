// Package retention 清理过期数据（D 批 P2-15）。
//
// # 功能
// 按保留期删三张表：过期会话、过期重置令牌、旧签到任务行。
// 每批有上限，跑完一批再跑下一批；单次失败只记日志，不退出进程。
//
// # 归属
// app 层用例。端口见本文件：Store 接 adapters/pg；本包不写 SQL、不发 HTTP。
//
// # 复用
// Service、Deps、Pruned；Start、Interval。
//
// # 口径
// **审计链不清**：audit_logs 是 append-only，清理只能走迁移级操作，
// 否则保留期与不可篡改互相打架。图片保留期归 app/photos，不在这里。
// 任务行保留 TaskRetentionDays 天（按业务日切点算，不看时刻）。
package retention

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// 保留期与批量口径。
const (
	// TaskRetentionDays 是签到任务行的保留天数。
	// 报表按 30 天看，留 90 天给对账与复盘。
	TaskRetentionDays = 90
	// BatchSize 是单批删除上限：长事务会挡住认领与登录。
	BatchSize = 1000
	// Interval 是清理间隔。保留期以天计，不需要更密。
	Interval = 6 * time.Hour
	// stopTimeout 是停机时等当前一轮收尾的上限。
	stopTimeout = 5 * time.Second
)

// Store 是清理动作的存储端口。
type Store interface {
	DeleteExpiredSessions(ctx context.Context, before time.Time, limit int) (int64, error)
	DeleteExpiredPasswordResets(ctx context.Context, before time.Time, limit int) (int64, error)
	DeleteTasksBefore(ctx context.Context, before string, limit int) (int64, error)
}

// Deps 是清理服务的依赖。Location 决定"多少天前"按哪个时区数。
type Deps struct {
	Store    Store
	Clock    coreclock.Clock
	Location *time.Location
	Logger   *slog.Logger
}

// Service 是清理用例。
type Service struct {
	deps Deps
	log  *slog.Logger
}

// Pruned 是一轮清理删掉的行数。
type Pruned struct {
	Sessions    int64
	ResetTokens int64
	Tasks       int64
}

// NewService 构造清理服务。缺端口即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("retention: nil store")
	}
	if deps.Clock == nil {
		return nil, errors.New("retention: nil clock")
	}
	if deps.Location == nil {
		return nil, errors.New("retention: nil location")
	}
	log := deps.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Service{deps: deps, log: log}, nil
}

// Prune 跑一轮清理：三张表各自跑到不足一批为止。
// 任一表失败即返回已删数与该错误；调用方决定是否记日志。
func (s *Service) Prune(ctx context.Context) (Pruned, error) {
	now := s.deps.Clock.Now()
	var out Pruned
	var err error
	if out.Sessions, err = s.drain(ctx, func(limit int) (int64, error) {
		return s.deps.Store.DeleteExpiredSessions(ctx, now, limit)
	}); err != nil {
		return out, err
	}
	if out.ResetTokens, err = s.drain(ctx, func(limit int) (int64, error) {
		return s.deps.Store.DeleteExpiredPasswordResets(ctx, now, limit)
	}); err != nil {
		return out, err
	}
	cutoff := now.In(s.deps.Location).AddDate(0, 0, -TaskRetentionDays).Format("2006-01-02")
	if out.Tasks, err = s.drain(ctx, func(limit int) (int64, error) {
		return s.deps.Store.DeleteTasksBefore(ctx, cutoff, limit)
	}); err != nil {
		return out, err
	}
	return out, nil
}

// drain 按批删除直到不足一批。
func (s *Service) drain(ctx context.Context, del func(limit int) (int64, error)) (int64, error) {
	var total int64
	for {
		n, err := del(BatchSize)
		total += n
		if err != nil {
			return total, err
		}
		if n < BatchSize {
			return total, nil
		}
		if err := ctx.Err(); err != nil {
			return total, err
		}
	}
}

// Start 起周期清理，返回停止函数。首轮立即执行，之后每 Interval 一次。
// 单轮失败只记日志：清理失败不该让进程退出或挡住签到。
func Start(ctx context.Context, svc *Service, logger *slog.Logger) func() {
	log := logger
	if log == nil {
		log = slog.Default()
	}
	runCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(Interval)
		defer ticker.Stop()
		for {
			svc.runOnce(runCtx, log)
			select {
			case <-runCtx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	var once sync.Once
	return func() {
		once.Do(func() {
			cancel()
			select {
			case <-done:
			case <-time.After(stopTimeout):
				log.Warn("retention: loop did not stop in time")
			}
		})
	}
}

// runOnce 跑一轮并记结果。取消不是错误。
func (s *Service) runOnce(ctx context.Context, log *slog.Logger) {
	pruned, err := s.Prune(ctx)
	if err != nil {
		if ctx.Err() == nil {
			log.Warn("retention: prune failed", "error", err.Error())
		}
		return
	}
	if pruned.Sessions+pruned.ResetTokens+pruned.Tasks > 0 {
		log.Info("retention: pruned", "sessions", pruned.Sessions,
			"reset_tokens", pruned.ResetTokens, "tasks", pruned.Tasks)
	}
}
