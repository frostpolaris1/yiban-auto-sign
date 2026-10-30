package account

import (
	"bytes"
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

type fakeAccountRepo struct {
	Repo
	record    Record
	cred      coreaccount.Credential
	err       error
	lastScope corescope.Scope
	credCalls int
}

func (f *fakeAccountRepo) Get(_ context.Context, scope corescope.Scope, _ string) (Record, error) {
	f.lastScope = scope
	return f.record, f.err
}

func (f *fakeAccountRepo) Credential(_ context.Context, scope corescope.Scope, _ string) (coreaccount.Credential, error) {
	f.lastScope = scope
	f.credCalls++
	return f.cred, f.err
}

type fakeSchoolReader struct{ SchoolReader }

type fakeAuditRepo struct{ appaudit.Repo }

type fakeTxManager struct{ app.TxManager }

func newCredentialService(t *testing.T, repo *fakeAccountRepo, cipher *Cipher) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Accounts:   repo,
		Schools:    fakeSchoolReader{},
		Audit:      fakeAuditRepo{},
		Tx:         fakeTxManager{},
		Clock:      clock.Fixed{T: time.Date(2026, 10, 26, 6, 0, 0, 0, time.UTC)},
		Cipher:     cipher,
		Limit:      10,
		OwnerLimit: 10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func TestSigningCredentialDecrypts(t *testing.T) {
	cipher, err := NewCipher(bytes.Repeat([]byte{7}, CredentialKeyBytes))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	sealed, err := cipher.Seal("school-1", "acc-1", CredentialPayload{
		DeviceCode: "device-code", Password: "secret", PhoneModel: "Model-X",
	})
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	repo := &fakeAccountRepo{
		record: Record{ID: "acc-1", SchoolID: "school-1", Phone: "13800000000"},
		cred:   sealed,
	}
	svc := newCredentialService(t, repo, cipher)
	got, err := svc.SigningCredential(context.Background(), corescope.Master(), "acc-1")
	if err != nil {
		t.Fatalf("SigningCredential: %v", err)
	}
	if got.SchoolID != "school-1" || got.Phone != "13800000000" ||
		got.Password != "secret" || got.DeviceCode != "device-code" || got.PhoneModel != "Model-X" {
		t.Fatalf("credential = %+v", got)
	}
	if repo.lastScope.Kind() != corescope.KindMaster {
		t.Fatalf("scope was not forwarded: %+v", repo.lastScope)
	}
}

func TestSigningCredentialRequiresCipher(t *testing.T) {
	repo := &fakeAccountRepo{record: Record{ID: "acc-1", SchoolID: "school-1"}}
	svc := newCredentialService(t, repo, nil)
	if _, err := svc.SigningCredential(context.Background(), corescope.Master(), "acc-1"); err == nil {
		t.Fatal("a missing cipher must fail closed")
	}
}

func TestSigningCredentialNotFound(t *testing.T) {
	repo := &fakeAccountRepo{err: errs.New(errs.CodeNotFound, "account not found")}
	cipher, err := NewCipher(bytes.Repeat([]byte{7}, CredentialKeyBytes))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	svc := newCredentialService(t, repo, cipher)
	if _, err := svc.SigningCredential(context.Background(), corescope.Master(), "acc-1"); err == nil {
		t.Fatal("not found must be returned")
	}
	if repo.credCalls != 0 {
		t.Fatalf("credential was read after a missing account: %d", repo.credCalls)
	}
}

// TestSigningCredentialRejectsZeroValueScope 钉住凭据读取的入口守卫。
// 零值作用域必须在读账号之前拒绝；删掉 RequireScope 时本用例红。
func TestSigningCredentialRejectsZeroValueScope(t *testing.T) {
	cipher, err := NewCipher(bytes.Repeat([]byte{7}, CredentialKeyBytes))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	sealed, err := cipher.Seal("school-1", "acc-1", CredentialPayload{
		Password: "secret", DeviceCode: "device-code", PhoneModel: "Model-X",
	})
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	// 仓储返回可用行与可解密凭据：无守卫时本调用会成功。
	repo := &fakeAccountRepo{record: Record{ID: "acc-1", SchoolID: "school-1"}, cred: sealed}
	svc := newCredentialService(t, repo, cipher)

	// 失败消息不打印返回值：它含明文口令。
	if _, err := svc.SigningCredential(context.Background(), corescope.Scope{}, "acc-1"); err == nil {
		t.Fatal("SigningCredential accepted a zero-value scope")
	} else if code := errs.From(err).Code; code != errs.CodeInternal {
		t.Fatalf("zero-value scope maps to %q; want internal_error", code)
	}
	if repo.credCalls != 0 {
		t.Fatalf("credential was read %d times with a zero-value scope", repo.credCalls)
	}
}
