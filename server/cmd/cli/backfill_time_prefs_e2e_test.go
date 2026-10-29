package main

// backfill-time-prefs 的真库 e2e（2gk 判据，派单 2026-10-29）。
//
// 模拟的真实用户动作：运维在完成 `migrate-legacy` 之后，对同一份 P8-A
// 导出目录跑一次 `yiban-cli backfill-time-prefs --dir 导出目录`，核对报告，
// 再加 `--apply` 落库。
//
// 断言的用户可见结果：
//   - dry-run 后新库 time_prefs 零行；
//   - --apply 写入旧自选值；重复执行零变更（幂等）；
//   - 同一用户多账号取值不一致时整用户跳过，报告列出掩码手机号；
//   - 坏行与四类不可解析行逐条给出原因；
//   - 写失败整批回滚，不留半写；
//   - stdout 报告零明文手机号。
//
// 夹具全合成值：手机号取 tests/synthetic-fixtures.json 登记值
// （13800000001–13800000007、13899999999）。旧库行只含补录读的列。
//
// 缺 YIBAN_TEST_DATABASE_URL 时 userTestSchema 显性 SKIP：跳过不是绿（AGENTS §30）。

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/legacy"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
)

// backfillE2EPhones 是本次 e2e 用的登记合成手机号（下标＝旧账号 id）。
var backfillE2EPhones = []string{
	"",            // 占位：旧账号 id 从 1 起
	"13800000001", // 旧账号 1：正常写入（owner A）
	"13800000002", // 旧账号 2：正常写入（owner B）
	"13800000003", // 旧账号 3：与 4 同属 owner D，取值冲突
	"13800000004", // 旧账号 4：与 3 同属 owner D，取值冲突
	"13800000005", // 旧账号 5：落地账号已软删
	"13800000006", // 旧账号 6：无 legacy_ids 映射
	"13800000007", // 旧账号 7：slot 非 5 倍数
	"13800000008", // 旧账号 8：归属用户已软删
}

