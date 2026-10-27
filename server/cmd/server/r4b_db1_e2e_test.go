package main

// R4B db1 e2e：管理面按 credential_error 过滤账号。
//
// 判据：账号进入 credential_error 后，GET /api/accounts?status=credential_error
// 能查到该行；status=active 查不到。前者证明状态值可被管理面选中，
// 后者证明该账号已退出可签到集合。

import (
	"net/http"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

func TestR4BDb1AdminListsCredentialError(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("db1-list-master@example.com", coreauth.RoleMaster)
	school := e.school("db1-list")
	accountID := e.account(school.ID, master.ID, "13800009601")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = 'credential_error' WHERE id = $1::uuid", accountID); err != nil {
		t.Fatalf("set credential_error: %v", err)
	}

	rec := e.do(http.MethodGet, "/api/accounts?status=credential_error", nil, e.login(master))
	w2cWantStatus(t, rec, http.StatusOK)
	var page oapi.AccountPage
	e.decode(rec, &page)
	found := false
	for _, item := range page.Items {
		if item.Id == accountID {
			found = true
			if item.Status != oapi.AccountStatusCredentialError {
				t.Fatalf("listed status = %q, want credential_error", item.Status)
			}
		}
	}
	if !found {
		t.Fatalf("credential_error filter returned %d rows, none is %s", len(page.Items), accountID)
	}

	rec = e.do(http.MethodGet, "/api/accounts?status=active", nil, e.login(master))
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &page)
	for _, item := range page.Items {
		if item.Id == accountID {
			t.Fatalf("a credential_error account appears in the active filter")
		}
	}
}
