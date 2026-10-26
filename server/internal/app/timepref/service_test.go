package timepref_test

import (
	"context"
	"testing"
	"time"

	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// fakeStore 在内存里保存一条偏好，形状与 pg 实现一致。
type fakeStore struct {
	pref  coreschedule.TimePref
	found bool
	puts  []*int
}

func (f *fakeStore) Get(context.Context, string) (coreschedule.TimePref, error) {
	if !f.found {
		return coreschedule.TimePref{}, coreerrs.ErrNotFound
	}
	return f.pref, nil
}

func (f *fakeStore) Put(_ context.Context, userID string, offset *int, now time.Time) error {
	f.pref = coreschedule.TimePref{UserID: userID, OffsetMin: offset, UpdatedAt: now}
	f.found = true
	f.puts = append(f.puts, offset)
	return nil
}

func newService(t *testing.T, store *fakeStore, now time.Time, allow bool, cooldown time.Duration) *apptimepref.Service {
	t.Helper()
	svc, err := apptimepref.NewService(apptimepref.Deps{
		Store: store, Clock: coreclock.Fixed{T: now}, Allow: allow, Cooldown: cooldown,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func ptr(v int) *int { return &v }

// 无记录时读返回空值，且 allowed 如实反映部署开关。
func TestGetWithoutRecord(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	for _, allow := range []bool{true, false} {
		svc := newService(t, &fakeStore{}, now, allow, 30*time.Second)
		view, err := svc.Get(context.Background(), "u1")
		if err != nil {
			t.Fatalf("Get: %v", err)
		}
		if view.OffsetMin != nil || view.Allowed != allow {
			t.Fatalf("view = %+v, allow=%v", view, allow)
		}
	}
}

// 关闭开关时写入被拒绝；读仍返回已存值。
func TestDisabledRefusesWrite(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	store := &fakeStore{}
	svc := newService(t, store, now, false, 0)
	if _, err := svc.Set(context.Background(), "u1", ptr(30)); err == nil {
		t.Fatal("关闭开关应拒绝写入")
	} else if code := coreerrs.From(err).Code; code != coreerrs.CodeForbidden {
		t.Fatalf("错误码 = %q, want forbidden", code)
	}
	if len(store.puts) != 0 {
		t.Fatal("拒绝写入后不得落库")
	}
}

// 偏移必须是 0..1439 之间的 5 的倍数。
func TestOffsetValidation(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	cases := []int{-5, 1, 7, 1440, 1500}
	for _, off := range cases {
		svc := newService(t, &fakeStore{}, now, true, 0)
		_, err := svc.Set(context.Background(), "u1", ptr(off))
		if err == nil {
			t.Fatalf("偏移 %d 应被拒绝", off)
		}
		if code := coreerrs.From(err).Code; code != coreerrs.CodeValidation {
			t.Fatalf("偏移 %d 错误码 = %q", off, code)
		}
	}
	for _, off := range []int{0, 5, 1435} {
		store := &fakeStore{}
		svc := newService(t, store, now, true, 0)
		if _, err := svc.Set(context.Background(), "u1", ptr(off)); err != nil {
			t.Fatalf("偏移 %d 应放行: %v", off, err)
		}
	}
}

// 冷却期内拒绝切换；恰好满冷却放行；清除同样受冷却约束。
func TestCooldown(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	store := &fakeStore{found: true, pref: coreschedule.TimePref{
		UserID: "u1", OffsetMin: ptr(10), UpdatedAt: now.Add(-10 * time.Second),
	}}
	svc := newService(t, store, now, true, 30*time.Second)
	if _, err := svc.Set(context.Background(), "u1", ptr(20)); err == nil {
		t.Fatal("冷却期内应拒绝")
	} else if code := coreerrs.From(err).Code; code != coreerrs.CodeRateLimited {
		t.Fatalf("错误码 = %q, want rate_limited", code)
	}
	if _, err := svc.Set(context.Background(), "u1", nil); err == nil {
		t.Fatal("冷却期内清除也应拒绝")
	}

	// 恰好等于冷却间隔：放行。
	store.pref.UpdatedAt = now.Add(-30 * time.Second)
	view, err := svc.Set(context.Background(), "u1", ptr(20))
	if err != nil {
		t.Fatalf("满冷却应放行: %v", err)
	}
	if view.OffsetMin == nil || *view.OffsetMin != 20 {
		t.Fatalf("view = %+v", view)
	}
}

// 首次写入没有冷却：新用户可立即设置。
func TestFirstWriteNoCooldown(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	store := &fakeStore{}
	svc := newService(t, store, now, true, time.Hour)
	if _, err := svc.Set(context.Background(), "u1", ptr(60)); err != nil {
		t.Fatalf("首次写入应放行: %v", err)
	}
}

// 清除写 NULL，并返回空偏移。
func TestClear(t *testing.T) {
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)
	store := &fakeStore{}
	svc := newService(t, store, now, true, 0)
	if _, err := svc.Set(context.Background(), "u1", ptr(60)); err != nil {
		t.Fatalf("set: %v", err)
	}
	view, err := svc.Set(context.Background(), "u1", nil)
	if err != nil {
		t.Fatalf("clear: %v", err)
	}
	if view.OffsetMin != nil || store.pref.OffsetMin != nil {
		t.Fatalf("清除后仍非空: view=%+v store=%+v", view, store.pref)
	}
}
