package legacy

// 旧 owner 解析表的判据：先精确邮箱、再 @ 前缀，歧义不猜。
// 本文件不连库；真库多 owner 导入的端到端断言在 cmd/cli 的 e2e。

import "testing"

func ownerFixtureUsers() []map[string]any {
	return []map[string]any{
		{"id": JSONNumber("1"), "email": "Admin@Example.invalid"},
		{"id": JSONNumber("2"), "email": "user-a@example.invalid"},
		{"id": JSONNumber("3"), "email": "user-a@other.invalid"},
		{"id": JSONNumber("4"), "email": "user-b@example.invalid"},
	}
}

func TestOwnerIndexResolve(t *testing.T) {
	idx := newOwnerIndex(ownerFixtureUsers())
	cases := []struct {
		name      string
		owner     string
		wantID    int64
		wantVia   string
		wantCands int
		wantFound bool
	}{
		{"exact email is case and space insensitive", "  ADMIN@example.INVALID  ", 1, ownerViaExact, 1, true},
		{"unique prefix resolves", "user-b", 4, ownerViaPrefix, 1, true},
		{"prefix is case insensitive", "  USER-B  ", 4, ownerViaPrefix, 1, true},
		{"ambiguous prefix reports candidates", "user-a", 0, "", 2, false},
		{"exact email wins over shared prefix", "user-a@other.invalid", 3, ownerViaExact, 1, true},
		{"email shaped unknown does not fall back to prefix", "user-a@missing.invalid", 0, "", 0, false},
		{"empty owner is unresolved", "   ", 0, "", 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := idx.resolve(tc.owner)
			if got.found != tc.wantFound || got.oldID != tc.wantID || got.via != tc.wantVia || got.candidates != tc.wantCands {
				t.Fatalf("resolve(%q) = %+v, want id=%d via=%q candidates=%d found=%v",
					tc.owner, got, tc.wantID, tc.wantVia, tc.wantCands, tc.wantFound)
			}
		})
	}
}

func TestOwnerIndexSkipsRowsWithoutIDOrEmail(t *testing.T) {
	idx := newOwnerIndex([]map[string]any{
		{"email": "no-id@example.invalid"},
		{"id": JSONNumber("9"), "email": "  "},
	})
	if got := idx.resolve("no-id@example.invalid"); got.found || got.candidates != 0 {
		t.Fatalf("row without id must not resolve: %+v", got)
	}
	if got := idx.resolve(""); got.found || got.candidates != 0 {
		t.Fatalf("blank email must not create a candidate: %+v", got)
	}
}

// TestOwnerIndexExactAmbiguity 钉住 exact 级的歧义分支：
// 两行归一后同邮箱时 byEmail 保留两个候选，resolve 不得先到先得。
func TestOwnerIndexExactAmbiguity(t *testing.T) {
	idx := newOwnerIndex([]map[string]any{
		{"id": JSONNumber("1"), "email": "dup@example.invalid"},
		{"id": JSONNumber("2"), "email": "  DUP@Example.INVALID  "},
	})
	got := idx.resolve(" dup@example.invalid ")
	if got.found || got.oldID != 0 || got.via != "" || got.candidates != 2 {
		t.Fatalf("exact ambiguity must report 2 candidates and no winner: %+v", got)
	}
}

func TestMapLegacyRole(t *testing.T) {
	cases := []struct {
		old  string
		want string
		ok   bool
	}{
		{"admin", "admin", true},
		{"user", "user", true},
		{"master", "", false},
		{"", "", false},
	}
	for _, tc := range cases {
		got, ok := mapLegacyRole(tc.old)
		if got != tc.want || ok != tc.ok {
			t.Fatalf("mapLegacyRole(%q) = (%q, %v), want (%q, %v)", tc.old, got, ok, tc.want, tc.ok)
		}
	}
}
