package auth

import "testing"

func TestCSRFTokenIsDerived(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	a := CSRFToken(secret, "token-a")
	if a == "" {
		t.Fatal("empty csrf token")
	}
	if a == "token-a" {
		t.Fatal("csrf token equals the session token")
	}
	if a != CSRFToken(secret, "token-a") {
		t.Fatal("csrf token is not deterministic")
	}
	if a == CSRFToken(secret, "token-b") {
		t.Fatal("different session tokens share a csrf token")
	}
	if a == CSRFToken([]byte("another-secret-0123456789abcdef"), "token-a") {
		t.Fatal("different secrets share a csrf token")
	}
}
