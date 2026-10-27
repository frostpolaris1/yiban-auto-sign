package main

// 审计键派生的单元测试。
//
// # 功能
// 钉住 deriveAuditKey 的确定性、与 pepper 的区分、与 CSRF 派生的域分隔。
// 本文件**不**连数据库，**不**启动服务。
//
// # 口径
// 审计键是 HMAC-SHA256(pepper, "yiban.audit.key.v1") 的原始 32 字节。
// 同一 pepper 必须得到同一键：重启后旧链仍可校验。

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

func TestDeriveAuditKey(t *testing.T) {
	pepper := []byte("0123456789abcdef0123456789abcdef")

	first, err := assembly.DeriveAuditKey(pepper)
	if err != nil {
		t.Fatalf("DeriveAuditKey: %v", err)
	}
	second, err := assembly.DeriveAuditKey(pepper)
	if err != nil {
		t.Fatalf("DeriveAuditKey(second): %v", err)
	}
	if !bytes.Equal(first, second) {
		t.Fatal("two derivations of the same pepper differ; old chains would stop verifying")
	}
	if len(first) != sha256.Size {
		t.Fatalf("audit key length = %d, want %d", len(first), sha256.Size)
	}

	want := hmac.New(sha256.New, pepper)
	want.Write([]byte("yiban.audit.key.v1"))
	if !bytes.Equal(first, want.Sum(nil)) {
		t.Fatal("audit key does not match HMAC-SHA256(pepper, \"yiban.audit.key.v1\")")
	}

	if bytes.Equal(first, pepper) {
		t.Fatal("audit key equals the pepper itself")
	}
	// 域分隔：同一输入经 CSRF 派生必须得到不同的值。
	// 两者混用会让一个用途的密钥泄露另一个用途。
	if string(first) == coreauth.CSRFToken(pepper, "yiban.audit.key.v1") {
		t.Fatal("audit key collides with the CSRF derivation")
	}
	if _, err := assembly.DeriveAuditKey(nil); err == nil {
		t.Fatal("DeriveAuditKey accepted an empty pepper")
	}
}
