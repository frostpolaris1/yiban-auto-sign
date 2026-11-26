package pg

// 本文件是用户列表查询的**无数据库**单测。
//
// # 功能
// 直接调用 userListQuery，逐字断言 SQL 与参数。它**不**连库、**不**建 schema。
//
// # 归属
// pg 适配器的内部单测。e2e（cmd/server/users_list_e2e_test.go）验证行为，
// 本文件验证 e2e 看不见的承重形状：并列时刻的稳定次序、键集条件形状、
// LIKE 元字符转义、先裁页再聚合账号、分组只依赖存在性谓词。
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

	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// TestUserListQueryOrdersByQualifiedID 钉住 ORDER BY 的两个承重属性：
// 一是 users.id 提供并列 created_at 的稳定次序；
// 二是限定名避免裸 id 被解析成输出列 id::text（那会退化为 Incremental Sort）。
// 一次 Contains 同时守住两者：任何一处被改都会失配。
func TestUserListQueryOrdersByQualifiedID(t *testing.T) {
	sqlText, _, err := userListQuery("", "", "", "", "", 10, false)
	if err != nil {
		t.Fatalf("userListQuery: %v", err)
	}
	const want = " ORDER BY u.created_at DESC, u.id DESC"
	if !strings.Contains(sqlText, want) {
		t.Fatalf("ORDER BY is not %q:\n%s", strings.TrimSpace(want), sqlText)
	}
}