// backfillE2EExport 写一份只含补录所需两表的导出目录。
func backfillE2EExport(t *testing.T, prefs []map[string]any) string {
	t.Helper()
	dir := t.TempDir()
	accounts := make([]map[string]any, 0, len(backfillE2EPhones))
	for id := 1; id < len(backfillE2EPhones); id++ {
		accounts = append(accounts, map[string]any{
			"id": legacy.JSONNumber(itoaE2E(int64(id))), "phone": backfillE2EPhones[id],
			"name": "acct-" + itoaE2E(int64(id)),
		})
	}
	for name, rows := range map[string][]map[string]any{
		"accounts.jsonl":   accounts,
		"time_prefs.jsonl": prefs,
	} {
		var sb strings.Builder
		for _, row := range rows {
			sb.WriteString(legacy.CanonicalLine(row))
			sb.WriteString("\n")
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(sb.String()), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	return dir
}

func itoaE2E(n int64) string {
	return strconv.FormatInt(n, 10)
}

// backfillE2EPrefs 造旧 time_prefs 全量夹具：2 条可写、1 组冲突、4 类坏行/不可解析行。
func backfillE2EPrefs() []map[string]any {
	row := func(phone string, slot int64) map[string]any {
		return map[string]any{
			"phone": phone, "slot_min": legacy.JSONNumber(itoaE2E(slot)),
			"updated_at": "2026-01-01T00:00:00",
		}
	}
	return []map[string]any{
		row(backfillE2EPhones[1], 30), // → owner A：30
		row(backfillE2EPhones[2], 60), // → owner B：60
		row(backfillE2EPhones[3], 15), // } owner D：冲突
		row(backfillE2EPhones[4], 20), // }
		row(backfillE2EPhones[5], 30), // 落地账号已软删
		row(backfillE2EPhones[6], 45), // 无 legacy_ids 映射
		row(backfillE2EPhones[7], 7),  // 非 5 倍数
		row(backfillE2EPhones[8], 30), // 归属用户已软删
		row("13899999999", 35),        // 旧库孤儿：无账号行
	}
}

// backfillE2ESeed 建独立 schema 并落夹具：1 校、5 用户、5 账号、4 条 legacy_ids。
// 返回 dsn 与各用户 id。
func backfillE2ESeed(t *testing.T) (string, string, string, string) {
	t.Helper()
	dsn := userTestSchema(t)
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(db.Close)

	insertUser := func(email string, deleted bool) string {
		t.Helper()
		id, err := db.QueryString(ctx,
			`INSERT INTO users (email, password_hash, role, deleted_at)
			 VALUES ($1, 'argon2id$test', 'user', CASE WHEN $2::boolean THEN now() ELSE NULL END)
			 RETURNING id::text`, email, deleted)
		if err != nil {
			t.Fatalf("create user %s: %v", email, err)
		}
		return id
	}
	ownerA := insertUser("pref-a@example.invalid", false)
	ownerB := insertUser("pref-b@example.invalid", false)
	ownerD := insertUser("pref-d@example.invalid", false)
	inactiveOwner := insertUser("pref-inactive@example.invalid", true) // owner E：已软删用户

	schoolID, err := db.QueryString(ctx,
		`INSERT INTO schools (code, name) VALUES ($1, $2) RETURNING id::text`,
		"preffix0000", "Fixture School preffix0000")
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	insertAccount := func(ownerID, phone string, deleted bool) string {
		t.Helper()
		id, err := db.QueryString(ctx,
			`INSERT INTO accounts (school_id, owner_user_id, phone, status, deleted_at)
			 VALUES ($1::uuid, $2::uuid, $3, 'active', CASE WHEN $4::boolean THEN now() ELSE NULL END)
			 RETURNING id::text`, schoolID, ownerID, phone, deleted)
		if err != nil {
			t.Fatalf("create account %s: %v", phone, err)
		}
		return id
	}
	insertLegacy := func(oldID int64, newID string) {
		t.Helper()
		if _, err := db.Exec(ctx,
			`INSERT INTO legacy_ids (old_table, old_id, new_id) VALUES ('accounts', $1, $2::uuid)`,
			oldID, newID); err != nil {
			t.Fatalf("legacy_ids %d: %v", oldID, err)
		}
	}
	insertLegacy(1, insertAccount(ownerA, backfillE2EPhones[1], false))
	insertLegacy(2, insertAccount(ownerB, backfillE2EPhones[2], false))
	insertLegacy(3, insertAccount(ownerD, backfillE2EPhones[3], false))
	insertLegacy(4, insertAccount(ownerD, backfillE2EPhones[4], false))
	// 旧账号 5：落地账号软删（inactive 档）。
	liveOwnerE := insertUser("pref-e@example.invalid", false)
	insertLegacy(5, insertAccount(liveOwnerE, backfillE2EPhones[5], true))
	// 旧账号 6：故意不登记 legacy_ids（unmapped 档）。
	// 旧账号 8：账号活着，归属用户软删（inactive 档）。
	insertLegacy(8, insertAccount(inactiveOwner, backfillE2EPhones[8], false))
	return dsn, ownerA, ownerB, ownerD
}

// backfillE2ECount 数 time_prefs 行数。
func backfillE2ECount(t *testing.T, dsn string) int64 {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	n, err := db.QueryInt64(ctx, "SELECT count(*) FROM time_prefs")
	if err != nil {
		t.Fatalf("count time_prefs: %v", err)
	}
	return n
}

// backfillE2ERun 跑一次子命令，返回退出码与 stdout/stderr。
func backfillE2ERun(t *testing.T, dsn, dir string, apply bool) (int, string, string) {
	t.Helper()
	args := []string{"--dir", dir, "--dsn", dsn}
	if apply {
		args = append(args, "--apply")
	}
	var out, errOut strings.Builder
	code := runBackfillTimePrefs(args, &out, &errOut)
	return code, out.String(), errOut.String()
}

// TestBackfillTimePrefsE2E 是主判据：dry-run 零写、apply 落值、重复执行幂等、
// 冲突跳过并列名、坏行逐条拒绝、输出零明文。
func TestBackfillTimePrefsE2E(t *testing.T) {
	dsn, ownerA, ownerB, ownerD := backfillE2ESeed(t)
	dir := backfillE2EExport(t, backfillE2EPrefs())

	// 1) dry-run：报告给出计划，库零行。
	code, out, errOut := backfillE2ERun(t, dsn, dir, false)
	if code != 0 {
		t.Fatalf("dry-run exit %d, stderr:\n%s", code, errOut)
	}
	if n := backfillE2ECount(t, dsn); n != 0 {
		t.Fatalf("dry-run 后 time_prefs = %d 行, want 0", n)
	}
	var dry legacy.BackfillReport
	if err := json.Unmarshal([]byte(out), &dry); err != nil {
		t.Fatalf("dry-run 报告不是 JSON: %v\n%s", err, out)
	}
	if dry.Apply || dry.RowsWritten != 0 {
		t.Fatalf("dry-run 报告声称已写：apply=%v rows=%d", dry.Apply, dry.RowsWritten)
	}
	if len(dry.Writes) != 2 {
		t.Fatalf("dry-run writes = %+v, want 2 条（owner A/B）", dry.Writes)
	}
	if len(dry.Conflicts) != 1 || dry.Conflicts[0].UserID != ownerD {
		t.Fatalf("dry-run conflicts = %+v, want 1 条 owner D", dry.Conflicts)
	}
	if got := dry.Conflicts[0].Offsets; len(got) != 2 || got[0] != 15 || got[1] != 20 {
		t.Fatalf("dry-run 冲突取值 = %v, want [15 20]", got)
	}
	if len(dry.Skips) != 5 {
		t.Fatalf("dry-run skips = %+v, want 5 条", dry.Skips)
	}
	for _, phone := range backfillE2EPhones[1:] {
		if strings.Contains(out, phone) {
			t.Fatalf("报告含明文手机号 %s：\n%s", phone, out)
		}
	}

	// 2) apply：两条写入，值取自旧库。
	code, out, errOut = backfillE2ERun(t, dsn, dir, true)
	if code != 0 {
		t.Fatalf("apply exit %d, stderr:\n%s", code, errOut)
	}
	var applied legacy.BackfillReport
	if err := json.Unmarshal([]byte(out), &applied); err != nil {
		t.Fatalf("apply 报告不是 JSON: %v\n%s", err, out)
	}
	if !applied.Apply || applied.RowsWritten != 2 {
		t.Fatalf("apply 报告 rows_written = %d, want 2", applied.RowsWritten)
	}
	// 冲突主线（2gk 审查 P2-3）：--apply 也不写冲突用户，
	// 报告仍列出整用户进 conflicts，writes 不含 owner D。
	if len(applied.Conflicts) != 1 || applied.Conflicts[0].UserID != ownerD {
		t.Fatalf("apply conflicts = %+v, want 1 条 owner D", applied.Conflicts)
	}
	for _, item := range applied.Writes {
		if item.UserID == ownerD {
			t.Fatalf("apply writes 含冲突用户 owner D：%+v", applied.Writes)
		}
	}
	if len(applied.Writes) != 2 {
		t.Fatalf("apply writes = %+v, want 2 条", applied.Writes)
	}
	if n := backfillE2ECount(t, dsn); n != 2 {
		t.Fatalf("apply 后 time_prefs = %d 行, want 2", n)
	}
	if got := backfillE2EOffset(t, dsn, ownerA); got != 30 {
		t.Fatalf("owner A 的 offset = %d, want 30", got)
	}
	if got := backfillE2EOffset(t, dsn, ownerB); got != 60 {
		t.Fatalf("owner B 的 offset = %d, want 60", got)
	}
	if n := backfillE2EOffsetRows(t, dsn, ownerD); n != 0 {
		t.Fatalf("冲突用户 owner D 落了 %d 行, want 0", n)
	}

	// 3) 再跑一次 --apply：零变更、不新增行（幂等）。
	code, out, errOut = backfillE2ERun(t, dsn, dir, true)
	if code != 0 {
		t.Fatalf("re-apply exit %d, stderr:\n%s", code, errOut)
	}
	var again legacy.BackfillReport
	if err := json.Unmarshal([]byte(out), &again); err != nil {
		t.Fatalf("re-apply 报告不是 JSON: %v\n%s", err, out)
	}
	if again.RowsWritten != 0 {
		t.Fatalf("re-apply rows_written = %d, want 0（幂等）", again.RowsWritten)
	}
	if n := backfillE2ECount(t, dsn); n != 2 {
		t.Fatalf("re-apply 后 time_prefs = %d 行, want 2", n)
	}
}

// backfillE2EOffset 读一个用户的 offset。
func backfillE2EOffset(t *testing.T, dsn, userID string) int64 {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	n, err := db.QueryInt64(ctx, `SELECT offset_min FROM time_prefs WHERE user_id = $1::uuid`, userID)
	if err != nil {
		t.Fatalf("read offset: %v", err)
	}
	return n
}

// backfillE2EOffsetRows 数一个用户的 time_prefs 行数。
func backfillE2EOffsetRows(t *testing.T, dsn, userID string) int64 {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	n, err := db.QueryInt64(ctx, `SELECT count(*) FROM time_prefs WHERE user_id = $1::uuid`, userID)
	if err != nil {
		t.Fatalf("count user prefs: %v", err)
	}
	return n
}

// TestBackfillTimePrefsOverwritesExistingValueE2E 钉住覆盖语义（2gk 审查 P2-1）：
// time_prefs 已有不同取值时，--apply 覆盖为新值，rows_written 计实际变更行数。
// 模拟的真实用户动作：库里已存在该用户的旧自选（如试运行或手工补录过），
// 运维重跑补录，期望新值落库。
func TestBackfillTimePrefsOverwritesExistingValueE2E(t *testing.T) {
	dsn, ownerA, ownerB, _ := backfillE2ESeed(t)
	dir := backfillE2EExport(t, backfillE2EPrefs())
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	// 预插与补录值不同的旧值：owner A 存 45，补录夹具给 30；owner B 不插，作对照。
	if _, err := db.Exec(ctx,
		`INSERT INTO time_prefs (user_id, offset_min, updated_at) VALUES ($1::uuid, 45, now())`,
		ownerA); err != nil {
		t.Fatalf("seed time_prefs: %v", err)
	}

	code, out, errOut := backfillE2ERun(t, dsn, dir, true)
	if code != 0 {
		t.Fatalf("apply exit %d, stderr:\n%s", code, errOut)
	}
	var applied legacy.BackfillReport
	if err := json.Unmarshal([]byte(out), &applied); err != nil {
		t.Fatalf("apply 报告不是 JSON: %v\n%s", err, out)
	}
	// 覆盖 owner A 计 1 + 新插 owner B 计 1 = 2。
	if applied.RowsWritten != 2 {
		t.Fatalf("rows_written = %d, want 2（覆盖 1 + 插入 1）", applied.RowsWritten)
	}
	if got := backfillE2EOffset(t, dsn, ownerA); got != 30 {
		t.Fatalf("owner A 的 offset = %d, want 30（覆盖生效）", got)
	}
	if got := backfillE2EOffset(t, dsn, ownerB); got != 60 {
		t.Fatalf("owner B 的 offset = %d, want 60", got)
	}
}

// TestBackfillTimePrefsRollsBackOnWriteFailureE2E 钉住「失败不半写」：
// 同批里一条指向不存在的用户（外键失败），先写的行必须一起消失。
func TestBackfillTimePrefsRollsBackOnWriteFailureE2E(t *testing.T) {
	dsn, ownerA, _, _ := backfillE2ESeed(t)
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()

	writer := legacy.NewTimePrefWriter(db)
	_, err = writer.UpsertOffsets(ctx, []legacy.PrefWrite{
		{UserID: ownerA, OffsetMin: 30},
		{UserID: "00000000-0000-0000-0000-000000000000", OffsetMin: 45},
	}, time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC))
	if err == nil {
		t.Fatal("写不存在的用户没有失败：外键判据或事务边界失效")
	}
	if n := backfillE2ECount(t, dsn); n != 0 {
		t.Fatalf("失败后 time_prefs = %d 行, want 0（半写）", n)
	}
}

// TestBackfillTimePrefsRequiresDir 钉住用法错误退出码 2，且不连库。
func TestBackfillTimePrefsRequiresDir(t *testing.T) {
	var out, errOut strings.Builder
	if code := runBackfillTimePrefs(nil, &out, &errOut); code != 2 {
		t.Fatalf("缺 --dir code = %d, want 2", code)
	}
	if !strings.Contains(errOut.String(), "--dir") {
		t.Fatalf("stderr 未点名 --dir：%s", errOut.String())
	}
}

// TestBackfillTimePrefsSingleErrorPrefix 钉住 stderr 前缀单次：
// 致命错误由 CLI 统一加前缀，库层文本不带前缀，叠加即缺陷（2gk 审查 P3）。
// 用缺 --dir 的路径触发；该路径不连库。
func TestPrintBackfillErrorSinglePrefix(t *testing.T) {
	// 判据：前缀只在 printBackfillError 加一次。Msg 自带前缀＝缺陷复现，
	// 计数必须变 2（见 2gk 审查 M5：旧用例走 --dir 缺失分支，恒真无牙）。
	cases := []struct {
		name string
		err  error
		code int
	}{
		{"legacy fatal", &legacy.Error{Code: 2, Msg: "read export: boom"}, 2},
		{"ordinary", errors.New("boom"), 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var buf strings.Builder
			if got := printBackfillError(&buf, tc.err); got != tc.code {
				t.Fatalf("code = %d, want %d", got, tc.code)
			}
			if n := strings.Count(buf.String(), "backfill-time-prefs:"); n != 1 {
				t.Fatalf("stderr 前缀出现 %d 次, want 1：%s", n, buf.String())
			}
		})
	}
}

func TestPrintBackfillErrorRejectsDoublePrefix(t *testing.T) {
	// 突变守卫：Msg 自带前缀时计数必须是 2——证明上一条测试有判别力。
	var buf strings.Builder
	code := printBackfillError(&buf, &legacy.Error{Code: 2, Msg: "backfill-time-prefs: read export: boom"})
	if code != 2 {
		t.Fatalf("code = %d, want 2", code)
	}
	if n := strings.Count(buf.String(), "backfill-time-prefs:"); n != 2 {
		t.Fatalf("自带前缀的 Msg 应产生 2 次前缀（证明判据有牙），实得 %d：%s", n, buf.String())
	}
}
