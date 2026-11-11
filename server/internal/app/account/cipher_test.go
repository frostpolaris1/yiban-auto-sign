package account

// 凭据加解密的固定向量与边界。
//
// 这里的 kid 向量由 Python hmac 独立算出，不是本仓代码自证。
// 本文件**不**连库、**不**测轮换。

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
)

// 零值与半构造的 cipher 必须返回错误而不是 panic：
// 装配缺失要显性失败，不得崩进程。
func TestCipherZeroValueFailsClosed(t *testing.T) {
	var zero Cipher
	if _, err := zero.Seal("s", "a", CredentialPayload{Password: "p"}); err == nil {
		t.Fatal("zero Cipher.Seal returned no error")
	}
	if _, err := zero.Open(coreaccount.Credential{}); err == nil {
		t.Fatal("zero Cipher.Open returned no error")
	}
	if _, err := zero.SealSecret("domain", "plain"); err == nil {
		t.Fatal("zero Cipher.SealSecret returned no error")
	}
	if _, err := zero.OpenSecret("domain", "AAAA"); err == nil {
		t.Fatal("zero Cipher.OpenSecret returned no error")
	}

	var nilCipher *Cipher
	if _, err := nilCipher.Seal("s", "a", CredentialPayload{}); err == nil {
		t.Fatal("nil Cipher.Seal returned no error")
	}
	if _, err := nilCipher.Open(coreaccount.Credential{}); err == nil {
		t.Fatal("nil Cipher.Open returned no error")
	}
}

func TestCipherKIDMatchesFixedVector(t *testing.T) {
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	const want = "82d9e1003806a255"
	if got := cipher.KID(); got != want {
		t.Fatalf("kid = %q, want %q (Python hmac vector)", got, want)
	}
}

func TestCipherRoundTripPinsPayloadKeys(t *testing.T) {
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	const (
		school  = "018f0000-0000-7000-8000-000000000001"
		account = "018f0000-0000-7000-8000-000000000002"
	)
	sealed, err := cipher.Seal(school, account, CredentialPayload{
		DeviceCode: "device",
		Password:   "pass",
		PhoneModel: "Pixel",
	})
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	if sealed.Alg != CredentialAlg || sealed.AADVersion != CredentialAADVersion ||
		sealed.KID != cipher.KID() || len(sealed.Nonce) != 12 || sealed.Version != 1 {
		t.Fatalf("sealed header = %#v", sealed)
	}

	// 用内部 AEAD 复核明文形态：三键齐全且顺序固定。
	plain, err := cipher.aead.Open(nil, sealed.Nonce, sealed.Ciphertext, aad(school, account))
	if err != nil {
		t.Fatalf("open with the documented AAD: %v", err)
	}
	const wantPlain = `{"device_code":"device","password":"pass","phone_model":"Pixel"}`
	if string(plain) != wantPlain {
		t.Fatalf("plaintext = %q, want %q", plain, wantPlain)
	}

	got, err := cipher.Open(sealed)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if got.DeviceCode != "device" || got.Password != "pass" || got.PhoneModel != "Pixel" {
		t.Fatalf("payload = %#v", got)
	}
}

func TestCipherRejectsForeignAADAndUnknownKey(t *testing.T) {
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	sealed, err := cipher.Seal(
		"018f0000-0000-7000-8000-000000000001",
		"018f0000-0000-7000-8000-000000000002",
		CredentialPayload{Password: "pass"})
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}

	// 跨账号搬运：AAD 不匹配，解密失败。
	moved := sealed
	moved.AccountID = "018f0000-0000-7000-8000-000000000003"
	if _, err := cipher.Open(moved); err == nil || !strings.Contains(err.Error(), "decrypted") {
		t.Fatalf("cross-account open = %v, want a decryption failure", err)
	}

	// 未知 kid：拒绝，不返回空负载。
	unknown := sealed
	unknown.KID = "0000000000000000"
	if _, err := cipher.Open(unknown); err == nil {
		t.Fatal("Open accepted an unknown kid")
	}

	// 短钥与长钥都拒绝，不补齐。
	for _, key := range [][]byte{[]byte("short"), make([]byte, 33)} {
		if _, err := NewCipher(key); err == nil {
			t.Fatalf("NewCipher accepted a %d-byte key", len(key))
		}
	}
}

