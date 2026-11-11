package main

// OS 文件系统实现的判据：原子写覆盖与 0600/0700 权限。
// app/selfcheck 只保留 FileSystem 端口；实现与它的真实 I/O 判据都在本包。

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/selfcheck"
)

func TestOSFileSystemWriteReplacesExistingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "update-state.json")
	if err := os.WriteFile(path, []byte("old"), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := (osFileSystem{}).WriteFileAtomic(path, []byte("new"), 0o600); err != nil {
		t.Fatalf("WriteFileAtomic: %v", err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(body) != "new" {
		t.Fatalf("body = %q, want new", body)
	}
}

func TestStateFileAndDirectoryPermissions(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SKIP: Windows 不保留 0600/0700 权限位。")
	}
	dir := t.TempDir()
	stateDir := filepath.Join(dir, "nested")
	path := filepath.Join(stateDir, "update-state.json")
	opts := selfcheck.Options{
		CredentialKey: testCLICredentialKey,
		HashPepper:    testCLIPepper,
	}
	now := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	if _, err := selfcheck.RecordKeys(osFileSystem{}, path, opts, now); err != nil {
		t.Fatalf("RecordKeys: %v", err)
	}
	fileInfo, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat state: %v", err)
	}
	if got := fileInfo.Mode().Perm(); got != 0o600 {
		t.Fatalf("state file perm = %04o, want 0600", got)
	}
	dirInfo, err := os.Stat(stateDir)
	if err != nil {
		t.Fatalf("stat dir: %v", err)
	}
	if got := dirInfo.Mode().Perm(); got != 0o700 {
		t.Fatalf("state dir perm = %04o, want 0700", got)
	}
}
