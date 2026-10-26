package protocol

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"strings"
	"testing"
)

func TestEncryptPasswordRoundTrip(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	der, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatalf("MarshalPKIXPublicKey: %v", err)
	}
	publicPEM := string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))

	encrypted, err := EncryptPassword("secret", publicPEM)
	if err != nil {
		t.Fatalf("EncryptPassword: %v", err)
	}
	if len(encrypted) != 172 || !strings.HasSuffix(encrypted, "=") {
		t.Fatalf("base64 length = %d, want 172 with one trailing =", len(encrypted))
	}
	ciphertext, err := base64.StdEncoding.DecodeString(encrypted)
	if err != nil {
		t.Fatalf("DecodeString: %v", err)
	}
	if len(ciphertext) != 128 {
		t.Fatalf("ciphertext = %d bytes, want 128", len(ciphertext))
	}
	plain, err := rsa.DecryptPKCS1v15(rand.Reader, key, ciphertext)
	if err != nil {
		t.Fatalf("DecryptPKCS1v15: %v", err)
	}
	if string(plain) != "secret" {
		t.Fatalf("plaintext = %q", plain)
	}
}

func TestEncryptPasswordFixtureKey(t *testing.T) {
	page, err := ParseAuthorizePage(string(readFixture(t, "authorize_page.html")))
	if err != nil {
		t.Fatalf("ParseAuthorizePage: %v", err)
	}
	encrypted, err := EncryptPassword("secret", page.PublicKeyPEM)
	if err != nil {
		t.Fatalf("EncryptPassword: %v", err)
	}
	ciphertext, err := base64.StdEncoding.DecodeString(encrypted)
	if err != nil {
		t.Fatalf("DecodeString: %v", err)
	}
	if len(ciphertext) != 128 {
		t.Fatalf("ciphertext = %d bytes, want 128 for RSA-1024", len(ciphertext))
	}
}

func TestEncryptPasswordPKCS1Key(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	publicPEM := string(pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PUBLIC KEY",
		Bytes: x509.MarshalPKCS1PublicKey(&key.PublicKey),
	}))
	encrypted, err := EncryptPassword("secret", publicPEM)
	if err != nil {
		t.Fatalf("EncryptPassword: %v", err)
	}
	ciphertext, err := base64.StdEncoding.DecodeString(encrypted)
	if err != nil {
		t.Fatalf("DecodeString: %v", err)
	}
	plain, err := rsa.DecryptPKCS1v15(rand.Reader, key, ciphertext)
	if err != nil {
		t.Fatalf("DecryptPKCS1v15: %v", err)
	}
	if string(plain) != "secret" {
		t.Fatalf("plaintext = %q", plain)
	}
}

func TestEncryptPasswordRejectsPrivateKey(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	privatePEM := string(pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(key),
	}))
	if _, err := EncryptPassword("secret", privatePEM); err == nil {
		t.Fatal("a private key must not be accepted")
	}
}

func TestEncryptPasswordRejectsBadKey(t *testing.T) {
	for _, pemText := range []string{"", "not a pem", "-----BEGIN PUBLIC KEY-----\nQQ==\n-----END PUBLIC KEY-----"} {
		if _, err := EncryptPassword("secret", pemText); err == nil {
			t.Fatalf("EncryptPassword accepted %q", pemText)
		}
	}
}
