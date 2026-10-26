package yiban

import (
	"sync"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

const maxCachedResults = 1024

// resultCache 按 AttemptID 缓存签到结果。
// 同键并发由 per-key 锁串行；容量上限按任意键淘汰。
type resultCache struct {
	mu      sync.Mutex
	results map[string]contract.Result
	locks   map[string]*sync.Mutex
}

func newResultCache() *resultCache {
	return &resultCache{
		results: make(map[string]contract.Result),
		locks:   make(map[string]*sync.Mutex),
	}
}

func (c *resultCache) lockFor(key string) *sync.Mutex {
	c.mu.Lock()
	defer c.mu.Unlock()
	lock := c.locks[key]
	if lock == nil {
		lock = &sync.Mutex{}
		c.locks[key] = lock
	}
	return lock
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
