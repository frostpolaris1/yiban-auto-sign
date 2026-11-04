package updatecheck

// 状态文件与密钥指纹的判据。
//
// # 口径
// 未知字段保留、未来 schema 拒绝、文件 0600、目录 0700；
// 指纹只出现前缀，原文不落盘。

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// testOptions 返回一组合法的自检输入。
func testOptions() Options {
	return Options{
		DSN:           testDSN,
		HTTPAddr:      "127.0.0.1:47183",
		UIDir:         "/srv/ui",
		PhotoDir:      "/var/lib/yiban/photos",
		CredentialKey: testCredentialKey,
		HashPepper:    testPepper,
		BackupDir:     "/var/lib/yiban/pre-migrate",
		StatePath:     "/var/lib/yiban/update-state.json",
		Unit:          "yiban",
	}
}

func TestLoadStateMissingIsNotAnError(t *testing.T) {
	_, exists, err := LoadState(newFakeFS(), "/var/lib/yiban/update-state.json")
	if err != nil {
		t.Fatalf("LoadState: %v", err)
	}
	if exists {
		t.Fatal("exists = true for a missing file")
	}
}

func TestRecordKeysRoundTripPreservesUnknownFields(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "update-state.json")
	raw := `{
  "schema_version": 1,
  "key_fingerprints": {
    "credential_key": "sha256:old",
    "hash_pepper": "sha256:old",
    "future_key": "sha256:keep"
  },
  "recorded_at": "2026-01-01T00:00:00Z",
  "last_update": {"version": "0.6.4"},
  "note": "keep me"
}
`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatalf("seed state: %v", err)
	}
	st, err := RecordKeys(OSFileSystem{}, path, testOptions(), fixedNow)
	if err != nil {
		t.Fatalf("RecordKeys: %v", err)
	}
	if st.KeyFingerprints["future_key"] != "sha256:keep" {
		t.Fatalf("unknown fingerprint key not preserved: %+v", st.KeyFingerprints)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read state: %v", err)
	}
	for _, want := range []string{`"last_update"`, `"note"`, `"future_key"`, `"recorded_at": "2026-11-04T12:00:00Z"`} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("state lost %s:\n%s", want, body)
		}
	}
	for _, secret := range []string{testCredentialKey, testPepper} {
		if strings.Contains(string(body), secret) {
			t.Fatalf("state file stores secret %q", secret)
		}
	}
	loaded, exists, err := LoadState(OSFileSystem{}, path)
	if err != nil || !exists {
		t.Fatalf("LoadState: %v exists=%v", err, exists)
	}
	if loaded.KeyFingerprints[FingerprintCredentialKey] != st.KeyFingerprints[FingerprintCredentialKey] {
		t.Fatalf("credential fingerprint not persisted")
	}
}

func TestStateFileAndDirectoryPermissions(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SKIP: Windows 不保留 0600/0700 权限位。")
	}
	dir := t.TempDir()
	stateDir := filepath.Join(dir, "nested")
	path := filepath.Join(stateDir, "update-state.json")
	if _, err := RecordKeys(OSFileSystem{}, path, testOptions(), fixedNow); err != nil {
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

func TestRecordKeysRefusesFutureSchema(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "update-state.json")
	raw := `{"schema_version":2,"key_fingerprints":{},"recorded_at":""}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if _, err := RecordKeys(OSFileSystem{}, path, testOptions(), fixedNow); err == nil {
		t.Fatal("RecordKeys accepted schema_version=2")
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(body) != raw {
		t.Fatalf("future state file was modified:\n%s", body)
	}
}

func TestRecordKeysRequiresValidKeys(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "update-state.json")
	opts := testOptions()
	opts.CredentialKey = ""
	if _, err := RecordKeys(OSFileSystem{}, path, opts, fixedNow); err == nil {
		t.Fatal("RecordKeys accepted a missing credential key")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("state file written despite bad key: %v", err)
	}
}

func TestRecordKeysWriteError(t *testing.T) {
	f := newFakeFS()
	f.writeErr = os.ErrPermission
	if _, err := RecordKeys(f, "/var/lib/yiban/update-state.json", testOptions(), fixedNow); err == nil {
		t.Fatal("RecordKeys ignored a write error")
	}
}

func TestSaveStateRejectsVersionMismatch(t *testing.T) {
	if err := SaveState(newFakeFS(), "/state.json", State{SchemaVersion: 2}); err == nil {
		t.Fatal("SaveState accepted schema_version=2")
	}
}

func TestLoadStateRejectsBadShapes(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"empty", ""},
		{"not-json", "{"},
		{"no-version", `{"key_fingerprints":{}}`},
		{"bad-version", `{"schema_version":"one"}`},
		{"bad-fingerprints", `{"schema_version":1,"key_fingerprints":"nope"}`},
		{"bad-recorded-at", `{"schema_version":1,"recorded_at":42}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFakeFS()
			f.files["/state.json"] = []byte(tc.body)
			if _, _, err := LoadState(f, "/state.json"); err == nil {
				t.Fatal("LoadState accepted a bad state file")
			}
		})
	}
}

func TestOSFileSystemWriteReplacesExistingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "update-state.json")
	if err := os.WriteFile(path, []byte("old"), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := (OSFileSystem{}).WriteFileAtomic(path, []byte("new"), 0o600); err != nil {
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

func TestProbeWritableRequiresExistingDirectory(t *testing.T) {
	dir := t.TempDir()
	if err := (OSFileSystem{}).ProbeWritable(dir); err != nil {
		t.Fatalf("ProbeWritable(existing): %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("probe left files behind: %v", entries)
	}
	missing := filepath.Join(dir, "missing")
	if err := (OSFileSystem{}).ProbeWritable(missing); err == nil {
		t.Fatal("ProbeWritable(missing) = nil, want error")
	}
}

func TestProbeReadable(t *testing.T) {
	dir := t.TempDir()
	// 空目录可读（Readdirnames 返回 io.EOF）。
	if err := (OSFileSystem{}).ProbeReadable(dir); err != nil {
		t.Fatalf("ProbeReadable(empty): %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "one"), []byte("x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := (OSFileSystem{}).ProbeReadable(dir); err != nil {
		t.Fatalf("ProbeReadable(non-empty): %v", err)
	}
	if err := (OSFileSystem{}).ProbeReadable(filepath.Join(dir, "missing")); err == nil {
		t.Fatal("ProbeReadable(missing) = nil, want error")
	}
}
