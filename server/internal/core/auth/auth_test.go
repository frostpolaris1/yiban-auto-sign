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

func TestHashValueRejectsEmptyPepper(t *testing.T) {
	if _, err := HashValue(nil, "203.0.113.5"); err == nil {
		t.Fatal("empty pepper must be an error, not a silent default")
	}
}

func TestHashIPRoundTrip(t *testing.T) {
	pepper := []byte("0123456789abcdef0123456789abcdef")
	hash, err := HashIP(pepper, "203.0.113.5")
	if err != nil {
		t.Fatal(err)
	}
	if hash.IsZero() || len(hash.String()) != 64 {
		t.Fatalf("digest = %q", hash.String())
	}
	again, err := HashIP(pepper, "203.0.113.5")
	if err != nil || again != hash {
		t.Fatalf("digest is not deterministic: %v", err)
	}
	other, err := HashIP([]byte("another pepper 0123456789abc"), "203.0.113.5")
	if err != nil || other == hash {
		t.Fatal("a different pepper must change the digest")
	}
	stored, err := IPHashFromStored(hash.String())
	if err != nil || stored != hash {
		t.Fatalf("stored round trip: %v", err)
	}
	if _, err := IPHashFromStored("203.0.113.5"); err == nil {
		t.Fatal("a raw address must not wrap into IPHash")
	}
	if empty, err := IPHashFromStored(""); err != nil || !empty.IsZero() {
		t.Fatalf("an empty stored value is the NULL form: %v %v", empty, err)
	}
}
