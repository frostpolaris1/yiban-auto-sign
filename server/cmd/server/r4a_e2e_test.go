package main

// R4A e2e：保留期执行与 blob 回收。
//
// # 功能
// 用真实 PostgreSQL 与临时 blob 目录走完整链路：
// 到期行物理删除、软删行按删除时刻起算、无引用 blob 回收、
// 未满 1h 的新 blob 保留，以及日循环在装配处的启动与停止。
//
// # 归属
// cmd/server 的 e2e。并发的 app、adapters 与 entry 只能在这里拼装。
//
// # 口径
// 每个用例独占一个数据库 schema 与一个临时 blob 目录。
// 缺 DSN 时按 w2cNewEnv 的可见跳过横幅处理，绝不静默当绿。

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// r4aBlob 写入一份 blob 并返回摘要。
func (e *w2cEnv) r4aBlob(t *testing.T, data []byte) [32]byte {
	t.Helper()
	sha := sha256.Sum256(data)
	if err := e.photoBlob.Put(e.ctx, sha, data); err != nil {
		e.t.Fatalf("put blob: %v", err)
	}
	return sha
}

// r4aSetBlobAge 把 blob 的修改时间改成 now 之前。
func (e *w2cEnv) r4aSetBlobAge(t *testing.T, sha [32]byte, age time.Duration) {
	t.Helper()
	name := hex.EncodeToString(sha[:])
	path := filepath.Join(e.photoDir, name[:2], name)
	at := time.Now().Add(-age)
	if err := os.Chtimes(path, at, at); err != nil {
		e.t.Fatalf("chtimes %s: %v", name, err)
	}
}

// r4aBlobExists 报告 blob 文件是否仍在。
func (e *w2cEnv) r4aBlobExists(sha [32]byte) bool {
	name := hex.EncodeToString(sha[:])
	_, err := os.Stat(filepath.Join(e.photoDir, name[:2], name))
	return err == nil
}

// r4aPhotoRow 写一行图片记录；at 是 created_at。
func (e *w2cEnv) r4aPhotoRow(t *testing.T, owner string, seed byte, sha [32]byte, at time.Time) corephoto.Record {
	t.Helper()
	rec, err := e.photos.Create(e.ctx, corephoto.Record{
		OwnerUserID: owner, SHA256: sha, Mime: "image/jpeg",
		Width: 8, Height: 8, Bytes: 16, CreatedAt: at,
	})
	if err != nil {
		e.t.Fatalf("create photo row: %v", err)
	}
	return rec
}

// 一轮清理同时处理行与 blob，按各用户保留期与删除时刻判定。
func TestR4APhotoRetentionSweepsRowsAndBlobs(t *testing.T) {
	e := w2cNewEnv(t)
	now := clock.System{}.Now().UTC()
	userA := e.user("r4a-ret-a@example.com", "user")
	userB := e.user("r4a-ret-b@example.com", "user")
	userC := e.user("r4a-ret-c@example.com", "user")
	for id, days := range map[string]int{userA.ID: 30, userB.ID: 30, userC.ID: 365} {
		if err := e.photos.PutRetention(e.ctx, id, days, now); err != nil {
			t.Fatalf("put retention %d: %v", days, err)
		}
	}

	shared := e.r4aBlob(t, []byte("shared blob"))
	e.r4aSetBlobAge(t, shared, 3*time.Hour)
	onlyA := e.r4aBlob(t, []byte("only a blob"))
	e.r4aSetBlobAge(t, onlyA, 2*time.Hour)
	softOldBlob := e.r4aBlob(t, []byte("soft old blob"))
	e.r4aSetBlobAge(t, softOldBlob, 2*time.Hour)
	softNewBlob := e.r4aBlob(t, []byte("soft new blob"))
	e.r4aSetBlobAge(t, softNewBlob, 2*time.Hour)
	orphanStale := e.r4aBlob(t, []byte("orphan stale blob"))
	e.r4aSetBlobAge(t, orphanStale, 2*time.Hour)
	orphanFresh := e.r4aBlob(t, []byte("orphan fresh blob"))

	// A 的 31 天前行到期；B 引用同一摘要且未到期，文件必须留下。
	expiredA := e.r4aPhotoRow(t, userA.ID, 1, shared, now.Add(-31*24*time.Hour))
	keptB := e.r4aPhotoRow(t, userB.ID, 1, shared, now.Add(-5*24*time.Hour))
	expiredOnlyA := e.r4aPhotoRow(t, userA.ID, 2, onlyA, now.Add(-31*24*time.Hour))
	// 60 天前、保留期 365 天：留。
	keptLong := e.r4aPhotoRow(t, userC.ID, 3, softNewBlob, now.Add(-60*24*time.Hour))
	// 软删 31 天前：删；软删 1 天前：留。
	softOld := e.r4aPhotoRow(t, userA.ID, 4, softOldBlob, now.Add(-90*24*time.Hour))
	softNew := e.r4aPhotoRow(t, userA.ID, 5, softNewBlob, now.Add(-90*24*time.Hour))
	if _, err := e.photos.SoftDelete(e.ctx, corescope.User(userA.ID), softOld.ID, now.Add(-31*24*time.Hour)); err != nil {
		t.Fatalf("soft delete old: %v", err)
	}
	if _, err := e.photos.SoftDelete(e.ctx, corescope.User(userA.ID), softNew.ID, now.Add(-24*time.Hour)); err != nil {
		t.Fatalf("soft delete new: %v", err)
	}

	res, err := e.photoSvc.Sweep(e.ctx, 100)
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if res.RowsDeleted != 3 {
		t.Fatalf("rows_deleted = %d, want 3", res.RowsDeleted)
	}
	// onlyA、softOldBlob 与无引用的 orphanStale 可回收；
	// shared 仍被 B 引用，orphanFresh 未满 1h。
	if res.BlobsDeleted != 3 {
		t.Fatalf("blobs_deleted = %d, want 3", res.BlobsDeleted)
	}
	for name, id := range map[string]string{
		"A 到期行":    expiredA.ID,
		"A 独享行":    expiredOnlyA.ID,
		"软删 31 天前": softOld.ID,
	} {
		if left := e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", id); left != 0 {
			t.Fatalf("%s 未删除, rows=%d", name, left)
		}
	}
	for name, id := range map[string]string{
		"B 未到期行":   keptB.ID,
		"365 天保留行": keptLong.ID,
		"软删 1 天前行": softNew.ID,
	} {
		if left := e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", id); left != 1 {
			t.Fatalf("%s 被误删, rows=%d", name, left)
		}
	}
	for name, sha := range map[string][32]byte{
		"共享摘要": shared, "未满 1h 的新 blob": orphanFresh,
		"365 天行引用的 blob": softNewBlob,
	} {
		if !e.r4aBlobExists(sha) {
			t.Fatalf("%s 被误删", name)
		}
	}
	for name, sha := range map[string][32]byte{
		"独享已删行 blob": onlyA, "软删到期 blob": softOldBlob, "无引用陈旧 blob": orphanStale,
	} {
		if e.r4aBlobExists(sha) {
			t.Fatalf("%s 未被回收", name)
		}
	}
}

