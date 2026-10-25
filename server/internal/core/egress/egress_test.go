package egress

import "testing"

func TestIdentity(t *testing.T) {
	cases := map[string]string{
		"":                         Direct,
		"  ":                       Direct,
		"http://user:pw@host:3128": "http://host:3128",
		"http://[::1]:3128":        "http://[::1]:3128",
		"host:3128":                Unparseable,
		"://bad":                   Unparseable,
	}
	for in, want := range cases {
		if got := Identity(in); got != want {
			t.Fatalf("Identity(%q) = %q, want %q", in, got, want)
		}
	}
}
