package selfcheck

// 状态文件与密钥指纹的判据。
//
// # 口径
// 未知字段保留、未来 schema 拒绝、文件 0600、目录 0700；
// 指纹只出现前缀，原文不落盘。

import (
	"os"
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
	const path = "/var/lib/yiban/update-state.json"
	oldFP := "sha256:" + strings.Repeat("a", 64)
	oldPepperFP := "sha256:" + strings.Repeat("b", 64)
	futureFP := "sha256:" + strings.Repeat("c", 64)
	raw := `{
  "schema_version": 1,
  "key_fingerprints": {
    "credential_key": "` + oldFP + `",
    "hash_pepper": "` + oldPepperFP + `",
    "future_key": "` + futureFP + `"
  },
  "recorded_at": "2026-01-01T00:00:00Z",
  "last_update": {"version": "0.6.4"},
  "note": "keep me"
}
`
	f := newFakeFS()
	f.files[path] = []byte(raw)
	st, err := RecordKeys(f, path, testOptions(), fixedNow)
	if err != nil {
		t.Fatalf("RecordKeys: %v", err)
	}
	if st.KeyFingerprints["future_key"] != futureFP {
		t.Fatalf("unknown fingerprint key not preserved: %+v", st.KeyFingerprints)
	}
	body := string(f.writes[path])
	for _, want := range []string{`"last_update"`, `"note"`, `"future_key"`, `"recorded_at": "2026-11-04T12:00:00Z"`} {
		if !strings.Contains(body, want) {
			t.Fatalf("state lost %s:\n%s", want, body)
		}
	}
	for _, secret := range []string{testCredentialKey, testPepper} {
		if strings.Contains(body, secret) {
			t.Fatalf("state file stores secret %q", secret)
		}
	}
	loaded, exists, err := LoadState(f, path)
	if err != nil || !exists {
		t.Fatalf("LoadState: %v exists=%v", err, exists)
	}
	if loaded.KeyFingerprints[FingerprintCredentialKey] != st.KeyFingerprints[FingerprintCredentialKey] {
		t.Fatalf("credential fingerprint not persisted")
	}
}

func TestRecordKeysRefusesFutureSchema(t *testing.T) {
	const path = "/var/lib/yiban/update-state.json"
	raw := `{"schema_version":2,"key_fingerprints":{},"recorded_at":""}`
	f := newFakeFS()
	f.files[path] = []byte(raw)
	if _, err := RecordKeys(f, path, testOptions(), fixedNow); err == nil {
		t.Fatal("RecordKeys accepted schema_version=2")
	}
	if _, written := f.writes[path]; written {
		t.Fatalf("future state file was modified:\n%s", f.writes[path])
	}
}

func TestRecordKeysRequiresValidKeys(t *testing.T) {
	const path = "/var/lib/yiban/update-state.json"
	opts := testOptions()
	opts.CredentialKey = ""
	f := newFakeFS()
	if _, err := RecordKeys(f, path, opts, fixedNow); err == nil {
		t.Fatal("RecordKeys accepted a missing credential key")
	}
	if _, written := f.writes[path]; written {
		t.Fatalf("state file written despite bad key: %s", f.writes[path])
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
		{"bad-fingerprint-value", `{"schema_version":1,"key_fingerprints":{"credential_key":"leak-me-not"}}`},
		{"bad-recorded-at", `{"schema_version":1,"recorded_at":42}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFakeFS()
			f.files["/state.json"] = []byte(tc.body)
			_, _, err := LoadState(f, "/state.json")
			if err == nil {
				t.Fatal("LoadState accepted a bad state file")
			}
			if strings.Contains(err.Error(), "leak-me-not") {
				t.Fatalf("error echoes the invalid fingerprint: %v", err)
			}
		})
	}
}

func TestSaveStateRejectsInvalidFingerprint(t *testing.T) {
	err := SaveState(newFakeFS(), "/state.json", State{
		SchemaVersion:   StateSchemaVersion,
		KeyFingerprints: map[string]string{"credential_key": "leak-me-not"},
		RecordedAt:      "2026-11-04T12:00:00Z",
	})
	if err == nil {
		t.Fatal("SaveState accepted an invalid fingerprint")
	}
	if strings.Contains(err.Error(), "leak-me-not") {
		t.Fatalf("error echoes the invalid fingerprint: %v", err)
	}
}