// TestUserListQueryKeysetShape 钉住键集条件的形状。
// 两个占位符必须紧邻，第二个必须带 ::uuid 定型。
func TestUserListQueryKeysetShape(t *testing.T) {
	const id = "0192f000-0000-7000-8000-000000000001"
	at := time.Date(2026, 10, 28, 8, 0, 0, 0, time.UTC)

	sqlText, args, err := userListQuery("", "", "", "", encodeCursor(at, id), 10, false)
	if err != nil {
		t.Fatalf("userListQuery with cursor: %v", err)
	}
	const want = " AND (u.created_at, u.id) < ($1, $2::uuid)"
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
			sqlText, args, err := userListQuery("", "", tc.query, "", "", 10, false)
			if err != nil {
				t.Fatalf("userListQuery(q=%q): %v", tc.query, err)
			}
			if !strings.Contains(sqlText, ` AND u.email LIKE $1 || '%' ESCAPE '\'`) {
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

// TestUserListQueryGroupClauses 钉住三个分组的相关存在性谓词。
//
// 分组在裁页之前求值，只能引用 users 与相关 EXISTS：聚合列尚未产生，
// 任何 coalesce(c.*) 形状都会把聚合拉回全表，故逐字禁止。
func TestUserListQueryGroupClauses(t *testing.T) {
	const (
		reviewExists = "EXISTS (SELECT 1 FROM accounts a WHERE a.owner_user_id = u.id AND a.deleted_at IS NULL AND a.status IN ('pending', 'rejected'))"
		liveExists   = "EXISTS (SELECT 1 FROM accounts a WHERE a.owner_user_id = u.id AND a.deleted_at IS NULL)"
	)
	cases := []struct {
		group coreuser.Group
		want  string
	}{
		{coreuser.GroupPending, " AND " + reviewExists},
		{coreuser.GroupNormal, " AND NOT " + reviewExists + " AND " + liveExists},
		{coreuser.GroupVacant, " AND NOT " + liveExists},
	}
	for _, tc := range cases {
		sqlText, _, err := userListQuery("", "", "", tc.group, "", 10, false)
		if err != nil {
			t.Fatalf("userListQuery(group=%s): %v", tc.group, err)
		}
		if !strings.Contains(sqlText, tc.want) {
			t.Fatalf("group %s clause is not %q:\n%s", tc.group, tc.want, sqlText)
		}
		// 分组谓词只能出现在 page CTE 的 WHERE 里；该区间止于 CTE 的 ORDER BY。
		whereStart := strings.Index(sqlText, "WHERE")
		whereEnd := strings.Index(sqlText, " ORDER BY u.created_at DESC, u.id DESC")
		if whereStart < 0 || whereEnd <= whereStart {
			t.Fatalf("group %s list query has no page WHERE region:\n%s", tc.group, sqlText)
		}
		if strings.Contains(sqlText[whereStart:whereEnd], "coalesce(c.") {
			t.Fatalf("group %s still filters on aggregate columns:\n%s", tc.group, sqlText)
		}
	}
}

// TestUserListQueryPagesBeforeAccountAggregation 钉住 P2-8 的查询形状：
// 先用只含 users 的过滤/键集/排序/LIMIT 裁出本页（page CTE），
// 再对页内 owner 做 LEFT JOIN LATERAL 聚合账号。
//
// 两条承重性质：
//  1. LIMIT 出现在账号聚合之前：空页不会触达 accounts；
//  2. 全表 GROUP BY owner_user_id 的 account_counts CTE 已消失。
func TestUserListQueryPagesBeforeAccountAggregation(t *testing.T) {
	sqlText, _, err := userListQuery("", "", "", coreuser.GroupPending, "", 10, false)
	if err != nil {
		t.Fatalf("userListQuery: %v", err)
	}
	if !strings.Contains(sqlText, "WITH page AS") {
		t.Fatalf("list query has no page CTE:\n%s", sqlText)
	}
	limit := strings.Index(sqlText, " LIMIT $")
	if limit < 0 {
		t.Fatalf("list query has no LIMIT:\n%s", sqlText)
	}
	join := strings.Index(sqlText, "LEFT JOIN LATERAL")
	if join < 0 {
		t.Fatalf("list query has no LEFT JOIN LATERAL account aggregate:\n%s", sqlText)
	}
	if limit > join {
		t.Fatalf("LIMIT is applied after account aggregation; the page must be bounded first:\n%s", sqlText)
	}
	for _, banned := range []string{"GROUP BY owner_user_id", "account_counts"} {
		if strings.Contains(sqlText, banned) {
			t.Fatalf("list query still contains %q:\n%s", banned, sqlText)
		}
	}
	if !strings.Contains(sqlText, "WHERE a.owner_user_id = u.id AND a.deleted_at IS NULL") {
		t.Fatalf("lateral aggregate is not scoped to the row owner:\n%s", sqlText)
	}
}

// TestUserListQueryRejectsGroupWithDeleted 钉住互斥组合在 SQL 层也 fail closed。
func TestUserListQueryRejectsGroupWithDeleted(t *testing.T) {
	if _, _, err := userListQuery("", "", "", coreuser.GroupPending, "", 10, true); err == nil {
		t.Fatal("group + include_deleted was accepted")
	}
}

// TestUserCountsQueryScansUsersOnce 钉住 Counts 的查询形状：
// 单次扫描 users、LEFT JOIN LATERAL 按行聚合，绝不对 accounts 全表 GROUP BY。
//
// 直接断言包内常量：与 List 共用 userAccountAggregate，口径只此一处。
func TestUserCountsQueryScansUsersOnce(t *testing.T) {
	query := userCountsQuery
	for _, banned := range []string{"GROUP BY", "account_counts"} {
		if strings.Contains(query, banned) {
			t.Fatalf("Counts still aggregates accounts globally with %q:\n%s", banned, query)
		}
	}
	if !strings.Contains(query, "FROM users u") {
		t.Fatalf("Counts does not scan users:\n%s", query)
	}
	if !strings.Contains(query, "LEFT JOIN LATERAL") {
		t.Fatalf("Counts is not a single users scan with per-user lateral aggregate:\n%s", query)
	}
	if !strings.Contains(query, "WHERE a.owner_user_id = u.id AND a.deleted_at IS NULL") {
		t.Fatalf("Counts lateral aggregate is not owner-scoped:\n%s", query)
	}
}
