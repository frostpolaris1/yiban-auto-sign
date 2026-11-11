package main

// abs R1 端到端：急停门咬住交互面与手动排期面（真库、真装配）。
//
// # 功能
// 用 assembly.Build 的真实装配与真 PostgreSQL，把 engine.paused 置 true 后：
// detect / verify / 三 refresh 端点 / plan / override 全部回 503，
// 且 plan/override 零落行、零覆盖。detect 与 verify 走 gated provider，
// 证明装配把急停门接到了账号认证与凭据校验两个消费点。
//
// # 口径
// 缺 DSN 时由 newAuthEnv 显式跳过（SKIP 不是绿）。
// 业务日按服务端默认时区 Asia/Shanghai 计算；时钟走 core/clock。

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

func TestAbsR1PausedGatesManualSurfaces(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) {
		// 容量/计划端点要求正数并发与速率；装配缺省是 0。
		cfg.Workers = 6
		cfg.AvgAttempt = 3 * time.Second
		cfg.EgressRate = 1.0
	}, nil)
	ctx := e.ctx

	hash, err := coreauth.HashPassword("master-password-9!")
	if err != nil {
		t.Fatalf("hash master password: %v", err)
	}
	var master coreuser.Record
	err = e.db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		master, txErr = e.users.Create(ctx, coreuser.New{
			Email: "abs-r1-master@example.com", PasswordHash: hash, Role: coreauth.RoleMaster,
		}, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create master: %v", err)
	}
	resp, session := e.login(master.Email, "master-password-9!")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login master = %d", resp.StatusCode)
	}
	csrf := session.CSRFToken

	school, err := pg.NewSchoolRepo(e.db).Create(ctx, coreschool.New{Code: "abs-r1", Name: "ABS R1"})
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	sched := pg.NewScheduleRepo(e.db)
	taskID, err := sched.CreateSchoolTask(ctx, school.ID, "晨签")
	if err != nil {
		t.Fatalf("create task: %v", err)
	}
	accts := pg.NewAccountRepo(e.db)
	var acct coreaccount.Record
	err = e.db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		acct, txErr = accts.Create(ctx, coreaccount.New{
			SchoolID: school.ID, OwnerUserID: master.ID, Phone: "13800138036",
		}, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create account: %v", err)
	}
	if _, err := accts.Review(ctx, corescope.Master(), acct.ID, true, "", master.ID, coreclock.System{}.Now()); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	if n := e.scalarInt("SELECT count(*) FROM accounts WHERE id = $1::uuid AND status = 'active' AND deleted_at IS NULL", acct.ID); n != 1 {
		t.Fatalf("account %s is not active/visible: n=%d", acct.ID, n)
	}
	cipher, err := appaccount.NewCipher(authTestCredentialKey)
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	sealed, err := cipher.Seal(school.ID, acct.ID, appaccount.CredentialPayload{
		Password: "platform-secret", DeviceCode: "device-code", PhoneModel: "Mi 9",
	})
	if err != nil {
		t.Fatalf("Seal credential: %v", err)
	}
	if err := accts.InsertCredential(ctx, sealed); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	// 急停：settings 行 engine.paused=true。
	if _, err := e.db.Exec(ctx,
		`INSERT INTO settings (key, value) VALUES ('engine.paused','true')
		 ON CONFLICT (key) DO UPDATE SET value = 'true'`); err != nil {
		t.Fatalf("pause engine: %v", err)
	}

	// 中国标准时间无夏令时，固定偏移与服务端 Asia/Shanghai 等价。
	loc := time.FixedZone("CST", 8*3600)
	now := coreclock.System{}.Now().In(loc)
	day := now.Format("2006-01-02")
	start := time.Date(now.Year(), now.Month(), now.Day(), 9, 0, 0, 0, loc)
	end := start.Add(30 * time.Minute)

	post := func(path string, body any) (*http.Response, []byte) {
		return e.do(http.MethodPost, path, body, map[string]string{"X-CSRF-Token": csrf})
	}
	want503 := func(name string, resp *http.Response, payload []byte) {
		t.Helper()
		if resp.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("%s = %d, want 503; body=%q", name, resp.StatusCode, payload)
		}
		var env struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
		}
		if err := json.Unmarshal(payload, &env); err != nil {
			t.Fatalf("%s decode: %v; body=%q", name, err, payload)
		}
		if env.Error.Code != "capacity_exceeded" {
			t.Fatalf("%s code = %q, want capacity_exceeded（既有闭集）", name, env.Error.Code)
		}
	}

	resp, payload := post("/api/accounts/detect",
		map[string]string{"phone": "13800138036", "password": "platform-secret"})
	want503("detect", resp, payload)

	resp, payload = post("/api/accounts/"+acct.ID+"/verify", nil)
	want503("verify", resp, payload)

	resp, payload = post("/api/schedule/windows/refresh?day="+day, nil)
	want503("windows refresh", resp, payload)

	resp, payload = post("/api/capacity/refresh?school_id="+school.ID+"&task_id="+taskID+"&day="+day, nil)
	want503("capacity refresh", resp, payload)

	resp, payload = post("/api/schedule/preview/refresh?day="+day+"&school_id="+school.ID+"&task_id="+taskID, nil)
	want503("preview refresh", resp, payload)

	resp, payload = post("/api/schedule/plan", map[string]string{
		"day": day, "school_id": school.ID, "task_id": taskID,
	})
	want503("plan", resp, payload)

	resp, payload = post("/api/schedule/override", map[string]string{
		"day": day, "school_id": school.ID, "task_id": taskID,
		"start": start.Format(time.RFC3339), "end": end.Format(time.RFC3339),
		"reason": "急停期间不得写入",
	})
	want503("override", resp, payload)

	// 急停期间零落行、零覆盖。
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks"); n != 0 {
		t.Fatalf("paused plan wrote %d task rows, want 0", n)
	}
	if n := e.scalarInt("SELECT count(*) FROM window_overrides"); n != 0 {
		t.Fatalf("paused override wrote %d rows, want 0", n)
	}
}
