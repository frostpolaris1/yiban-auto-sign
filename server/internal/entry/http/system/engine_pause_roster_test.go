package system_test

// 全局暂停键（engine.paused，批 3a）与只读名册的关系守卫。
//
// 判据：engine.paused 是 DB-only 机制键，不在 registry 名册内——
// 即使 settings 表已存在该覆盖行，GET /api/admin/settings 也**不得**
// 把它列出来（source=db 通路只服务 registry 登记的键）。

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
)

// 名册不显示 engine.paused：写回键集之外的行不进 source=db 通路。
func TestEnginePausedKeyIsNotInRoster(t *testing.T) {
	store := newMemScheduleStore()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "YIBAN_WINDOW_EDGE_FRONT_SEC", Group: "调度", Default: float64(60), HasDefault: true},
	}, mapSource{})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	roster.WithOverrides(resolver)
	audits := &memAudit{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &memAnnouncementStore{}, Audit: audits, Tx: passthroughTx{}, Clock: fixedClock{},
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	handler := systemhttp.NewHandler(roster, svc, nil)

	// settings 表里确实写入了 engine.paused=true（模拟暂停已生效）。
	actor := appaudit.Actor{UserID: "11111111-1111-1111-1111-111111111111",
		Role: coreauth.RoleMaster, Label: "m***@example.com"}
	if err := svc.SetEnginePaused(context.Background(), corescope.Master(), actor, true); err != nil {
		t.Fatalf("SetEnginePaused: %v", err)
	}
	if value, ok := store.boolRows[appsettings.KeyEnginePaused]; !ok || value != "true" {
		t.Fatalf("engine.paused row = (%q, %v), want true", value, ok)
	}

	// 名册不得包含该键：Known 恒 false（registry 无键），覆盖行不进读面。
	c, rec := newContextWithEmail(http.MethodGet, "/api/admin/settings", "", coreauth.RoleMaster, "m@example.com")
	handler.AdminSettings(c)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET settings = %d: %s", rec.Code, rec.Body.String())
	}
	var parsed struct {
		Groups []struct {
			Items []struct {
				Key string `json:"key"`
			} `json:"items"`
		} `json:"groups"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &parsed); err != nil {
		t.Fatalf("decode roster: %v", err)
	}
	for _, group := range parsed.Groups {
		for _, item := range group.Items {
			if item.Key == appsettings.KeyEnginePaused {
				t.Fatal("engine.paused must not appear in the settings roster")
			}
		}
	}
}
