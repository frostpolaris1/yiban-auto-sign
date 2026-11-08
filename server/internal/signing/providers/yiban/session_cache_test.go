package yiban

// 会话槽容量与清理的守卫单测。
//
// 判据：槽位不超过 maxSessions；最久未见被淘汰；空闲超过会话 TTL
// 或已过期的槽被清理；并发取槽不越界。本文件不访问平台。

import (
	"fmt"
	"net/http"
	"sync"
	"testing"
	"time"
)

func newSessionCache(t *testing.T) (*Client, *manualClock) {
	t.Helper()
	clock := &manualClock{now: time.Date(2026, 11, 8, 6, 0, 0, 0, time.UTC)}
	return New(Config{Clock: clock}), clock
}

func sessionCount(c *Client) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.sessions)
}

func hasSession(c *Client, id string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.sessions[id]
	return ok
}

func TestSessionCacheIsBounded(t *testing.T) {
	c, clock := newSessionCache(t)
	for i := 0; i < maxSessions+200; i++ {
		clock.Advance(time.Millisecond)
		c.entry(fmt.Sprintf("acct-%d", i))
	}
	if got := sessionCount(c); got > maxSessions {
		t.Fatalf("session slots = %d, want <= %d", got, maxSessions)
	}
}

func TestSessionCacheEvictsLeastRecentlySeen(t *testing.T) {
	c, clock := newSessionCache(t)
	for i := 0; i < maxSessions; i++ {
		clock.Advance(time.Millisecond)
		c.entry(fmt.Sprintf("acct-%d", i))
	}
	// 触碰 acct-0，使它不再是 LRU；新键应淘汰 acct-1。
	c.entry("acct-0")
	c.entry("acct-new")
	if !hasSession(c, "acct-0") {
		t.Fatal("recently used session was evicted")
	}
	if hasSession(c, "acct-1") {
		t.Fatal("least recently used session survived the cap")
	}
	if !hasSession(c, "acct-new") {
		t.Fatal("new session was not inserted")
	}
	if got := sessionCount(c); got > maxSessions {
		t.Fatalf("session slots = %d, want <= %d", got, maxSessions)
	}
}

func TestSessionCacheSweepsIdleEntries(t *testing.T) {
	c, clock := newSessionCache(t)
	c.entry("acct-idle")
	clock.Advance(c.ttl + 2*sessionSweepInterval)
	c.entry("acct-trigger")
	if hasSession(c, "acct-idle") {
		t.Fatal("idle session survived the TTL sweep")
	}
	if !hasSession(c, "acct-trigger") {
		t.Fatal("the trigger session is missing")
	}
}

func TestSessionCacheSweepsExpiredSessions(t *testing.T) {
	c, clock := newSessionCache(t)
	entry := c.entry("acct-expired")
	entry.mu.Lock()
	entry.http = &http.Client{}
	entry.expires = clock.Now()
	entry.mu.Unlock()

	clock.Advance(2 * sessionSweepInterval)
	c.entry("acct-trigger")
	if hasSession(c, "acct-expired") {
		t.Fatal("expired session survived the sweep")
	}
}

func TestClearSessionDoesNotRecreateEvictedSlot(t *testing.T) {
	c, _ := newSessionCache(t)
	before := sessionCount(c)
	c.clearSession("acct-missing")
	if got := sessionCount(c); got != before {
		t.Fatalf("clearSession created %d slots, want %d", got-before, 0)
	}
}

func TestSessionCacheConcurrentAccessIsBounded(t *testing.T) {
	c, clock := newSessionCache(t)
	var wg sync.WaitGroup
	for worker := 0; worker < 32; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for i := 0; i < maxSessions/8; i++ {
				c.entry(fmt.Sprintf("acct-%d-%d", worker, i))
			}
		}(worker)
	}
	// 时钟推进只由测试 goroutine 做，避免 manualClock 串行化平台路径。
	for i := 0; i < 20; i++ {
		clock.Advance(time.Millisecond)
		time.Sleep(time.Millisecond)
	}
	wg.Wait()
	if got := sessionCount(c); got > maxSessions {
		t.Fatalf("session slots = %d, want <= %d", got, maxSessions)
	}
}