// 同一明文两次加密的 nonce 必须不同。
// nonce 复用会让 AES-GCM 泄漏明文异或值，属于不可接受的回归。
func TestCipherNonceIsFresh(t *testing.T) {
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	payload := CredentialPayload{Password: "pass"}
	first, err := cipher.Seal(
		"018f0000-0000-7000-8000-000000000001",
		"018f0000-0000-7000-8000-000000000002", payload)
	if err != nil {
		t.Fatalf("Seal first: %v", err)
	}
	second, err := cipher.Seal(
		"018f0000-0000-7000-8000-000000000001",
		"018f0000-0000-7000-8000-000000000002", payload)
	if err != nil {
		t.Fatalf("Seal second: %v", err)
	}
	if bytes.Equal(first.Nonce, second.Nonce) {
		t.Fatalf("two seals reused the nonce %x", first.Nonce)
	}
}

// failingReader 模拟系统随机源故障：SealSecret 必须报错而不是写半截密文。
type failingReader struct{}

func (failingReader) Read([]byte) (int, error) { return 0, errors.New("entropy unavailable") }

// 秘密串扩展：同一把钥、固定域标签，base64 往返；跨域与篡改都拒绝。
func TestCipherSecretRoundTrip(t *testing.T) {
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	const plaintext = "smtp-auth-code-9!"
	blob, err := cipher.SealSecret(SecretDomainMailPass, plaintext)
	if err != nil {
		t.Fatalf("SealSecret: %v", err)
	}
	if blob == plaintext || strings.Contains(blob, plaintext) {
		t.Fatalf("blob contains the plaintext: %q", blob)
	}
	got, err := cipher.OpenSecret(SecretDomainMailPass, blob)
	if err != nil {
		t.Fatalf("OpenSecret: %v", err)
	}
	if got != plaintext {
		t.Fatalf("OpenSecret = %q, want %q", got, plaintext)
	}

	// 跨域不可解：同一把钥、不同 AAD 标签。
	if _, err := cipher.OpenSecret("yiban.other.v1", blob); err == nil {
		t.Fatal("OpenSecret accepted a foreign domain")
	}
	// 坏 base64 / 截断 / 篡改都拒绝，不返回空串。
	for _, bad := range []string{"not-base64!!", "AAAA", blob[:len(blob)-4]} {
		if _, err := cipher.OpenSecret(SecretDomainMailPass, bad); err == nil {
			t.Fatalf("OpenSecret accepted malformed blob %q", bad)
		}
	}
	// 空域标签是调用方错误。
	if _, err := cipher.SealSecret("", plaintext); err == nil {
		t.Fatal("SealSecret accepted an empty domain")
	}
}

// shortThenErrorReader 首次短读、随后报错：io.ReadFull 必须失败关闭。
type shortThenErrorReader struct{ calls int }

func (r *shortThenErrorReader) Read(p []byte) (int, error) {
	if r.calls == 0 {
		r.calls++
		return len(p) / 2, nil
	}
	return 0, errors.New("short read")
}

// 随机源故障与零值 cipher 都显性报错：加密失败不得静默落库。
func TestCipherSecretFailsClosed(t *testing.T) {
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	cipher.random = failingReader{}
	if _, err := cipher.SealSecret(SecretDomainMailPass, "x"); err == nil {
		t.Fatal("SealSecret succeeded with a failing random source")
	}
	// 短读不得当完整 nonce：io.ReadFull 报错，SealSecret 失败关闭。
	cipher.random = &shortThenErrorReader{}
	if _, err := cipher.SealSecret(SecretDomainMailPass, "x"); err == nil {
		t.Fatal("SealSecret succeeded with a short random read")
	}
	var zero Cipher
	if _, err := zero.SealSecret(SecretDomainMailPass, "x"); err == nil {
		t.Fatal("zero cipher SealSecret succeeded")
	}
	if _, err := zero.OpenSecret(SecretDomainMailPass, "AAAA"); err == nil {
		t.Fatal("zero cipher OpenSecret succeeded")
	}
}
