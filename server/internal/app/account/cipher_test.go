package account

// 凭据加解密的固定向量与边界。
//
// 这里的 kid 向量由 Python hmac 独立算出，不是本仓代码自证。
// 本文件**不**连库、**不**测轮换。

import (
	"bytes"
	"strings"
	"testing"
)

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
