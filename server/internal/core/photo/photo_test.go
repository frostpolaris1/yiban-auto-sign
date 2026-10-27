package photo_test

import (
	"testing"
	"time"

	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
)

// 保留期只接受闭集。
func TestValidRetention(t *testing.T) {
	for _, days := range []int{30, 90, 180, 365} {
		if !corephoto.ValidRetention(days) {
			t.Fatalf("%d 应在闭集内", days)
		}
	}
	for _, days := range []int{0, -1, 29, 45, 91, 364, 366} {
		if corephoto.ValidRetention(days) {
			t.Fatalf("%d 不应在闭集内", days)
		}
	}
}

// 到期判据：恰好满保留期即到期；软删行从删除时刻起算。
func TestRecordExpired(t *testing.T) {
	now := time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC)
	rec := corephoto.Record{CreatedAt: now.Add(-90 * 24 * time.Hour)}
	if !rec.Expired(now, 90) {
		t.Fatal("满 90 天应到期")
	}
	if rec.Expired(now.Add(-time.Nanosecond), 90) {
		t.Fatal("未满 90 天不应到期")
	}
	if rec.Expired(now, 0) {
		t.Fatal("保留期非正不得到期")
	}
	deletedAt := now
	rec.DeletedAt = &deletedAt
	if rec.Expired(now, 90) {
		t.Fatal("软删未满保留期不得到期（从删除时刻起算）")
	}
	oldDelete := now.Add(-91 * 24 * time.Hour)
	recent := corephoto.Record{CreatedAt: now.Add(-time.Hour), DeletedAt: &oldDelete}
	if !recent.Expired(now, 90) {
		t.Fatal("软删满 91 天应到期，即使创建时间很近")
	}
	if !rec.Deleted() {
		t.Fatal("Deleted() 应为真")
	}
}
