package main

// 审计链巡检端点 e2e：真 PostgreSQL + 真路由。
//
// # 功能
// 钉住四条验收：空链不是通过；合法链 ok=true 且报告链头哈希；
// 直接改库破坏一行后 broken_at 命中该行且不复述审计正文；
// user 403，admin 与 master 200（断链也是 200 的巡检结论）。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离、会话签发与 HTTP 客户端。
//
// # 口径
// 破坏行走“先禁用追加保护触发器、改一行、立即恢复”的测试路径；
// 生产代码没有任何改行入口。缺 DSN 时打可见横幅再跳过。

import (
	"context"
	"encoding/hex"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	anchoradapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/anchor"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

type auditStatusBody struct {
	OK           bool      `json:"ok"`
	Empty        bool      `json:"empty"`
	CheckedRows  int64     `json:"checked_rows"`
	LastHash     string    `json:"last_hash"`
	BrokenAt     *int64    `json:"broken_at"`
	CheckedAt    time.Time `json:"checked_at"`
	AnchorStatus string    `json:"anchor_status"`
	AnchorOK     bool      `json:"anchor_ok"`
	AnchorReason string    `json:"anchor_reason"`
	AnchorSeq    int64     `json:"anchor_seq"`
	AnchorCount  int64     `json:"anchor_count"`
}

func (e *w2cEnv) auditStatus(issued *coreauth.Issued) auditStatusBody {
	e.t.Helper()
	rec := e.do(http.MethodGet, "/api/admin/audit-status", nil, issued)
	w2cWantStatus(e.t, rec, http.StatusOK)
	var body auditStatusBody
	e.decode(rec, &body)
	return body
}

func TestAuditStatusE2E(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("e2c-master@example.com", coreauth.RoleMaster)
	adminUser := e.user("e2c-admin@example.com", coreauth.RoleAdmin)
	plain := e.user("e2c-user@example.com", coreauth.RoleUser)
	masterSession := e.login(master)

	// 1. 空链：零行，什么都没证明，不是通过。
	empty := e.auditStatus(masterSession)
	if empty.OK || !empty.Empty {
		t.Fatalf("empty chain = %+v, want ok=false empty=true", empty)
	}
	if empty.CheckedRows != 0 || empty.LastHash != "" || empty.BrokenAt != nil {
		t.Fatalf("empty chain = %+v, want zero rows and no anchor", empty)
	}
	if empty.AnchorStatus != "disabled" || empty.AnchorOK {
		t.Fatalf("anchor status without a configured path = %+v, want disabled", empty)
	}

	// 2. 三条合法行：ok=true，链头与库内一致。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		for _, action := range []coreaudit.Action{
			coreaudit.ActionUserCreate,
			coreaudit.ActionSchoolCreate,
			coreaudit.ActionAdminGrant,
		} {
			if _, err := e.audits.Append(ctx, appaudit.Entry{
				TS:         time.Date(2026, 11, 8, 8, 0, 0, 0, time.UTC),
				ActorLabel: "e2c-master",
				Action:     action,
				Detail:     map[string]any{"field": "role"},
			}); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		t.Fatalf("append audit rows: %v", err)
	}

	headHash := e.scalarString("SELECT encode(hash, 'hex') FROM audit_logs ORDER BY id DESC LIMIT 1")
	intact := e.auditStatus(masterSession)
	if !intact.OK || intact.Empty {
		t.Fatalf("intact chain = %+v, want ok=true empty=false", intact)
	}
	if intact.CheckedRows != 3 {
		t.Fatalf("checked rows = %d, want 3", intact.CheckedRows)
	}
	if intact.LastHash != headHash {
		t.Fatalf("last hash = %q, want %q", intact.LastHash, headHash)
	}
	if intact.BrokenAt != nil {
		t.Fatalf("broken_at = %v, want null", intact.BrokenAt)
	}
	if intact.CheckedAt.IsZero() {
		t.Fatal("checked_at is zero")
	}

	// 3. 破坏第二行：链校验必须定位到该行，且响应不带审计正文。
	firstID := e.scalarInt("SELECT id FROM audit_logs ORDER BY id LIMIT 1")
	secondID := e.scalarInt("SELECT id FROM audit_logs ORDER BY id OFFSET 1 LIMIT 1")
	firstHash := e.scalarString("SELECT encode(hash, 'hex') FROM audit_logs WHERE id = $1", firstID)
	if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only"); err != nil {
		t.Fatalf("disable append-only trigger: %v", err)
	}
	t.Cleanup(func() {
		_, _ = e.db.Exec(context.Background(), "ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
	})
	if _, err := e.db.Exec(e.ctx,
		`UPDATE audit_logs SET detail = '{"field":"reason"}' WHERE id = $1`, secondID); err != nil {
		t.Fatalf("tamper with a row: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only"); err != nil {
		t.Fatalf("re-enable append-only trigger: %v", err)
	}

	rec := e.do(http.MethodGet, "/api/admin/audit-status", nil, masterSession)
	w2cWantStatus(t, rec, http.StatusOK)
	var broken auditStatusBody
	e.decode(rec, &broken)
	if broken.OK || broken.Empty {
		t.Fatalf("broken chain = %+v, want ok=false empty=false", broken)
	}
	if broken.BrokenAt == nil || *broken.BrokenAt != secondID {
		t.Fatalf("broken_at = %v, want %d", broken.BrokenAt, secondID)
	}
	if broken.CheckedRows != 2 {
		t.Fatalf("checked rows = %d, want 2", broken.CheckedRows)
	}
	if broken.LastHash != firstHash {
		t.Fatalf("last hash = %q, want %q", broken.LastHash, firstHash)
	}
	for _, forbidden := range []string{"field", "role", "reason"} {
		if strings.Contains(rec.Body.String(), forbidden) {
			t.Fatalf("response leaked audit detail %q: %s", forbidden, rec.Body.String())
		}
	}

	// 4. 角色门：user 403；admin（未绑定学校）与 master 200。
	userRec := e.do(http.MethodGet, "/api/admin/audit-status", nil, e.login(plain))
	w2cWantStatus(t, userRec, http.StatusForbidden)
	if code := e.errorCode(userRec); code != "forbidden" {
		t.Fatalf("user error code = %q", code)
	}
	adminRec := e.do(http.MethodGet, "/api/admin/audit-status", nil, e.login(adminUser))
	w2cWantStatus(t, adminRec, http.StatusOK)

	// 链头哈希是 64 位小写十六进制；解码长度 32 字节。
	raw, err := hex.DecodeString(headHash)
	if err != nil || len(raw) != 32 {
		t.Fatalf("head hash %q is not a 32-byte hex string: %v", headHash, err)
	}
}

// 库外锚点：删前缀、截尾、整表清空都必须把巡检翻红。
// 前缀删除后剩余链仍自洽（首行 prev_hash 不比对），只有锚点行数能发现。
func TestAuditStatusAnchorE2E(t *testing.T) {
	appendRows := func(t *testing.T, e *w2cEnv) {
		t.Helper()
		if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
			for _, action := range []coreaudit.Action{
				coreaudit.ActionUserCreate,
				coreaudit.ActionSchoolCreate,
				coreaudit.ActionAdminGrant,
			} {
				if _, err := e.audits.Append(ctx, appaudit.Entry{
					TS:         time.Date(2026, 11, 8, 8, 0, 0, 0, time.UTC),
					ActorLabel: "anchor-master",
					Action:     action,
				}); err != nil {
					return err
				}
			}
			return nil
		}); err != nil {
			t.Fatalf("append audit rows: %v", err)
		}
	}
	writeAnchor := func(t *testing.T, e *w2cEnv, path string) {
		t.Helper()
		lastID := e.scalarInt("SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1")
		hashHex := e.scalarString("SELECT encode(hash, 'hex') FROM audit_logs WHERE id = $1", lastID)
		hash, err := hex.DecodeString(hashHex)
		if err != nil || len(hash) != 32 {
			t.Fatalf("decode head hash: %v", err)
		}
		if err := anchoradapter.NewFile(path).Append(e.ctx, coreaudit.Anchor{
			Seq: lastID, Count: 3, Hash: hash,
		}, time.Date(2026, 11, 11, 1, 0, 0, 0, time.UTC)); err != nil {
			t.Fatalf("append anchor: %v", err)
		}
	}
	tamper := func(t *testing.T, e *w2cEnv, statement string) {
		t.Helper()
		if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only"); err != nil {
			t.Fatalf("disable append-only trigger: %v", err)
		}
		t.Cleanup(func() {
			_, _ = e.db.Exec(context.Background(), "ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only")
		})
		if _, err := e.db.Exec(e.ctx, statement); err != nil {
			t.Fatalf("tamper: %v", err)
		}
		if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only"); err != nil {
			t.Fatalf("re-enable append-only trigger: %v", err)
		}
	}

	cases := []struct {
		name      string
		statement string
		check     func(t *testing.T, body auditStatusBody)
	}{
		{
			name:      "prefix deletion",
			statement: "DELETE FROM audit_logs WHERE id = (SELECT min(id) FROM audit_logs)",
			check: func(t *testing.T, body auditStatusBody) {
				if body.BrokenAt != nil {
					t.Fatalf("remaining chain should be internally consistent, broken_at = %v", body.BrokenAt)
				}
				if body.Empty {
					t.Fatal("prefix deletion must not look empty")
				}
			},
		},
		{
			name:      "tail truncation",
			statement: "DELETE FROM audit_logs WHERE id = (SELECT max(id) FROM audit_logs)",
			check: func(t *testing.T, body auditStatusBody) {
				if body.BrokenAt != nil {
					t.Fatalf("remaining chain should be internally consistent, broken_at = %v", body.BrokenAt)
				}
			},
		},
		{
			name:      "whole table",
			statement: "DELETE FROM audit_logs",
			check: func(t *testing.T, body auditStatusBody) {
				if !body.Empty {
					t.Fatal("an emptied table must report empty")
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			anchorPath := filepath.Join(t.TempDir(), "audit-anchor.log")
			e := w2cNewEnvWithAnchor(t, anchorPath)
			master := e.user("anchor-"+strings.ReplaceAll(tc.name, " ", "-")+"@example.com", coreauth.RoleMaster)
			session := e.login(master)
			appendRows(t, e)
			writeAnchor(t, e, anchorPath)

			before := e.auditStatus(session)
			if !before.OK || before.AnchorStatus != "ok" || !before.AnchorOK {
				t.Fatalf("before tamper = %+v, want ok with anchor ok", before)
			}
			if before.AnchorSeq != 3 || before.AnchorCount != 3 {
				t.Fatalf("anchor seq/count = %d/%d, want 3/3", before.AnchorSeq, before.AnchorCount)
			}

			tamper(t, e, tc.statement)
			after := e.auditStatus(session)
			tc.check(t, after)
			if after.OK || after.AnchorStatus != "mismatch" || after.AnchorOK {
				t.Fatalf("after tamper = %+v, want red with anchor mismatch", after)
			}
			if after.AnchorReason == "" {
				t.Fatal("anchor mismatch must carry a reason")
			}
		})
	}
}
