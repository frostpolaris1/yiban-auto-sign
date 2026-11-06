package account

// 账号编辑乐观锁的守卫单测。
//
// 判据：
//   - expected_updated_at 与行锁下读到的 updated_at 相等时放行；
//   - 不等时 409 conflict，账号字段与审计都不动；
//   - 省略或零值不校验，旧客户端行为不变；
//   - 乐观锁判定先于字段校验：过期请求带非法字段仍是 409，不是 400。
//
// 事务体由 reviewFakeTx 直接执行，仓储复用 reviewFakeRepo。
// 真实行锁与 PG 微秒精度由 pg 与 cmd/server 的真库 e2e 覆盖。

import (
	"context"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// updateInstant 带微秒，与 PostgreSQL timestamptz 的存储精度同口径。
var updateInstant = time.Date(2026, 11, 6, 12, 0, 0, 123456000, time.UTC)

// newOptimisticService 构造只关心 Update 的用例与审计记录器。
func newOptimisticService(t *testing.T, repo Repo) (*Service, *reviewFakeAudit) {
	t.Helper()
	audit := &reviewFakeAudit{}
	svc, err := NewService(Deps{
		Accounts:   repo,
		Schools:    reviewFakeSchools{},
		Audit:      audit,
		Tx:         reviewFakeTx{},
		Clock:      clock.Fixed{T: time.Date(2026, 11, 6, 12, 1, 0, 0, time.UTC)},
		Cipher:     mustCipher(t),
		Owner:      reviewFakeOwners{},
		Device:     &fakeDeviceReader{},
		Limit:      10,
		OwnerLimit: 10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, audit
}

func mustCipher(t *testing.T) *Cipher {
	t.Helper()
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	return cipher
}

// optimisticRecord 是一行带已知 updated_at 的活跃账号。
func optimisticRecord() Record {
	return Record{
		ID: "acct-1", SchoolID: "school-1", OwnerUserID: "owner-1",
		Phone: "13800138000", Status: StatusActive, UpdatedAt: updateInstant,
	}
}

// 相等放行：账号改成功，写一条 account.update 审计。
func TestUpdateAcceptsMatchingUpdatedAt(t *testing.T) {
	repo := &reviewFakeRepo{current: optimisticRecord()}
	svc, audit := newOptimisticService(t, repo)
	name := "新的备注名"
	expected := updateInstant

	if _, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
		UpdateInput{Name: &name, ExpectedUpdatedAt: &expected},
		appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
		t.Fatalf("Update 在 updated_at 相等时必须放行: %v", err)
	}
	if repo.current.Name != name {
		t.Fatalf("name = %q, want %q", repo.current.Name, name)
	}
	if len(audit.entries) != 1 || audit.entries[0].Action != "account.update" {
		t.Fatalf("审计 = %+v, want 一条 account.update", audit.entries)
	}
}

// 不等即 409，且账号、状态与审计都不动。
func TestUpdateRejectsStaleUpdatedAt(t *testing.T) {
	repo := &reviewFakeRepo{current: optimisticRecord()}
	svc, audit := newOptimisticService(t, repo)
	name := "不该落库的备注名"
	expected := updateInstant.Add(time.Microsecond)

	_, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
		UpdateInput{Name: &name, ExpectedUpdatedAt: &expected},
		appaudit.Actor{UserID: "master-1", Role: "master"})
	if err == nil {
		t.Fatal("Update 在 updated_at 不等时必须失败")
	}
	if got := errs.From(err).Code; got != errs.CodeConflict {
		t.Fatalf("错误码 = %q, want conflict: %v", got, err)
	}
	if got := errs.From(err).Message; got != "account was modified by another request" {
		t.Fatalf("错误消息 = %q", got)
	}
	if repo.current.Name != "" {
		t.Fatalf("过期请求写了备注名: %q", repo.current.Name)
	}
	if repo.setStatus != 0 {
		t.Fatalf("过期请求写了状态: %d 次", repo.setStatus)
	}
	if len(audit.entries) != 0 {
		t.Fatalf("过期请求写了审计: %+v", audit.entries)
	}
}

// 省略与零值都不校验：旧客户端保持 last-write-wins。
func TestUpdateIgnoresAbsentOrZeroUpdatedAt(t *testing.T) {
	for _, tc := range []struct {
		name     string
		expected *time.Time
	}{
		{"省略", nil},
		{"零值", &time.Time{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			repo := &reviewFakeRepo{current: optimisticRecord()}
			svc, audit := newOptimisticService(t, repo)
			name := "旧客户端备注"

			if _, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
				UpdateInput{Name: &name, ExpectedUpdatedAt: tc.expected},
				appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
				t.Fatalf("不校验时 Update 必须放行: %v", err)
			}
			if repo.current.Name != name {
				t.Fatalf("name = %q, want %q", repo.current.Name, name)
			}
			if len(audit.entries) != 1 {
				t.Fatalf("审计 = %d 条, want 1", len(audit.entries))
			}
		})
	}
}

// 乐观锁先于字段校验：过期请求带非法手机号仍报 409，不报 400。
func TestUpdateStaleBeatsFieldValidation(t *testing.T) {
	repo := &reviewFakeRepo{current: optimisticRecord()}
	svc, audit := newOptimisticService(t, repo)
	invalid := "not-a-phone"
	expected := updateInstant.Add(time.Second)

	_, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
		UpdateInput{Phone: &invalid, ExpectedUpdatedAt: &expected},
		appaudit.Actor{UserID: "master-1", Role: "master"})
	if got := errs.From(err).Code; got != errs.CodeConflict {
		t.Fatalf("错误码 = %q, want conflict（字段校验不得抢先）", got)
	}
	if len(audit.entries) != 0 {
		t.Fatalf("过期请求写了审计: %+v", audit.entries)
	}
}
