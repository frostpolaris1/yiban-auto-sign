package account

// 设备刷新的急停透传守卫（批 abs R1 / F2）。
//
// 判据：平台端口返回宿主自产 paused 错误时，用例原样上抛（入口回 503），
// 不得折叠成 upstream_unavailable，也不得进入写回事务。

import (
	"bytes"
	"context"
	"errors"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

func TestDeviceRefreshPassesThroughPausedGate(t *testing.T) {
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
	repo := &fakeAccountRepo{
		record: Record{ID: "acc-1", SchoolID: "school-1", Phone: "13800000000"},
		cred:   sealed,
	}
	device := &fakeDeviceReader{err: errs.NewPaused()}
	svc, err := NewService(Deps{
		Accounts: repo,
		Schools:  fakeSchoolReader{},
		Audit:    fakeAuditRepo{},
		Tx:       fakeTxManager{},
		Clock:    clock.Fixed{T: time.Date(2026, 11, 11, 6, 0, 0, 0, time.UTC)},
		Cipher:   cipher,
		Owner:    fakeOwnerReader{},
		Device:   device,
		Quota:    fixedQuota(10),
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	_, err = svc.DeviceRefresh(context.Background(), corescope.Master(), "acc-1", appaudit.Actor{UserID: "m-1"})
	if err == nil {
		t.Fatal("paused DeviceRefresh must fail")
	}
	var paused *errs.PausedError
	if err == nil || !errors.As(err, &paused) {
		t.Fatalf("err = %v, want paused（不得折叠成 upstream）", err)
	}
	if device.calls != 1 {
		t.Fatalf("device port calls = %d, want 1", device.calls)
	}
}
