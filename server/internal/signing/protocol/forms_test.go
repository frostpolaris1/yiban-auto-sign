package protocol

import (
	"net/url"
	"strings"
	"testing"
)

// splitFixturePairs 按 & 与首个 = 拆夹具字段。
// usersure_form.txt 存的是字段原值，不是 urlencode 输出。
func splitFixturePairs(body string) map[string]string {
	out := map[string]string{}
	for _, pair := range strings.Split(body, "&") {
		parts := strings.SplitN(pair, "=", 2)
		if len(parts) == 2 {
			out[parts[0]] = parts[1]
		}
	}
	return out
}

func formKeys(body string) []string {
	var keys []string
	for _, pair := range strings.Split(body, "&") {
		parts := strings.SplitN(pair, "=", 2)
		keys = append(keys, parts[0])
	}
	return keys
}

func TestBuildUsersureFormMatchesFixture(t *testing.T) {
	fixture := strings.TrimRight(string(readFixture(t, "usersure_form.txt")), "\n")
	want := splitFixturePairs(fixture)
	got := BuildUsersureForm(want["oauth_uname"], want["oauth_upwd"],
		want["client_id"], want["redirect_uri"], want["display"], nil)

	values, err := url.ParseQuery(got)
	if err != nil {
		t.Fatalf("ParseQuery: %v", err)
	}
	for key, value := range want {
		if values.Get(key) != value {
			t.Fatalf("%s = %q, want %q", key, values.Get(key), value)
		}
	}
	keys := formKeys(got)
	wantOrder := []string{"oauth_uname", "oauth_upwd", "client_id", "redirect_uri", "display"}
	if len(keys) != len(wantOrder) {
		t.Fatalf("keys = %v", keys)
	}
	for i, key := range wantOrder {
		if keys[i] != key {
			t.Fatalf("keys = %v, want %v", keys, wantOrder)
		}
	}
}

func TestBuildUsersureFormAppendsScope(t *testing.T) {
	scope := "1,2,3,4,"
	got := BuildUsersureForm("1", "2", "3", "4", "html", &scope)
	if !strings.HasSuffix(got, "&scope=1%2C2%2C3%2C4%2C") {
		t.Fatalf("scope was not appended or encoded: %s", got)
	}
}

func TestBuildSignInBodyMatchesFixtures(t *testing.T) {
	in := SignInBody{
		LngLat:   Point{Lng: 0.101837, Lat: 1.003954},
		Address:  "示例省示例市示例区示例大学示例校区-示例苑·公共教学组团",
		OutState: "1",
	}
	got := BuildSignInBody(in)
	wantURL := strings.TrimRight(string(readFixture(t, "sign_in_body_urlencoded.txt")), "\n")
	if got != wantURL {
		t.Fatalf("body mismatch\n got: %s\nwant: %s", got, wantURL)
	}

	values, err := url.ParseQuery(got)
	if err != nil {
		t.Fatalf("ParseQuery: %v", err)
	}
	wantInfo := `{"Reason": "", "AttachmentFileName": "", "LngLat": "0.101837,1.003954", "Address": "示例省示例市示例区示例大学示例校区-示例苑·公共教学组团"}`
	if values.Get("SignInfo") != wantInfo {
		t.Fatalf("SignInfo = %q\nwant %q", values.Get("SignInfo"), wantInfo)
	}
	if values.Get("AttachmentFileName") != "" ||
		values.Get("OutState") != "1" ||
		values.Get("Code") != "" ||
		values.Get("PhoneModel") != "" {
		t.Fatalf("fields = %v", values)
	}
}

func TestBuildSignInBodyDefaultsOutState(t *testing.T) {
	got := BuildSignInBody(SignInBody{LngLat: Point{Lng: 1, Lat: 2}, Address: "x"})
	values, err := url.ParseQuery(got)
	if err != nil {
		t.Fatalf("ParseQuery: %v", err)
	}
	if values.Get("OutState") != "1" {
		t.Fatalf("OutState = %q, want 1", values.Get("OutState"))
	}
}

func TestBuildSignInBodyEscapesJSON(t *testing.T) {
	got := BuildSignInBody(SignInBody{
		LngLat:  Point{Lng: 1, Lat: 2},
		Address: `A"B\C`,
	})
	values, err := url.ParseQuery(got)
	if err != nil {
		t.Fatalf("ParseQuery: %v", err)
	}
	info := values.Get("SignInfo")
	if !strings.Contains(info, `"Address": "A\"B\\C"`) {
		t.Fatalf("SignInfo = %q", info)
	}
}
