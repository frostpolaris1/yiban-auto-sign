package protocol

import (
	"os"
	"path/filepath"
	"testing"
)

// readFixture 读取 testdata 下的夹具。
func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	return body
}

// envelopeData 解析信封并返回 data 字段。
func envelopeData(t *testing.T, name string) any {
	t.Helper()
	env, err := ParseAPIEnvelope(readFixture(t, name))
	if err != nil {
		t.Fatalf("parse envelope %s: %v", name, err)
	}
	return env.Data
}
