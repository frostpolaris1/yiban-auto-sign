package yiban

import (
	"sync"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

const (
	maxCachedResults = 1024
	lockStripes      = 64
)

// resultCache 按 AttemptID 缓存签到结果。
// 同键并发由固定数量的分片锁串行；容量上限按任意键淘汰。
type resultCache struct {
	mu      sync.Mutex
	results map[string]contract.Result
	locks   [lockStripes]sync.Mutex
}

func newResultCache() *resultCache {
	return &resultCache{results: make(map[string]contract.Result)}
}

// lockFor 返回键所属的分片锁。分片而非每键一锁：内存有界。
func (c *resultCache) lockFor(key string) *sync.Mutex {
	var hash uint32 = 2166136261
	for i := 0; i < len(key); i++ {
		hash ^= uint32(key[i])
		hash *= 16777619
	}
	return &c.locks[hash%lockStripes]
}

func (c *resultCache) get(key string) (contract.Result, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	result, ok := c.results[key]
	return result, ok
}

func (c *resultCache) put(key string, result contract.Result) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.results) >= maxCachedResults {
		for existing := range c.results {
			if existing != key {
				delete(c.results, existing)
				break
			}
		}
	}
	c.results[key] = result
}
