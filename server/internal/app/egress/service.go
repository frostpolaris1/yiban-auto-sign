package egress

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"sync"
	"time"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// Deps 是出口分配用例的依赖。
// Clock 注入当前时刻：健康冷却判定必须可测，不读墙钟。
type Deps struct {
	Store Store
	Clock coreclock.Clock
}

// Service 是出口分配服务。健康状态存进程内存：单进程是明文不变量。
type Service struct {
	deps Deps

	mu        sync.Mutex
	unhealthy map[string]time.Time // egressID -> 冷却截止
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("egress: nil store")
	}
	if deps.Clock == nil {
		return nil, errors.New("egress: nil clock")
	}
	return &Service{deps: deps, unhealthy: map[string]time.Time{}}, nil
}

// Assign 返回账号的出口：已有绑定原样返回（故障也不迁移）；
// 无绑定时在启用且健康的出口中确定性选择并落库。
func (s *Service) Assign(ctx context.Context, accountID string) (Egress, error) {
	if accountID == "" {
		return Egress{}, coreerrs.New(coreerrs.CodeValidation, "account is required")
	}
	enabled, err := s.deps.Store.ListEnabled(ctx)
	if err != nil {
		return Egress{}, err
	}
	if len(enabled) == 0 {
		return Egress{}, coreerrs.New(coreerrs.CodeValidation, "no enabled egress")
	}
	// 已有绑定：直接返回（含不健康出口——粘性优先）。
	if id, err := s.deps.Store.GetAssignment(ctx, accountID); err == nil {
		return lookup(enabled, id), nil
	} else if !isNotFound(err) {
		return Egress{}, err
	}
	preferred := s.pick(enabled, accountID, s.deps.Clock.Now())
	finalID, err := s.deps.Store.AssignOrGet(ctx, accountID, preferred.ID)
	if err != nil {
		return Egress{}, err
	}
	return lookup(enabled, finalID), nil
}

// MarkFailed 把出口标为不健康直到 until：新账号不再选它，
// 已绑定账号不迁移（隔离而不是漂移）。
func (s *Service) MarkFailed(egressID string, until time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.unhealthy[egressID] = until
}

// Healthy 报告出口在 now 是否健康。
func (s *Service) Healthy(egressID string, now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	until, ok := s.unhealthy[egressID]
	return !ok || !now.Before(until)
}

// pick 在健康出口中按账号确定性取模；全不健康时退化为全部启用出口
// （宁可慢也不把账号判成"无出口"）。
func (s *Service) pick(enabled []Egress, accountID string, now time.Time) Egress {
	candidates := make([]Egress, 0, len(enabled))
	for _, e := range enabled {
		if s.Healthy(e.ID, now) {
			candidates = append(candidates, e)
		}
	}
	if len(candidates) == 0 {
		candidates = enabled
	}
	sum := sha256.Sum256([]byte(accountID))
	idx := binary.BigEndian.Uint64(sum[:8]) % uint64(len(candidates))
	return candidates[idx]
}

// lookup 按 ID 取出口；找不到返回零值（绑定指向已删出口属数据异常）。
func lookup(enabled []Egress, id string) Egress {
	for _, e := range enabled {
		if e.ID == id {
			return e
		}
	}
	return Egress{ID: id}
}
