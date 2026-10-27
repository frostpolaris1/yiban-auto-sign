package storage_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/storage"
)

// 写入后可读回同一字节；重复写入幂等。
func TestFSPutOpenRoundTrip(t *testing.T) {
	dir := t.TempDir()
	fs, err := storage.NewFS(dir)
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	data := []byte("image bytes")
	sha := storage.Digest(data)
	if err := fs.Put(context.Background(), sha, data); err != nil {
		t.Fatalf("put: %v", err)
	}
	if err := fs.Put(context.Background(), sha, data); err != nil {
		t.Fatalf("put again: %v", err)
	}
	body, err := fs.Open(context.Background(), sha)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer body.Close()
	got, err := io.ReadAll(body)
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("read = %q, %v", got, err)
	}
	// 布局：<root>/<前两位>/<hex>
	if _, err := os.Stat(filepath.Join(dir, hexOf(sha)[:2], hexOf(sha))); err != nil {
		t.Fatalf("blob 路径不符: %v", err)
	}
}

// 缺失摘要返回错误，不返回空流。
func TestFSOpenMissing(t *testing.T) {
	fs, err := storage.NewFS(t.TempDir())
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	if _, err := fs.Open(context.Background(), storage.Digest([]byte("absent"))); err == nil {
		t.Fatal("缺失 blob 应报错")
	}
}

// 空字节与空根被拒绝。
func TestFSRejectsInvalid(t *testing.T) {
	if _, err := storage.NewFS(""); err == nil {
		t.Fatal("空根应被拒绝")
	}
	fs, err := storage.NewFS(t.TempDir())
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	if err := fs.Put(context.Background(), storage.Digest(nil), nil); err == nil {
		t.Fatal("空字节应被拒绝")
	}
}

// 并发写同一摘要不报错，且不留下临时文件。
func TestFSConcurrentPutSameDigest(t *testing.T) {
	dir := t.TempDir()
	fs, err := storage.NewFS(dir)
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	data := []byte("concurrent image bytes")
	sha := storage.Digest(data)
	var wg sync.WaitGroup
	errs := make(chan error, 16)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs <- fs.Put(context.Background(), sha, data)
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent put: %v", err)
		}
	}
	left, err := filepath.Glob(filepath.Join(dir, "put-*"))
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	if len(left) != 0 {
		t.Fatalf("残留临时文件: %v", left)
	}
	body, err := fs.Open(context.Background(), sha)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer body.Close()
	got, err := io.ReadAll(body)
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("read = %q, %v", got, err)
	}
}

// 取消的上下文不落盘。
func TestFSCanceledContext(t *testing.T) {
	fs, err := storage.NewFS(t.TempDir())
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := fs.Put(ctx, storage.Digest([]byte("x")), []byte("x")); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
}

func hexOf(sha [32]byte) string {
	const hexdigits = "0123456789abcdef"
	out := make([]byte, 64)
	for i, b := range sha {
		out[i*2] = hexdigits[b>>4]
		out[i*2+1] = hexdigits[b&0x0f]
	}
	return string(out)
}

// 键必须等于字节摘要：内容寻址的根不变量。
func TestFSPutRejectsMismatchedDigest(t *testing.T) {
	fs, err := storage.NewFS(t.TempDir())
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	data := []byte("real bytes")
	wrong := storage.Digest([]byte("other bytes"))
	if err := fs.Put(context.Background(), wrong, data); err == nil {
		t.Fatal("摘要不匹配应被拒绝")
	}
}

// List 返回已落盘 blob 的摘要与修改时间；回收判据需要修改时间。
func TestFSListReturnsStoredBlobs(t *testing.T) {
	dir := t.TempDir()
	fs, err := storage.NewFS(dir)
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	want := map[[32]byte]bool{}
	for _, data := range [][]byte{[]byte("first blob"), []byte("second blob")} {
		sha := storage.Digest(data)
		if err := fs.Put(context.Background(), sha, data); err != nil {
			t.Fatalf("put: %v", err)
		}
		want[sha] = true
	}
	infos, err := fs.List(context.Background())
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(infos) != len(want) {
		t.Fatalf("摘要数 = %d, want %d", len(infos), len(want))
	}
	for _, info := range infos {
		if !want[info.Digest] {
			t.Fatalf("未知摘要 %x", info.Digest)
		}
		if info.ModTime.IsZero() {
			t.Fatal("修改时间为零值")
		}
		if age := time.Since(info.ModTime); age < 0 || age > time.Minute {
			t.Fatalf("修改时间不合理: %v (age %v)", info.ModTime, age)
		}
	}
}

// List 跳过临时件与非法文件名：回收不得把它们当 blob。
func TestFSListSkipsTemporaryAndInvalidNames(t *testing.T) {
	dir := t.TempDir()
	fs, err := storage.NewFS(dir)
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	data := []byte("kept blob")
	sha := storage.Digest(data)
	if err := fs.Put(context.Background(), sha, data); err != nil {
		t.Fatalf("put: %v", err)
	}
	shard := filepath.Join(dir, hexOf(sha)[:2])
	for _, name := range []string{"put-123456", "not-hex", hexOf(sha) + "0"} {
		if err := os.WriteFile(filepath.Join(shard, name), []byte("x"), 0o600); err != nil {
			t.Fatalf("seed %s: %v", name, err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "put-root-temp"), []byte("x"), 0o600); err != nil {
		t.Fatalf("seed root temp: %v", err)
	}
	infos, err := fs.List(context.Background())
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(infos) != 1 || infos[0].Digest != sha {
		t.Fatalf("listed = %+v, want only %x", infos, sha)
	}
}

// Remove 删除已落盘 blob；重复删除幂等。
func TestFSRemove(t *testing.T) {
	dir := t.TempDir()
	fs, err := storage.NewFS(dir)
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	data := []byte("removable blob")
	sha := storage.Digest(data)
	if err := fs.Put(context.Background(), sha, data); err != nil {
		t.Fatalf("put: %v", err)
	}
	if err := fs.Remove(context.Background(), sha); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if _, err := fs.Open(context.Background(), sha); err == nil {
		t.Fatal("删除后仍可打开")
	}
	if err := fs.Remove(context.Background(), sha); err != nil {
		t.Fatalf("重复删除 = %v, want nil", err)
	}
}

// 取消的上下文不删除文件。
func TestFSRemoveCanceledContext(t *testing.T) {
	fs, err := storage.NewFS(t.TempDir())
	if err != nil {
		t.Fatalf("NewFS: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := fs.Remove(ctx, storage.Digest([]byte("x"))); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
}
