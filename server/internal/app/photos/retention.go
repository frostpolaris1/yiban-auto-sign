// 本文件实现图片保留期的日循环。
//
// # 功能
// Run 启动即清理一轮，随后每 interval 清理一轮。
// 每轮先删到期行，再回收无引用 blob；失败记错并继续下一轮。
//
// # 归属
// app/photos 的后台组件。装配点见 cmd/server 的 buildStack。
//
// # 复用
// RetentionLoop、RetentionOptions；DefaultRetentionInterval、DefaultPurgeBatch。
//
// # 口径
// 周期是常量，不读配置。测试用 RetentionOptions.Interval 注入短周期。
// ctx 取消即返回；单轮内每个 blob 之间检查取消。

package photos

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"
)

const (
	// DefaultRetentionInterval 是生产清理周期：一天一轮。
	DefaultRetentionInterval = 24 * time.Hour
	// DefaultPurgeBatch 是单轮内每次删除的行数上限。
	// 与统一分页上限同值；更大的批次只延长单事务持锁时间。
	DefaultPurgeBatch = 200
	// RetentionStopTimeout 是停止函数等待循环退出的上限。
	RetentionStopTimeout = 10 * time.Second
)

// RetentionOptions 是清理循环的构造参数。
type RetentionOptions struct {
	// Service 是用例服务，不得为 nil。
	Service *Service
	// Logger 是循环日志；nil 用 slog.Default。
	Logger *slog.Logger
	// Interval 是两轮之间的间隔；0 用 DefaultRetentionInterval。
	Interval time.Duration
	// Batch 是每次删除的行数上限；0 用 DefaultPurgeBatch。
	Batch int
}

// RetentionLoop 是按固定周期执行保留期清理的组件。
type RetentionLoop struct {
	svc      *Service
	log      *slog.Logger
	interval time.Duration
	batch    int
}

// NewRetentionLoop 构造清理循环。缺服务或参数越界即失败。
func NewRetentionLoop(opts RetentionOptions) (*RetentionLoop, error) {
	if opts.Service == nil {
		return nil, errors.New("photos: nil retention service")
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.Interval == 0 {
		opts.Interval = DefaultRetentionInterval
	}
	if opts.Interval < 0 {
		return nil, errors.New("photos: retention interval must not be negative")
	}
	if opts.Batch == 0 {
		opts.Batch = DefaultPurgeBatch
	}
	if opts.Batch < 1 || opts.Batch > DefaultPurgeBatch {
		return nil, fmt.Errorf("photos: retention batch must be between 1 and %d", DefaultPurgeBatch)
	}
	return &RetentionLoop{
		svc: opts.Service, log: opts.Logger,
		interval: opts.Interval, batch: opts.Batch,
	}, nil
}

// Run 启动即清理一轮，之后按 interval 清理，直到 ctx 取消。
func (l *RetentionLoop) Run(ctx context.Context) {
	l.sweep(ctx)
	ticker := time.NewTicker(l.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			l.sweep(ctx)
		}
	}
}

// sweep 执行一轮并记日志。错误只记录：下一轮照常执行。
func (l *RetentionLoop) sweep(ctx context.Context) {
	result, err := l.svc.Sweep(ctx, l.batch)
	if err != nil {
		l.log.Error("photo retention sweep failed",
			"error", err.Error(),
			"rows_deleted", result.RowsDeleted,
			"blobs_deleted", result.BlobsDeleted)
		return
	}
	l.log.Info("photo retention sweep",
		"rows_deleted", result.RowsDeleted,
		"blobs_deleted", result.BlobsDeleted)
}

// StartRetention 在后台启动生产口径的清理循环，返回停止函数。
//
// 装配层只需一行调用；停循环的等待逻辑留在这里，不复制到入口件。
// 返回的 stop 幂等：取消 ctx、等待循环退出，最多等 RetentionStopTimeout。
// ctx 取消同样停止循环。
func StartRetention(ctx context.Context, svc *Service, logger *slog.Logger) (func(), error) {
	loop, err := NewRetentionLoop(RetentionOptions{Service: svc, Logger: logger})
	if err != nil {
		return nil, err
	}
	log := logger
	if log == nil {
		log = slog.Default()
	}
	runCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		loop.Run(runCtx)
	}()
	var once sync.Once
	stop := func() {
		once.Do(func() {
			cancel()
			select {
			case <-done:
			case <-time.After(RetentionStopTimeout):
				log.Warn("photo retention loop did not stop within the retention stop timeout")
			}
		})
	}
	return stop, nil
}
