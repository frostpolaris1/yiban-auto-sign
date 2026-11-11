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

// TestDeriveKeyMatchesLegacyDerivation 对多组 pepper 逐项复刻旧算法对拍。
// nil 与空串是同一错误边界；32B 组额外钉住迁移前实测的黄金 hex。
func TestDeriveKeyMatchesLegacyDerivation(t *testing.T) {
	const goldenPepper = "0123456789abcdef0123456789abcdef"
	const goldenHex = "36f54ed4c5c5c61b8635111f0f2b16d6bdb8ad718115e819d5bbd63e0aa6daea"

	cases := []struct {
		name    string
		pepper  []byte
		wantErr bool
		golden  string // 非空时额外钉住十六进制黄金值
	}{
		{"nil pepper", nil, true, ""},
		{"空串", []byte{}, true, ""},
		{"16B", []byte("0123456789abcdef"), false, ""},
		{"32B", []byte(goldenPepper), false, goldenHex},
		{"64B", []byte("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"), false, ""},
		{"1KB", bytes.Repeat([]byte("k"), 1024), false, ""},
		{"含中文 UTF-8", []byte("易班审计密钥-中文-pepper"), false, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := audit.DeriveKey(c.pepper)
			if c.wantErr {
				if err == nil {
					t.Fatal("DeriveKey accepted an empty pepper")
				}
				if got != nil {
					t.Fatalf("DeriveKey returned %d bytes alongside an error", len(got))
				}
				return
			}
			if err != nil {
				t.Fatalf("DeriveKey: %v", err)
			}
			// 旧算法复刻：逐字节对拍。
			legacy := hmac.New(sha256.New, c.pepper)
			legacy.Write([]byte("yiban.audit.key.v1"))
			want := legacy.Sum(nil)
			if !bytes.Equal(got, want) {
				t.Fatal("DeriveKey drifted from the legacy assembly.DeriveAuditKey output")
			}
			if len(got) != sha256.Size {
				t.Fatalf("audit key length = %d, want %d", len(got), sha256.Size)
			}
			if c.golden == "" {
				return
			}
			if gotHex := hex.EncodeToString(got); gotHex != c.golden {
				t.Fatalf("audit key = %s, want %s", gotHex, c.golden)
			}
			// 黄金 pepper 的附加性质：确定性、不等于 pepper、与 CSRF 域分隔。
			again, err := audit.DeriveKey(c.pepper)
			if err != nil {
				t.Fatalf("DeriveKey(second): %v", err)
			}
			if !bytes.Equal(got, again) {
				t.Fatal("two derivations of the same pepper differ; old chains would stop verifying")
			}
			if bytes.Equal(got, c.pepper) {
				t.Fatal("audit key equals the pepper itself")
			}
			if string(got) == coreauth.CSRFToken(c.pepper, "yiban.audit.key.v1") {
				t.Fatal("audit key collides with the CSRF derivation")
			}
		})
	}
}
