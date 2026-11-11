package account

// 账号列表用例的守卫单测。
// 判据：零值作用域是调用方缺陷，必须在到达仓储之前拒绝。
// 仓储只接受已定型的范围；把无效范围下传会让权限判断依赖适配器。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

type fakeListRepo struct {
	Repo
	lists           int
	items           []Record
	lastScope       corescope.Scope
	lastSchoolID    string
	lastStatus      Status
	lastLimit       int
	credentialCalls int
	credentialIDs   []string
	credentials     map[string]Credential
	credentialErr   error
}

func (f *fakeListRepo) List(_ context.Context, scope corescope.Scope, schoolID string, status Status, _, _ string, limit int, _ bool) ([]Record, string, error) {
	f.lists++
	f.lastScope = scope
	f.lastSchoolID = schoolID
	f.lastStatus = status
	f.lastLimit = limit
	return f.items, "", nil
}

func (f *fakeListRepo) Credentials(_ context.Context, _ corescope.Scope, ids []string) (map[string]Credential, error) {
	f.credentialCalls++
	f.credentialIDs = append([]string(nil), ids...)
	if f.credentialErr != nil {
		return nil, f.credentialErr
	}
	return f.credentials, nil
}

// newListService 构造只关心 List 的用例。
func newListService(t *testing.T, repo Repo, cipher *Cipher) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Accounts: repo,
		Schools:  fakeSchoolReader{},
		Audit:    fakeAuditRepo{},
		Tx:       fakeTxManager{},
		Clock:    clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Cipher:   cipher,
		Owner:    fakeOwnerReader{},
		Device:   &fakeDeviceReader{},
		Quota:    fixedQuota(10),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// failingOwnerReader 模拟属主读库失败：基础设施错误，不是资源缺失。
type failingOwnerReader struct{ OwnerReader }

func (failingOwnerReader) GetByID(context.Context, string) (coreuser.Record, error) {
	return coreuser.Record{}, errors.New("db connection lost")
}

// Create 指定属主时，OwnerReader 的非 not_found 错误必须原样上抛；
// 改写成 404 会把读库故障伪装成「用户不存在」，误导操作者。
func TestCreateOwnerReaderFailurePassesThrough(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}}}
	svc, err := NewService(Deps{
		Accounts: repo,
		Schools:  fakeSchoolReader{},
		Audit:    fakeAuditRepo{},
		Tx:       fakeTxManager{},
		Clock:    clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Owner:    failingOwnerReader{},
		Device:   &fakeDeviceReader{},
		Quota:    fixedQuota(10),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}
	_, err = svc.Create(context.Background(), corescope.Master(), CreateInput{
		SchoolID:    "school-1",
		Phone:       "13800138000",
		Password:    "secret",
		OwnerUserID: "owner-1",
	}, actor)
	if err == nil {
		t.Fatal("Create succeeded while the owner reader was failing")
	}
	if err.Error() != "db connection lost" {
		t.Fatalf("Create rewrote the reader error: %v", err)
	}
	if strings.Contains(err.Error(), "not found") || strings.Contains(err.Error(), "owner user") {
		t.Fatalf("the failure must not read as not_found: %v", err)
	}
}

// TestListRejectsZeroValueScope 钉住 List 的入口守卫。
// 删掉 RequireScope 时，零值作用域会被下传到仓储——本用例红。
func TestListRejectsZeroValueScope(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}}}
	svc, err := NewService(Deps{
		Accounts: repo,
		Schools:  fakeSchoolReader{},
		Audit:    fakeAuditRepo{},
		Tx:       fakeTxManager{},
		Clock:    clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Owner:    fakeOwnerReader{},
		Device:   &fakeDeviceReader{},
		Quota:    fixedQuota(10),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	if _, err := svc.List(context.Background(), corescope.Scope{}, ListFilter{}, "", 10); err == nil {
		t.Fatal("List accepted a zero-value scope")
	} else if got := errs.From(err).Code; got != errs.CodeInternal {
		t.Fatalf("zero-value scope maps to %q; want internal_error", got)
	}
	if repo.lists != 0 {
		t.Fatalf("List queried the repository %d times with a zero-value scope", repo.lists)
	}
}

// FirstActiveForSchool 复用 List 的 master 口径：只取该校 active 账号的第一条。
func TestFirstActiveForSchoolUsesActiveFilter(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}, {ID: "acc-2"}}}
	svc := newListService(t, repo, nil)
	got, err := svc.FirstActiveForSchool(context.Background(), "school-1")
	if err != nil {
		t.Fatalf("FirstActiveForSchool: %v", err)
	}
	if got.ID != "acc-1" {
		t.Fatalf("account = %q, want acc-1", got.ID)
	}
	if repo.lastSchoolID != "school-1" || repo.lastStatus != StatusActive || repo.lastLimit != 1 {
		t.Fatalf("filter = school %q status %q limit %d, want school-1/active/1",
			repo.lastSchoolID, repo.lastStatus, repo.lastLimit)
	}
	if repo.lastScope.Kind() != corescope.KindMaster {
		t.Fatalf("scope = %v, want master", repo.lastScope.Kind())
	}
}

