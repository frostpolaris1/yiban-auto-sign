package auth

// 口令哈希的并发闸与端口。
//
// # 功能
// 定义 Hasher 端口；PlainHasher 直接调用包级算法；Bounded 用信号量
// 限制同时运行的哈希与校验。本文件**不**读写数据库、**不**决定口令策略。
//
// # 归属
// core 层纯领域包。用例层（app/auth、app/user）持有 Hasher，
// 装配层决定生产用 Bounded 还是测试替身。
//
// # 口径
// 并发上限按**整个进程**计：同一个 Bounded 实例必须传给全部用例。
// 槽位在分配 Argon2 内存之前取得，故并发上限就是内存上限。
// 排队等槽位受调用方 ctx 约束；ctx 取消即返回错误，不无限堆积。

import (
	"context"
	"errors"
	"fmt"

	"golang.org/x/sync/semaphore"
)

// Hasher 是口令哈希端口。
// Hash 生成 argon2id PHC 串；Verify 校验新旧两种格式。
// 两个动作都可能消耗几十毫秒与 19 MiB 内存，生产实现必须设并发上限。
type Hasher interface {
	Hash(ctx context.Context, password string) (string, error)
	Verify(ctx context.Context, password, encoded string) (PasswordCheck, error)
}

// PlainHasher 直接调用包级算法，不设并发上限。
// **只用于测试与一次性工具**；服务装配必须用 Bounded。
type PlainHasher struct{}

// Hash 实现 Hasher。
func (PlainHasher) Hash(_ context.Context, password string) (string, error) {
	return HashPassword(password)
}

// Verify 实现 Hasher。
func (PlainHasher) Verify(_ context.Context, password, encoded string) (PasswordCheck, error) {
	return VerifyPassword(password, encoded)
}

// Bounded 用信号量限制同时运行的哈希与校验。
// 零值不可用；请用 NewBounded 构造。
type Bounded struct {
	sem *semaphore.Weighted
}

// NewBounded 构造并发上限为 n 的哈希闸。
// n < 1 按 1 处理：静默放行 0 会让每次调用永久阻塞。
func NewBounded(n int) *Bounded {
	if n < 1 {
		n = 1
	}
	return &Bounded{sem: semaphore.NewWeighted(int64(n))}
}

// Hash 取得槽位后生成哈希。
func (b *Bounded) Hash(ctx context.Context, password string) (string, error) {
	var out string
	err := b.run(ctx, func() error {
		hash, err := HashPassword(password)
		out = hash
		return err
	})
	return out, err
}

// Verify 取得槽位后校验口令。
func (b *Bounded) Verify(ctx context.Context, password, encoded string) (PasswordCheck, error) {
	var out PasswordCheck
	err := b.run(ctx, func() error {
		check, err := VerifyPassword(password, encoded)
		out = check
		return err
	})
	return out, err
}

// run 是并发闸的唯一实现点。测试直接调用它观察并发峰值。
func (b *Bounded) run(ctx context.Context, f func() error) error {
	if b == nil || b.sem == nil {
		return errors.New("auth: nil KDF limiter")
	}
	if err := b.sem.Acquire(ctx, 1); err != nil {
		return fmt.Errorf("auth: wait for a KDF slot: %w", err)
	}
	defer b.sem.Release(1)
	return f()
}
