package auth_test

// 口令哈希的单元测试。
//
// # 功能
// 钉住 argon2id 往返、旧 scrypt 兼容与「需要重哈希」信号。
// 本文件**不**测时序，**不**测数据库。
//
// # 口径
// scrypt 向量由 Python hashlib.scrypt 生成，参数与 werkzeug 相同。

import (
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// legacyHash 是 werkzeug 形状的旧哈希。口令是 legacyPassword。
// 向量由 Python hashlib.scrypt 生成，不是本仓代码算出。
const (
	legacyHash     = "scrypt:65536:8:1$testsalt12345678$b0e015afcf201322238638333e5680cb8a0cb7ac1e223bb488289c6a6156e3aa5a128ee8f9ba94ef3813684f5237992c248f9beb23f4fc946bcf25278795abf8"
	legacyPassword = "legacy-pass"
)

// 新哈希必须以 argon2id 与固定参数落盘。
func TestHashPasswordUsesArgon2id(t *testing.T) {
	encoded, err := coreauth.HashPassword("correct-horse-battery")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	if !strings.HasPrefix(encoded, "$argon2id$v=19$m=19456,t=2,p=1$") {
		t.Fatalf("hash = %q, want the frozen argon2id parameter prefix", encoded)
	}
	if strings.Contains(encoded, "correct-horse-battery") {
		t.Fatal("hash contains the plaintext password")
	}

	check, err := coreauth.VerifyPassword("correct-horse-battery", encoded)
	if err != nil {
		t.Fatalf("VerifyPassword: %v", err)
	}
	if !check.Valid {
		t.Fatal("the fresh hash did not verify")
	}
	if check.NeedsRehash {
		t.Fatal("a fresh hash was marked for rehash")
	}
}

func TestVerifyPasswordRejectsWrongPassword(t *testing.T) {
	encoded, err := coreauth.HashPassword("right-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	check, err := coreauth.VerifyPassword("wrong-password", encoded)
	if err != nil {
		t.Fatalf("VerifyPassword: %v", err)
	}
	if check.Valid {
		t.Fatal("a wrong password verified")
	}
}

// 旧 werkzeug scrypt 哈希必须可校验，并要求重哈希。
func TestVerifyLegacyScrypt(t *testing.T) {
	check, err := coreauth.VerifyPassword(legacyPassword, legacyHash)
	if err != nil {
		t.Fatalf("VerifyPassword(scrypt): %v", err)
	}
	if !check.Valid {
		t.Fatal("the legacy scrypt hash did not verify")
	}
	if !check.NeedsRehash {
		t.Fatal("a legacy scrypt hash must be marked for rehash")
	}

	wrong, err := coreauth.VerifyPassword("not-the-password", legacyHash)
	if err != nil {
		t.Fatalf("VerifyPassword(scrypt, wrong): %v", err)
	}
	if wrong.Valid {
		t.Fatal("a wrong password verified against the scrypt hash")
	}
}

// 未知格式是错误，不是「口令错误」。
// 调用方据此区分「旧格式」与「坏行」。
func TestVerifyPasswordRejectsUnknownFormat(t *testing.T) {
	for _, encoded := range []string{
		"",
		"plaintext",
		"argon2id$v=19$m=1,t=1,p=1$not-base64$not-base64",
		"$argon2id$v=19$m=x,t=2,p=1$c2FsdA$aGFzaA",
		"scrypt:notanumber:8:1$salt$00",
		"scrypt:1024:8$salt$00",
	} {
		if _, err := coreauth.VerifyPassword("password", encoded); err == nil {
			t.Fatalf("VerifyPassword(%q) returned no error", encoded)
		}
	}
}

// 校验侧上界只防库内坏行的资源事故。
// 上界取现用参数的 2 倍：超过即拒，坏行不得让单次校验吃掉上 GiB。
func TestVerifyPasswordRejectsParamsAboveBounds(t *testing.T) {
	for _, encoded := range []string{
		"$argon2id$v=19$m=38913,t=2,p=1$c2FsdA$aGFzaA", // 内存超 2×19456
		"$argon2id$v=19$m=19456,t=5,p=1$c2FsdA$aGFzaA", // 迭代超 2×2
		"$argon2id$v=19$m=19456,t=2,p=3$c2FsdA$aGFzaA", // 并行超 2×1
		"scrypt:262144:8:1$testsalt12345678$00",        // N*r = 2097152 > 1048576
	} {
		if _, err := coreauth.VerifyPassword("password", encoded); err == nil {
			t.Fatalf("VerifyPassword(%q) accepted parameters above the bounds", encoded)
		}
	}
}

// 空口令不得悄悄哈希成有效凭据。
func TestHashPasswordRejectsEmpty(t *testing.T) {
	if _, err := coreauth.HashPassword(""); err == nil {
		t.Fatal("HashPassword accepted an empty password")
	}
}

// 同一口令两次哈希必须不同：盐每次重新生成。
func TestHashPasswordUsesFreshSalt(t *testing.T) {
	first, err := coreauth.HashPassword("same-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	second, err := coreauth.HashPassword("same-password")
	if err != nil {
		t.Fatalf("HashPassword: %v", err)
	}
	if first == second {
		t.Fatal("two hashes of the same password are identical")
	}
}