// 空列表是 not_found：计划层据此回退缓存窗口，不当平台故障。
func TestFirstActiveForSchoolEmptyIsNotFound(t *testing.T) {
	repo := &fakeListRepo{}
	svc := newListService(t, repo, nil)
	_, err := svc.FirstActiveForSchool(context.Background(), "school-1")
	if err == nil {
		t.Fatal("empty school returned an account")
	}
	if code := errs.From(err).Code; code != errs.CodeNotFound {
		t.Fatalf("code = %q, want not_found", code)
	}
}

// newTestCipher 构造 32 字节测试钥的凭据加解密器。
func newTestCipher(t *testing.T) *Cipher {
	t.Helper()
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	return cipher
}

// TestListSkipsCredentialReadWithoutCipher 钉住 fail-open 的展示面：
// 未装配 Cipher 时列表仍成功，且不触发凭据查询。
func TestListSkipsCredentialReadWithoutCipher(t *testing.T) {
	repo := &fakeListRepo{items: []Record{{ID: "acc-1"}}}
	svc := newListService(t, repo, nil)

	page, err := svc.List(context.Background(), corescope.Master(), ListFilter{}, "", 10)
	if err != nil {
		t.Fatalf("List without a cipher: %v", err)
	}
	if repo.credentialCalls != 0 {
		t.Fatalf("List read credentials %d times without a cipher", repo.credentialCalls)
	}
	if len(page.Items) != 1 || page.Items[0].PhoneModel != "" {
		t.Fatalf("page items = %+v, want one row with an empty model", page.Items)
	}
}

// TestListReturnsCredentialReadError 钉住批量读库失败必须上抛，不伪装成空机型。
func TestListReturnsCredentialReadError(t *testing.T) {
	repo := &fakeListRepo{
		items:         []Record{{ID: "acc-1"}},
		credentialErr: errors.New("db down"),
	}
	svc := newListService(t, repo, newTestCipher(t))

	if _, err := svc.List(context.Background(), corescope.Master(), ListFilter{}, "", 10); err == nil {
		t.Fatal("List swallowed a credential read failure")
	} else if err.Error() != "db down" {
		t.Fatalf("List rewrote the credential read failure: %v", err)
	}
	if repo.credentialCalls != 1 {
		t.Fatalf("credential reads = %d, want 1", repo.credentialCalls)
	}
}

// TestListEmptyPageSkipsCredentialRead 钉住空页不发批量查询。
func TestListEmptyPageSkipsCredentialRead(t *testing.T) {
	repo := &fakeListRepo{}
	svc := newListService(t, repo, newTestCipher(t))

	page, err := svc.List(context.Background(), corescope.Master(), ListFilter{}, "", 10)
	if err != nil {
		t.Fatalf("List on an empty page: %v", err)
	}
	if len(page.Items) != 0 {
		t.Fatalf("empty page returned %d rows", len(page.Items))
	}
	if repo.credentialCalls != 0 {
		t.Fatalf("empty page read credentials %d times", repo.credentialCalls)
	}
}

// TestListDecryptFailureLeavesModelEmpty 钉住坏凭据只影响该行展示。
func TestListDecryptFailureLeavesModelEmpty(t *testing.T) {
	repo := &fakeListRepo{
		items: []Record{{ID: "acc-1"}},
		credentials: map[string]Credential{
			"acc-1": {AccountID: "acc-1", SchoolID: "school-1", KID: "0000000000000000"},
		},
	}
	svc := newListService(t, repo, newTestCipher(t))

	page, err := svc.List(context.Background(), corescope.Master(), ListFilter{}, "", 10)
	if err != nil {
		t.Fatalf("List with an unreadable credential: %v", err)
	}
	if len(page.Items) != 1 || page.Items[0].PhoneModel != "" {
		t.Fatalf("page items = %+v, want the row with an empty model", page.Items)
	}
	if repo.credentialCalls != 1 {
		t.Fatalf("credential reads = %d, want 1", repo.credentialCalls)
	}
}

// TestListFillsModelFromCredential 钉住成功路径把机型写回行。
func TestListFillsModelFromCredential(t *testing.T) {
	cipher := newTestCipher(t)
	credential, err := cipher.Seal("school-1", "acc-1", CredentialPayload{PhoneModel: "Pixel 7"})
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	repo := &fakeListRepo{
		items:       []Record{{ID: "acc-1"}},
		credentials: map[string]Credential{"acc-1": credential},
	}
	svc := newListService(t, repo, cipher)

	page, err := svc.List(context.Background(), corescope.Master(), ListFilter{}, "", 10)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(page.Items) != 1 || page.Items[0].PhoneModel != "Pixel 7" {
		t.Fatalf("page items = %+v, want Pixel 7", page.Items)
	}
}