// 日循环：启动即清理；取消后 Run 在 1s 内返回。
func TestR4ARetentionLoopRunsAndStops(t *testing.T) {
	e := w2cNewEnv(t)
	now := clock.System{}.Now().UTC()
	owner := e.user("r4a-loop@example.com", "user")
	if err := e.photos.PutRetention(e.ctx, owner.ID, 30, now); err != nil {
		t.Fatalf("put retention: %v", err)
	}
	rec := e.r4aPhotoRow(t, owner.ID, 1, e.r4aBlob(t, []byte("loop blob")), now.Add(-31*24*time.Hour))
	loop, err := appphotos.NewRetentionLoop(appphotos.RetentionOptions{
		Service:  e.photoSvc,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
		Interval: 50 * time.Millisecond,
		Batch:    10,
	})
	if err != nil {
		t.Fatalf("NewRetentionLoop: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); loop.Run(ctx) }()

	deadline := time.Now().Add(2 * time.Second)
	for {
		if e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", rec.ID) == 0 {
			break
		}
		if time.Now().After(deadline) {
			cancel()
			t.Fatal("循环两跳内没有删到期行")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("取消后 Run 未在 1s 内返回")
	}
}

// 装配层真的启动清理循环：无调用者正是原始缺陷。
// buildStack 返回后首轮清理已排上，到期行必须在 2s 内消失；close 不得挂住。
func TestR4ABuildStackRunsRetentionLoop(t *testing.T) {
	e := w2cNewEnv(t)
	now := clock.System{}.Now().UTC()
	owner := e.user("r4a-wire@example.com", "user")
	if err := e.photos.PutRetention(e.ctx, owner.ID, 30, now); err != nil {
		t.Fatalf("put retention: %v", err)
	}
	rec := e.r4aPhotoRow(t, owner.ID, 1, e.r4aBlob(t, []byte("wire blob")), now.Add(-31*24*time.Hour))

	cfg := &config.Config{
		Env:             "test",
		HTTPAddr:        "127.0.0.1:0",
		DatabaseURL:     w2cWithSchema(w2cRequireDSN(t), e.schema),
		Timezone:        "Asia/Shanghai",
		HashPepper:      []byte("0123456789abcdef0123456789abcdef"),
		CredentialKey:   []byte("0123456789abcdef0123456789abcdef"),
		PhotoDir:        e.photoDir,
		Workers:         1,
		AvgAttempt:      60 * time.Second,
		EgressRate:      1,
		InsecureCookies: true,
	}
	var logs bytes.Buffer
	stack, err := buildStack(cfg, slog.New(slog.NewTextHandler(&logs, nil)))
	if err != nil {
		t.Fatalf("buildStack: %v", err)
	}
	closed := make(chan struct{})
	deadline := time.Now().Add(2 * time.Second)
	for {
		if e.scalarInt("SELECT count(*) FROM photos WHERE id = $1::uuid", rec.ID) == 0 {
			break
		}
		if time.Now().After(deadline) {
			stack.close()
			t.Fatalf("装配后首轮清理没有执行：RetentionLoop 未接线；logs=%s", logs.String())
		}
		time.Sleep(10 * time.Millisecond)
	}
	go func() { defer close(closed); stack.close() }()
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("关闭路径未在 2s 内返回：清理循环没有停止")
	}
}
