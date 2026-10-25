package protocol

import (
	"errors"
	"strings"
	"testing"
)

func TestParseAuthorizePageFixture(t *testing.T) {
	page, err := ParseAuthorizePage(string(readFixture(t, "authorize_page.html")))
	if err != nil {
		t.Fatalf("ParseAuthorizePage: %v", err)
	}
	if page.PageUse != "0000000000000000000000000000000000000000" {
		t.Fatalf("page_use = %q", page.PageUse)
	}
	if !strings.HasPrefix(page.PublicKeyPEM, "-----BEGIN PUBLIC KEY-----\n") ||
		!strings.HasSuffix(page.PublicKeyPEM, "\n-----END PUBLIC KEY-----") {
		t.Fatalf("PEM was not normalized: %q", page.PublicKeyPEM)
	}
	if strings.Contains(page.PublicKeyPEM, "\r") {
		t.Fatal("PEM must not contain CR")
	}
	urlText := string(readFixture(t, "usersure_url.txt"))
	if !strings.Contains(urlText, "ajax_sign="+page.PageUse) {
		t.Fatalf("usersure fixture does not carry the parsed page_use: %s", urlText)
	}
}

func TestParseAuthorizePageRejectsMissingToken(t *testing.T) {
	_, err := ParseAuthorizePage("<html><body>no token</body></html>")
	var parse *ParseError
	if !errors.As(err, &parse) || parse.Field != "page_use" {
		t.Fatalf("error = %v, want ParseError(page_use)", err)
	}
}

func TestParseAuthorizePageRejectsMissingKey(t *testing.T) {
	html := `<script>var page_use = 'abc123';</script>`
	_, err := ParseAuthorizePage(html)
	var parse *ParseError
	if !errors.As(err, &parse) || parse.Field != "public_key" {
		t.Fatalf("error = %v, want ParseError(public_key)", err)
	}
}

// type="test" 是上游的真实形态；定位只能靠 id="key"。
func TestParseAuthorizePageIgnoresType(t *testing.T) {
	html := `<script>var page_use = "abc123";</script>
<input type="test" id="key" value="-----BEGIN PUBLIC KEY-----
QUJD
-----END PUBLIC KEY-----">`
	page, err := ParseAuthorizePage(html)
	if err != nil {
		t.Fatalf("ParseAuthorizePage: %v", err)
	}
	if page.PageUse != "abc123" {
		t.Fatalf("page_use = %q", page.PageUse)
	}
}
