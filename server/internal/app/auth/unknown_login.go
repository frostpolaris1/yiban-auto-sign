// 未知邮箱登录失败的审计采样。
//
// # 功能
// 公开登录端点在邮箱不存在时也会走一次 dummy KDF 并留一条审计；
// 不设上限时，任何人可用随机邮箱刷爆不可自动清理的 audit_logs。
// 本采样器按「客户端 IP 的进程盐哈希」分桶，每桶每分钟最多放行一条审计；
// 未放行的尝试仍然写 Warn，并由调用方付一次空事务把数据库往返补齐。
// 放行后审计写失败的桶会标记 failed：同窗口内被抑制的尝试继续尝试写审计，
// 直到成功为止，避免审计故障时未知邮箱路径变成「先 500 后 401」而可枚举。
//
// # 归属
// app/auth 内部实现。键只在本文件生成，不 import 入口层、HTTP 或限流包。
//
// # 口径
// - 键 = SHA-256(进程随机盐 || 客户端 IP)，不保存明文 IP；日志只写前 16 位 hex。
// - 无 IP / 无 ClientMeta 共用空串 fallback 桶，仍限每分钟 1 条。
// - 表上限 4096；满且是新键时抑制审计（仍 Warn），表长不增。
// - 每次 allow 前先惰性回收窗口外键；进程内单副本实现，多副本各自计数。
// - 审计写失败由调用方按 fail-closed 返回错误；桶标记 failed 后，
//   同窗口内被抑制的尝试仍重试写审计，恢复后只多写一条再回到静默。

package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

const (
	// unknownLoginAuditWindow 是同一 IP 桶的审计最短间隔。
	unknownLoginAuditWindow = time.Minute
	// unknownLoginAuditMaxEntries 是进程内桶数上限，防止随机 IP 撑爆内存。
	unknownLoginAuditMaxEntries = 4096
)

// unknownLoginSampler 是未知邮箱登录审计的按 IP 采样器。
// 零值不可用；用 newUnknownLoginSampler 构造。
type unknownLoginSampler struct {
	clock clock.Clock

	mu   sync.Mutex
	salt [32]byte
	seen map[string]unknownLoginBucket
}

// unknownLoginBucket 是一个 IP 的采样窗口。
// auditFailed 记录本窗口内放行审计是否写失败；失败后同窗口的抑制尝试
// 必须继续重试审计，否则故障恢复时会暴露「未知邮箱 401、已知邮箱 500」。
type unknownLoginBucket struct {
	at          time.Time
	auditFailed bool
}

// newUnknownLoginSampler 构造采样器。盐取自 crypto/rand；
// 取随机失败时回退到注入时钟与对象地址的哈希——熵弱但不 panic，
// 且不使用 time.Now，满足 canonical 的 clock.now 规则。
func newUnknownLoginSampler(clk clock.Clock) *unknownLoginSampler {
	s := &unknownLoginSampler{
		clock: clk,
		seen:  make(map[string]unknownLoginBucket),
	}
	if _, err := rand.Read(s.salt[:]); err != nil {
		s.salt = sha256.Sum256([]byte(fmt.Sprintf("%d|%p", clk.Now().UTC().UnixNano(), s)))
	}
	return s
}

// allow 判断本次未知邮箱失败是否应写审计，并返回日志用的 IP 指纹。
// 指纹对空 IP 固定为 "unknown"，其余为 SHA-256 键的前 16 位小写 hex。
// 第三个返回值 retry 表示本窗口此前放行的审计写失败，调用方即使被抑制
// 也必须继续尝试写入；返回 false 时调用方仍须写 Warn。本方法从不返回错误。
func (s *unknownLoginSampler) allow(ip string) (bool, string, bool) {
	now := s.clock.Now().UTC()

	s.mu.Lock()
	defer s.mu.Unlock()

	for key, bucket := range s.seen {
		if now.Sub(bucket.at) >= unknownLoginAuditWindow {
			delete(s.seen, key)
		}
	}

	if ip == "" {
		if bucket, ok := s.seen[""]; ok {
			return false, "unknown", bucket.auditFailed
		}
		if len(s.seen) >= unknownLoginAuditMaxEntries {
			return false, "unknown", false
		}
		s.seen[""] = unknownLoginBucket{at: now}
		return true, "unknown", false
	}

	hasher := sha256.New()
	_, _ = hasher.Write(s.salt[:])
	_, _ = hasher.Write([]byte(ip))
	key := hex.EncodeToString(hasher.Sum(nil))
	fingerprint := key[:16]

	if bucket, ok := s.seen[key]; ok {
		return false, fingerprint, bucket.auditFailed
	}
	if len(s.seen) >= unknownLoginAuditMaxEntries {
		return false, fingerprint, false
	}
	s.seen[key] = unknownLoginBucket{at: now}
	return true, fingerprint, false
}

// markAuditFailed 记录本窗口放行的审计是否写失败。
// 键不存在时新建一个当前窗口的桶，保证失败状态至少保留到下一次清扫。
func (s *unknownLoginSampler) markAuditFailed(ip string, failed bool) {
	key := s.key(ip)
	now := s.clock.Now().UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	bucket, ok := s.seen[key]
	if !ok {
		bucket = unknownLoginBucket{at: now}
	}
	bucket.auditFailed = failed
	s.seen[key] = bucket
}

// key 返回采样桶键；空 IP 共用空串 fallback 桶。
func (s *unknownLoginSampler) key(ip string) string {
	if ip == "" {
		return ""
	}
	hasher := sha256.New()
	_, _ = hasher.Write(s.salt[:])
	_, _ = hasher.Write([]byte(ip))
	return hex.EncodeToString(hasher.Sum(nil))
}
