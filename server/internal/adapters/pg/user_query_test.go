package pg

// 本文件是用户列表查询的**无数据库**单测。
//
// # 功能
// 直接调用 userListQuery，逐字断言 SQL 与参数。它**不**连库、**不**建 schema。
//
// # 归属
// pg 适配器的内部单测。e2e（cmd/server/users_list_e2e_test.go）验证行为，
// 本文件验证 e2e 看不见的承重形状：并列时刻的稳定次序、键集条件形状、
// LIKE 元字符转义。这些性质只在 SQL 文本里，换计划或换数据都不改变观察结果。
//
// # 复用
// userListQuery、escapeLikePrefix、encodeCursor。
//
// # 口径
// 断言是逐字包含，不是正则：删除限定名或 tiebreaker 必须让本文件变红。

import (
	"strings"
	"testing"
	"time"
)

// TestUserListQueryOrdersByQualifiedID 钉住 ORDER BY 的两个承重属性：
// 一是 users.id 提供并列 created_at 的稳定次序；
// 二是限定名避免裸 id 被解析成输出列 id::text（那会退化为 Incremental Sort）。
// 一次 Contains 同时守住两者：任何一处被改都会失配。
func TestUserListQueryOrdersByQualifiedID(t *testing.T) {
	sqlText, _, err := userListQuery("", "", "", "", 10)
	if err != nil {
		t.Fatalf("userListQuery: %v", err)
	}
	const want = " ORDER BY created_at DESC, users.id DESC"
	if !strings.Contains(sqlText, want) {
		t.Fatalf("ORDER BY is not %q:\n%s", strings.TrimSpace(want), sqlText)
	}
}

// TestUserListQueryKeysetShape 钉住键集条件的形状。
// 两个占位符必须紧邻，第二个必须带 ::uuid 定型。
func TestUserListQueryKeysetShape(t *testing.T) {
	const id = "0192f000-0000-7000-8000-000000000001"
	at := time.Date(2026, 10, 28, 8, 0, 0, 0, time.UTC)

	sqlText, args, err := userListQuery("", "", "", encodeCursor(at, id), 10)
	if err != nil {
		t.Fatalf("userListQuery with cursor: %v", err)
	}
	const want = " AND (created_at, id) < ($1, $2::uuid)"
	if !strings.Contains(sqlText, want) {
		t.Fatalf("keyset clause is not %q:\n%s", strings.TrimSpace(want), sqlText)
	}
	if len(args) != 3 {
		t.Fatalf("args = %d, want 3 (created_at, id, limit+1)", len(args))
	}
	gotAt, ok := args[0].(time.Time)
	if !ok {
		t.Fatalf("args[0] = %T, want time.Time", args[0])
	}
	if !gotAt.Equal(at) {
		t.Fatalf("args[0] = %s, want %s", gotAt, at)
	}
	if args[1] != id {
		t.Fatalf("args[1] = %v, want %q", args[1], id)
	}
	if !strings.Contains(sqlText, " LIMIT $3") {
		t.Fatalf("LIMIT placeholder is not $3:\n%s", sqlText)
	}
}

// TestUserListQueryEscapesLikeMetacharacters 钉住前缀过滤的转义。
// 邮箱里的 %、_ 与 \ 是普通字符，不得被当成通配或转义符。
// 表驱动覆盖三个元字符与一个无元字符的对照。
func TestUserListQueryEscapesLikeMetacharacters(t *testing.T) {
	cases := []struct {
		name  string
		query string
		want  string
	}{
		{"percent", "a%b", `a\%b`},
		{"underscore", "a_b", `a\_b`},
		{"backslash", `a\b`, `a\\b`},
		{"plain", "ali", "ali"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			sqlText, args, err := userListQuery("", "", tc.query, "", 10)
			if err != nil {
				t.Fatalf("userListQuery(q=%q): %v", tc.query, err)
			}
			if !strings.Contains(sqlText, ` AND email LIKE $1 || '%' ESCAPE '\'`) {
				t.Fatalf("LIKE clause missing or reshaped:\n%s", sqlText)
			}
			if len(args) != 2 {
				t.Fatalf("args = %d, want 2 (prefix, limit+1)", len(args))
			}
			if args[0] != tc.want {
				t.Fatalf("prefix arg = %q, want %q", args[0], tc.want)
			}
		})
	}
}
