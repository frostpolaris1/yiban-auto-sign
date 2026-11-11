package audit_test

// 审计键派生的前后对拍：迁移前 assembly.DeriveAuditKey 的实现是
// HMAC-SHA256(pepper, "yiban.audit.key.v1")；迁移后 core/audit.DeriveKey
// 必须逐字节相同，否则重启后旧链无法校验。

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

func TestDeriveKeyMatchesLegacyDerivation(t *testing.T) {
	pepper := []byte("0123456789abcdef0123456789abcdef")

	got, err := audit.DeriveKey(pepper)
	if err != nil {
		t.Fatalf("DeriveKey: %v", err)
	}
	// 迁移前实现（逐字节对拍基准）。
	legacy := hmac.New(sha256.New, pepper)
	legacy.Write([]byte("yiban.audit.key.v1"))
	want := legacy.Sum(nil)
	if !bytes.Equal(got, want) {
		t.Fatal("DeriveKey drifted from the legacy assembly.DeriveAuditKey output")
	}
	// 固定 pepper 的黄金值：改域标签或算法时这里先红。
	const wantHex = "36f54ed4c5c5c61b8635111f0f2b16d6bdb8ad718115e819d5bbd63e0aa6daea"
	if gotHex := hex.EncodeToString(got); gotHex != wantHex {
		t.Fatalf("audit key = %s, want %s", gotHex, wantHex)
	}
	if len(got) != sha256.Size {
		t.Fatalf("audit key length = %d, want %d", len(got), sha256.Size)
	}

	again, err := audit.DeriveKey(pepper)
	if err != nil {
		t.Fatalf("DeriveKey(second): %v", err)
	}
	if !bytes.Equal(got, again) {
		t.Fatal("two derivations of the same pepper differ; old chains would stop verifying")
	}
	if bytes.Equal(got, pepper) {
		t.Fatal("audit key equals the pepper itself")
	}
	// 域分隔：同一输入经 CSRF 派生必须得到不同的值。
	if string(got) == coreauth.CSRFToken(pepper, "yiban.audit.key.v1") {
		t.Fatal("audit key collides with the CSRF derivation")
	}
	if _, err := audit.DeriveKey(nil); err == nil {
		t.Fatal("DeriveKey accepted an empty pepper")
	}
}
