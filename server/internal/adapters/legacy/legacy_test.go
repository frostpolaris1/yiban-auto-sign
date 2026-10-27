package legacy

// 旧信封解密与凭据重封装的判据（P8-B 派单 2026-10-27）。
//
// 旧信封形状：v/kid/nonce/ct/tag 全十六进制，AES-256-GCM，
// AAD＝手机号（UTF-8），ct 与 tag 分离（yiban/adapters/config/account_crypto.py）。
// kid = HMAC-SHA256(key, "yiban-accounts-kid-v1") 前 8 字节 hex。
//
// 黄金 kid 由 Python（hashlib/hmac 标准库）对测试钥现算：
// 钥 hex 00112233...eeff（32 字节）→ kid ef9fdd34bd7be741。

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

// testLegacyKeyHex 是测试用旧 KEK（32 字节，合成值）。
const testLegacyKeyHex = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

// goldenKid 是 Python 侧对该钥算出的黄金指纹。
const goldenKid = "ef9fdd34bd7be741"

// sealOldEnvelope 用旧口径加密：AAD=phone，ct 与 tag 分离放 hex 字段。
// 这是测试侧夹具构造器，独立于被测的 LegacyOpenEnvelope。
func sealOldEnvelope(t *testing.T, key []byte, phone, plaintext string, withKid bool) map[string]any {
	t.Helper()
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatalf("aes: %v", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatalf("gcm: %v", err)
	}
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		t.Fatalf("nonce: %v", err)
	}
	out := gcm.Seal(nil, nonce, []byte(plaintext), []byte(phone))
	version := "2"
	env := map[string]any{
		"v":     JSONNumber(version),
		"nonce": hex.EncodeToString(nonce),
		"ct":    hex.EncodeToString(out[:len(out)-16]),
		"tag":   hex.EncodeToString(out[len(out)-16:]),
	}
	if withKid {
		mac := hmac.New(sha256.New, key)
		mac.Write([]byte("yiban-accounts-kid-v1"))
		env["kid"] = hex.EncodeToString(mac.Sum(nil)[:8])
	} else {
		// v1 形状：无 kid，版本号也是 1。
		env["v"] = JSONNumber("1")
	}
	return env
}

func TestLegacyKidGolden(t *testing.T) {
	key, err := hex.DecodeString(testLegacyKeyHex)
	if err != nil {
		t.Fatalf("key: %v", err)
	}
	if got := LegacyKID(key); got != goldenKid {
		t.Fatalf("kid mismatch: got %s want %s", got, goldenKid)
	}
}

func TestLegacyOpenEnvelopeRoundtrip(t *testing.T) {
	key, _ := hex.DecodeString(testLegacyKeyHex)
	for _, withKid := range []bool{true, false} { // v2 带 kid、v1 无 kid 都可读
		env := sealOldEnvelope(t, key, "13800000001", "pw-fixture-0001", withKid)
		got, err := LegacyOpenEnvelope(env, "13800000001", key)
		if err != nil {
			t.Fatalf("withKid=%v: %v", withKid, err)
		}
		if got != "pw-fixture-0001" {
			t.Fatalf("withKid=%v: plaintext %q", withKid, got)
		}
	}
}

func TestLegacyOpenEnvelopeWrongKeyFailsLoud(t *testing.T) {
	key, _ := hex.DecodeString(testLegacyKeyHex)
	wrong, _ := hex.DecodeString(strings.Repeat("ff", 32))
	env := sealOldEnvelope(t, key, "13800000001", "pw-fixture-0001", true)
	if _, err := LegacyOpenEnvelope(env, "13800000001", wrong); err == nil {
		t.Fatal("wrong key must fail loudly, got nil error")
	}
}

func TestLegacyOpenEnvelopeWrongPhoneFails(t *testing.T) {
	key, _ := hex.DecodeString(testLegacyKeyHex)
	env := sealOldEnvelope(t, key, "13800000001", "pw-fixture-0001", true)
	// AAD＝手机号：换手机号解密必须失败（旧口径的跨账号互换防护）。
	if _, err := LegacyOpenEnvelope(env, "13800000002", key); err == nil {
		t.Fatal("wrong phone (AAD) must fail, got nil error")
	}
}

func TestLegacyOpenEnvelopeBadShapeFails(t *testing.T) {
	key, _ := hex.DecodeString(testLegacyKeyHex)
	if _, err := LegacyOpenEnvelope(map[string]any{}, "13800000001", key); err == nil {
		t.Fatal("empty envelope must fail")
	}
	bad := map[string]any{"v": JSONNumber("2"), "nonce": "zz", "ct": "ab", "tag": "cd"}
	if _, err := LegacyOpenEnvelope(bad, "13800000001", key); err == nil {
		t.Fatal("non-hex nonce must fail")
	}
}
