package system_test

// 周末门与默认窗口四键（批 3b）与只读名册的关系守卫。
//
// 判据：schedule.sunday_sign、schedule.saturday_sign、
// schedule.default_sign_start_min、schedule.default_sign_end_min 是
// DB-only 机制键，不在 registry 名册内——即使 settings 表已存在覆盖行，
// GET /api/admin/settings 也**不得**把它们列出来（source=db 通路只服务
// registry 登记的键，engine.paused 批 3a 同口径）。

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

// 名册不显示批 3b 四键：写回键集之外的行不进 source=db 通路。
func TestScheduleWeekendKeysAreNotInRoster(t *testing.T) {
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
	handler := systemhttp.NewHandler(roster, svc, nil).WithScheduleSettings(svc)

	// settings 表里确实写入四键（模拟周末门开着、窗口已配置）。
	actor := appaudit.Actor{UserID: "11111111-1111-1111-1111-111111111111",
		Role: coreauth.RoleMaster, Label: "m***@example.com"}
	if err := svc.SetSchedule(context.Background(), corescope.Master(), actor,
		[]appsettings.ScheduleChange{
			{Key: appsettings.KeyScheduleSundaySign, Op: appsettings.ScheduleOpSet, Value: true},
			{Key: appsettings.KeyScheduleSaturdaySign, Op: appsettings.ScheduleOpSet, Value: true},
			{Key: appsettings.KeyScheduleDefaultSignStartMin, Op: appsettings.ScheduleOpSet, Value: 480},
			{Key: appsettings.KeyScheduleDefaultSignEndMin, Op: appsettings.ScheduleOpSet, Value: 540},
		}); err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	if value, ok := store.boolRows[appsettings.KeyScheduleSundaySign]; !ok || value != "true" {
		t.Fatalf("sunday row = (%q, %v), want true", value, ok)
	}
	if value, ok := store.intRows[appsettings.KeyScheduleDefaultSignEndMin]; !ok || value != "540" {
		t.Fatalf("end row = (%q, %v), want 540", value, ok)
	}

	// 名册不得包含四键：Known 恒 false（registry 无键），覆盖行不进读面。
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
			switch item.Key {
			case appsettings.KeyScheduleSundaySign, appsettings.KeyScheduleSaturdaySign,
				appsettings.KeyScheduleDefaultSignStartMin, appsettings.KeyScheduleDefaultSignEndMin:
				t.Fatalf("%s must not appear in the settings roster", item.Key)
			}
		}
	}
}

// 名册不显示 3c 的分布档键：写回键集之外的行不进 source=db 通路。
// 与 3b 四键同一守卫形态（engine.paused 批 3a 同口径）。
func TestScheduleSignDistKeyIsNotInRoster(t *testing.T) {
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
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &memAnnouncementStore{}, Audit: &memAudit{}, Tx: passthroughTx{}, Clock: fixedClock{},
		Schedule: store, Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	handler := systemhttp.NewHandler(roster, svc, nil).WithScheduleSettings(svc)

	actor := appaudit.Actor{UserID: "11111111-1111-1111-1111-111111111111",
		Role: coreauth.RoleMaster, Label: "m***@example.com"}
	if err := svc.SetSchedule(context.Background(), corescope.Master(), actor,
		[]appsettings.ScheduleChange{
			{Key: appsettings.KeyScheduleSignDist, Op: appsettings.ScheduleOpSet, Value: appsettings.SignDistUniform},
		}); err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	if store.strRows[appsettings.KeyScheduleSignDist] != "uniform" {
		t.Fatalf("sign_dist row = %q, want uniform", store.strRows[appsettings.KeyScheduleSignDist])
	}

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
			if item.Key == appsettings.KeyScheduleSignDist {
				t.Fatalf("%s must not appear in the settings roster", item.Key)
			}
		}
	}
}
