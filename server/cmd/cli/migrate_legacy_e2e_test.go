package main

// migrate-legacy 的真库 e2e。
//
// 模拟的真实用户动作：运维在迁移窗口对旧栈导出物目录跑一次
// `yiban-cli migrate-legacy --dir 导出目录 --school-id … --fallback-owner-user-id …`。
// 断言的用户可见结果：每条账号落到自己的 owner、旧 admin 带院校绑定、
// 备注名入库、fallback 只兜底指定账号、重跑零新增、孤儿按档位处理、
// 旧口令能登录且透明升级、错钥显性红、必填旗缺失红。
//
// 夹具全合成值：手机号取 tests/synthetic-fixtures.json 登记值
// （13800000001–13800000005、13899999999）；信封十六进制为随机测试段；
// 旧口令哈希为 werkzeug scrypt 形状（口令 pw-fixture-0000，合成盐）。
//
// 缺 YIBAN_TEST_DATABASE_URL 时显性 SKIP：跳过不是绿。

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/legacy"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

const (
	e2eLegacyKeyHex = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
	// werkzeug scrypt 形状的旧口令哈希（口令 pw-fixture-0000，N=32768 r=8 p=1，
	// 盐 0123456789abcdef，dklen=64；2026-10-27 由 Python hashlib 现算）。
	e2eScryptHash = "scrypt:32768:8:1$0123456789abcdef$9b9e287f0e026b4d76b48016ec8706d7a64ff1609eab098ee2a7bd1fa187af5908b54a1578b206483511c7d7225a838e51aad45e91e96c4f361b1682db887dd3"
	e2ePlainPW    = "pw-fixture-0000"
	e2eCredPW     = "pw-fixture-0001"
	e2ePepper     = "0123456789abcdef0123456789abcdef"

	e2eSchoolCode      = "migrfix0000"
	e2eAdminSchoolCode = "migrfix0001"
	e2eMasterEmail     = "owner-fixture-0000@example.invalid"
	e2eAdminEmail      = "user0000@example.invalid"
	e2eUserEmail1      = "user0001@example.invalid"
	e2eUserEmail2      = "user0002@example.invalid"
	// 旧内置 master 的 owner 形状：不在旧 users 表，必须走 fallback。
	e2eOwnerMissing = "legacy-master"
)

var (
	e2ePhoneA      = "13800000001" // 完整邮箱 owner，旧 admin 名下
	e2ePhoneB      = "13800000002" // @ 前缀 owner，user_paused
	e2ePhoneC      = "13800000003" // owner 对不上，active → fallback；空备注名
	e2ePhoneD      = "13800000004" // 完整邮箱 owner，软删
	e2ePhoneE      = "13800000005" // 完整邮箱 owner，rejected
	e2ePhoneOrphan = "13899999999" // time_prefs 孤儿
	// 旧库机型没有长度约束：导入时必须按 rune 截到契约的 64。
	e2eLongPhoneModel      = strings.Repeat("机", 70)
	e2eTruncatedPhoneModel = strings.Repeat("机", 64)
)

// e2eNewKeyB64 是新栈凭据 KEK（32 字节合成值的 base64）。
var e2eNewKeyB64 = base64.StdEncoding.EncodeToString([]byte{
	0xa5, 0x1b, 0x7c, 0x3e, 0x9f, 0x20, 0x84, 0xd6, 0x0e, 0x53, 0xbb, 0x71, 0xc8, 0x2a, 0x6f, 0x14,
	0x3d, 0x95, 0xe0, 0x47, 0x8b, 0x12, 0xf6, 0x59, 0xcd, 0x30, 0xa8, 0x7e, 0x61, 0x09, 0xdf, 0x4c,
})

// e2eSealOld 用旧口径构造测试信封（AAD=手机号，ct/tag 分离）。
func e2eSealOld(t *testing.T, key []byte, phone, plaintext string) map[string]any {
	t.Helper()
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatalf("aes: %v", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatalf("gcm: %v", err)
	}
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		t.Fatalf("nonce: %v", err)
	}
	out := gcm.Seal(nil, nonce, []byte(plaintext), []byte(phone))
	return map[string]any{
		"v":     json.Number("2"),
		"kid":   legacy.LegacyKID(key),
		"nonce": hex.EncodeToString(nonce),
		"ct":    hex.EncodeToString(out[:len(out)-16]),
		"tag":   hex.EncodeToString(out[len(out)-16:]),
	}
}

// e2eUserRow 造一行旧 users：键序由 e2eWriteExport 的 CanonicalLine 统一排序。
func e2eUserRow(id int, email, role string, deleted, mailNotify int) map[string]any {
	return map[string]any{
		"id": json.Number(fmt.Sprint(id)), "email": email, "password_hash": e2eScryptHash,
		"role": role, "created_at": "2026-01-01T00:00:00", "pw_version": json.Number("1"),
		"deleted": json.Number(fmt.Sprint(deleted)), "deleted_at": "", "mail_notify": json.Number(fmt.Sprint(mailNotify)),
	}
}

// e2eAccountRow 造一行旧 accounts；password 留空串或旧信封对象。
func e2eAccountRow(id int, name, phone, owner, status, rejectReason string, password any, deleted, userPaused int) map[string]any {
	return map[string]any{
		"id": json.Number(fmt.Sprint(id)), "sort_order": json.Number(fmt.Sprint(id - 1)),
		"name": name, "phone": phone, "password": password, "phone_model": "",
		"phone_code": "", "owner": owner, "status": status, "reject_reason": rejectReason,
		"deleted": json.Number(fmt.Sprint(deleted)), "deleted_at": "", "deleted_by": "",
		"user_paused": json.Number(fmt.Sprint(userPaused)),
	}
}

// e2eExport 是一份合成导出物的四张业务表。
type e2eExport struct {
	Users    []map[string]any
	Accounts []map[string]any
	Prefs    []map[string]any
	Audits   []map[string]any
}

// e2eFixture 是默认多 owner 夹具：3 个旧用户（1 admin、2 user）、
// 5 个账号（完整邮箱、@ 前缀、fallback、软删、rejected），
// 1 条空备注名、1 条孤儿自选、2 条旧审计。
func e2eFixture(t *testing.T) e2eExport {
	t.Helper()
	legacyKey, _ := hex.DecodeString(e2eLegacyKeyHex)
	envA := e2eSealOld(t, legacyKey, e2ePhoneA, e2eCredPW)
	envB := e2eSealOld(t, legacyKey, e2ePhoneB, e2eCredPW)
	envD := e2eSealOld(t, legacyKey, e2ePhoneD, e2eCredPW)
	envCode := e2eSealOld(t, legacyKey, e2ePhoneD, "code-fixture-0001")

	users := []map[string]any{
		e2eUserRow(1, e2eAdminEmail, "admin", 0, 1),
		e2eUserRow(2, e2eUserEmail1, "user", 0, 0),
		e2eUserRow(3, e2eUserEmail2, "user", 0, 1),
	}
	acctA := e2eAccountRow(1, "acct-a", e2ePhoneA, "  "+strings.ToUpper(e2eAdminEmail)+"  ",
		"active", "", envA, 0, 0)
	acctA["phone_model"] = e2eLongPhoneModel
	acctB := e2eAccountRow(2, "acct-b", e2ePhoneB, "user0001", "active", "", envB, 0, 1)
	acctC := e2eAccountRow(3, "", e2ePhoneC, e2eOwnerMissing, "active", "", "", 0, 0)
	acctD := e2eAccountRow(4, "acct-d", e2ePhoneD, e2eUserEmail2, "active", "", envD, 1, 0)
	acctD["phone_code"] = envCode
	acctD["deleted_at"] = "2026-01-02T00:00:00"
	acctD["deleted_by"] = "admin"
	acctE := e2eAccountRow(5, "acct-e", e2ePhoneE, " "+e2eUserEmail1+" ", "rejected", "fixture-reason-0000", "", 0, 0)

	prefs := []map[string]any{
		{"phone": e2ePhoneA, "slot_min": json.Number("30"), "updated_at": "2026-01-01T00:00:00"},
		{"phone": e2ePhoneOrphan, "slot_min": json.Number("45"), "updated_at": "2026-01-01T00:01:00"},
	}
	audits := []map[string]any{
		{"id": json.Number("1"), "ts": "2026-01-01T00:00:00", "username": "ops",
			"action": "export-fixture", "target": "fixture-hash", "detail": "fixture"},
		{"id": json.Number("2"), "ts": "2026-01-01T00:01:00", "username": "ops",
			"action": "time_pref_set", "target": "fixture-hash", "detail": "fixture"},
	}
	return e2eExport{Users: users, Accounts: []map[string]any{acctA, acctB, acctC, acctD, acctE}, Prefs: prefs, Audits: audits}
}

// e2eWriteExport 把一份导出物写进 root/export，返回目录。
// 行键序与导出器一致（jsonl 每行键排序），对账表同口径。
func e2eWriteExport(t *testing.T, root string, ex e2eExport) string {
	t.Helper()
	dir := filepath.Join(root, "export")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	files := map[string][]map[string]any{
		"users.jsonl": ex.Users, "schools.jsonl": {}, "accounts.jsonl": ex.Accounts,
		"time_prefs.jsonl": ex.Prefs, "audit.jsonl": ex.Audits,
	}
	for name, tableRows := range files {
		var sb strings.Builder
		for _, row := range tableRows {
			sb.WriteString(legacy.CanonicalLine(row))
			sb.WriteString("\n")
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(sb.String()), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	var recon strings.Builder
	for _, table := range []string{"users", "accounts", "time_prefs", "audit"} {
		keyField := "id"
		if table == "time_prefs" {
			keyField = "phone"
		}
		rows := files[table+".jsonl"]
		for _, row := range rows {
			key := fmt.Sprint(row[keyField])
			if keyField == "phone" {
				key = coremask.MaskPhone(key)
			}
			recon.WriteString(legacy.CanonicalLine(map[string]any{
				"table": table, "key": key, "hash": legacy.CanonicalHash(table, key, row),
			}))
			recon.WriteString("\n")
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "reconciliation.jsonl"), []byte(recon.String()), 0o600); err != nil {
		t.Fatalf("write recon: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "summary.json"), []byte("{}\n"), 0o600); err != nil {
		t.Fatalf("write summary: %v", err)
	}
	return dir
}

// e2eEnv 是一次真库夹具：独立 schema、目标学校、admin 绑定学校、fallback master。
type e2eEnv struct {
	dsn           string
	schoolID      string
	adminSchoolID string
	fallbackID    string
}

// e2eSetupDB 建独立 schema，迁移到最新（含 legacy_ids 与 accounts.name），
// 建两所学校与 fallback master。DSN 读取与 SKIP 语义都在 userTestSchema。
func e2eSetupDB(t *testing.T) e2eEnv {
	t.Helper()
	dsn := userTestSchema(t)
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(db.Close)
	env := e2eEnv{dsn: dsn}
	if env.schoolID, err = db.QueryString(ctx,
		`INSERT INTO schools (code, name) VALUES ($1, $2) RETURNING id::text`,
		e2eSchoolCode, "Fixture School "+e2eSchoolCode); err != nil {
		t.Fatalf("create school: %v", err)
	}
	if env.adminSchoolID, err = db.QueryString(ctx,
		`INSERT INTO schools (code, name) VALUES ($1, $2) RETURNING id::text`,
		e2eAdminSchoolCode, "Fixture School "+e2eAdminSchoolCode); err != nil {
		t.Fatalf("create admin school: %v", err)
	}
	if env.fallbackID, err = db.QueryString(ctx,
		`INSERT INTO users (email, password_hash, role) VALUES ($1, 'argon2id$test', 'master') RETURNING id::text`,
		e2eMasterEmail); err != nil {
		t.Fatalf("create fallback owner: %v", err)
	}
	return env
}

// e2eArgs 是一次 migrate-legacy 调用的可调项。
// fallbackID/adminSchoolID 为空表示不传该旗，schoolID 为空表示用夹具学校，
// 用来覆盖必填与缺省行为。
type e2eArgs struct {
	fallbackID    string
	adminSchoolID string
	schoolID      string
	legacyKey     string
	strict        bool
}

func (e e2eEnv) defaults() e2eArgs {
	return e2eArgs{fallbackID: e.fallbackID, legacyKey: e2eLegacyKeyHex}
}

// e2eRun 跑一次 migrate-legacy，返回退出码与合并输出。
// 旧钥只走环境变量 YIBAN_LEGACY_ACCOUNTS_KEY（argv 不传钥）。
func e2eRun(t *testing.T, env e2eEnv, dir, out string, a e2eArgs) (int, string) {
	t.Helper()
	t.Setenv("YIBAN_CREDENTIAL_KEY", e2eNewKeyB64)
	if a.legacyKey != "" {
		t.Setenv("YIBAN_LEGACY_ACCOUNTS_KEY", a.legacyKey)
	}
	schoolID := a.schoolID
	if schoolID == "" {
		schoolID = env.schoolID
	}
	args := []string{"--dir", dir, "--out", out, "--dsn", env.dsn, "--school-id", schoolID}
	if a.fallbackID != "" {
		args = append(args, "--fallback-owner-user-id", a.fallbackID)
	}
	if a.adminSchoolID != "" {
		args = append(args, "--admin-school-id", a.adminSchoolID)
	}
	if a.strict {
		args = append(args, "--strict")
	}
	var sb strings.Builder
	code := runMigrateLegacy(args, &sb, &sb)
	return code, sb.String()
}

func e2eCount(t *testing.T, dsn, sql string, args ...any) int64 {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	n, err := db.QueryInt64(ctx, sql, args...)
	if err != nil {
		t.Fatalf("count %s: %v", sql, err)
	}
	return n
}

func e2eQueryString(t *testing.T, dsn, sql string, args ...any) string {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	value, err := db.QueryString(ctx, sql, args...)
	if err != nil {
		t.Fatalf("query %s: %v", sql, err)
	}
	return value
}

// e2eExec 执行一条写语句（模拟 purge 用）。
func e2eExec(t *testing.T, dsn, sql string, args ...any) {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec(ctx, sql, args...); err != nil {
		t.Fatalf("exec %s: %v", sql, err)
	}
}

func e2eLegacyNewID(t *testing.T, dsn, table string, oldID int) string {
	t.Helper()
	return e2eQueryString(t, dsn,
		`SELECT new_id::text FROM legacy_ids WHERE old_table = $1 AND old_id = $2`, table, oldID)
}

func e2eSummary(t *testing.T, out string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(out, "summary.json"))
	if err != nil {
		t.Fatalf("read summary: %v", err)
	}
	var summary map[string]any
	if err := json.Unmarshal(raw, &summary); err != nil {
		t.Fatalf("parse summary: %v", err)
	}
	return summary
}

// e2eAssertLegacyInvariant 断言不变式：任何落地行必有 legacy_ids 映射。
// fallback master 与学校是迁移前置件，不经导入器，不在断言内。
func e2eAssertLegacyInvariant(t *testing.T, dsn string) {
	t.Helper()
	if n := e2eCount(t, dsn,
		`SELECT count(*) FROM users u WHERE u.email <> $1 AND NOT EXISTS
		 (SELECT 1 FROM legacy_ids l WHERE l.old_table = 'users' AND l.new_id = u.id)`, e2eMasterEmail); n != 0 {
		t.Fatalf("%d landed users have no legacy_ids mapping", n)
	}
	if n := e2eCount(t, dsn,
		`SELECT count(*) FROM accounts a WHERE NOT EXISTS
		 (SELECT 1 FROM legacy_ids l WHERE l.old_table = 'accounts' AND l.new_id = a.id)`); n != 0 {
		t.Fatalf("%d landed accounts have no legacy_ids mapping", n)
	}
}

// TestNewCipherAcceptsEmptyDeviceCode 钉住 cipher 解码侧的裁决前提：
// CredentialPayload 三键全部出现、device_code 为空串仍可解回。
// 该前提不成立时导入器的凭据重加密方案整体失效。
// 放在 cmd/cli：boundary 门禁止 adapters 导入 app。
func TestNewCipherAcceptsEmptyDeviceCode(t *testing.T) {
	newKey := bytes.Repeat([]byte{0x42}, 32)
	cipher, err := appaccount.NewCipher(newKey)
	if err != nil {
		t.Fatalf("new cipher: %v", err)
	}
	cred, err := cipher.Seal("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002",
		appaccount.CredentialPayload{DeviceCode: "", Password: "pw-fixture-0001", PhoneModel: "model-0000"})
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	payload, err := cipher.Open(cred)
	if err != nil {
		t.Fatalf("open with empty device_code: %v", err)
	}
	if payload.Password != "pw-fixture-0001" || payload.DeviceCode != "" || payload.PhoneModel != "model-0000" {
		t.Fatalf("payload drifted: %+v", payload)
	}
}

// TestMigrateLegacyMultiOwnerImportE2E 是本批主判据：
// 多 owner 逐行解析、fallback 只落指定账号、旧 admin→admin 且写院校绑定、
// 备注名入库、状态/凭据/邮件开关/对账全部保持。
func TestMigrateLegacyMultiOwnerImportE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	out := filepath.Join(root, "report")
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID

	code, output := e2eRun(t, env, dir, out, args)
	if code != 0 {
		t.Fatalf("exit %d, output:\n%s", code, output)
	}

	// 行数：users 3 导入＋1 fallback master；accounts 5；凭据 3；映射 8。
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 4 {
		t.Fatalf("users = %d, want 4 (3 imported + fallback master)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 5 {
		t.Fatalf("accounts = %d, want 5", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM credentials"); n != 3 {
		t.Fatalf("credentials = %d, want 3", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 8 {
		t.Fatalf("legacy_ids = %d, want 8", n)
	}
	e2eAssertLegacyInvariant(t, env.dsn)

	// 每账号 owner_user_id：完整邮箱、@ 前缀、fallback 各走各的路。
	adminNew := e2eLegacyNewID(t, env.dsn, "users", 1)
	user1New := e2eLegacyNewID(t, env.dsn, "users", 2)
	user2New := e2eLegacyNewID(t, env.dsn, "users", 3)
	for _, tc := range []struct {
		phone string
		want  string
	}{
		{e2ePhoneA, adminNew}, {e2ePhoneB, user1New}, {e2ePhoneC, env.fallbackID},
		{e2ePhoneD, user2New}, {e2ePhoneE, user1New},
	} {
		got := e2eQueryString(t, env.dsn,
			`SELECT owner_user_id::text FROM accounts WHERE phone = $1`, tc.phone)
		if got != tc.want {
			t.Fatalf("owner of %s = %s, want %s", coremask.MaskPhone(tc.phone), got, tc.want)
		}
	}
	// fallback 只落指定账号，不是「全部账号」。
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE owner_user_id = $1::uuid", env.fallbackID); n != 1 {
		t.Fatalf("fallback-owned accounts = %d, want 1", n)
	}

	// 角色：旧 admin→admin，旧 user→user；admin 绑定写在 --admin-school-id。
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users WHERE id = $1::uuid AND role = 'admin'", adminNew); n != 1 {
		t.Fatalf("imported admin row = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users WHERE role = 'admin'"); n != 1 {
		t.Fatalf("admin rows = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users WHERE role = 'user'"); n != 2 {
		t.Fatalf("user rows = %d, want 2", n)
	}
	if n := e2eCount(t, env.dsn,
		"SELECT count(*) FROM admin_schools WHERE user_id = $1::uuid AND school_id = $2::uuid",
		adminNew, env.adminSchoolID); n != 1 {
		t.Fatalf("admin_schools binding = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools"); n != 1 {
		t.Fatalf("admin_schools rows = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools WHERE school_id = $1::uuid", env.schoolID); n != 0 {
		t.Fatalf("admin binding must use --admin-school-id, got %d rows on --school-id", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE school_id = $1::uuid", env.schoolID); n != 5 {
		t.Fatalf("accounts on --school-id = %d, want 5", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE school_id = $1::uuid", env.adminSchoolID); n != 0 {
		t.Fatalf("accounts must not use --admin-school-id, got %d", n)
	}

	// 备注名入库：空串照常，不是 NULL 也不是派生名。
	for _, tc := range []struct{ phone, name string }{
		{e2ePhoneA, "acct-a"}, {e2ePhoneB, "acct-b"}, {e2ePhoneC, ""},
		{e2ePhoneD, "acct-d"}, {e2ePhoneE, "acct-e"},
	} {
		if n := e2eCount(t, env.dsn,
			`SELECT count(*) FROM accounts WHERE phone = $1 AND name = $2`, tc.phone, tc.name); n != 1 {
			t.Fatalf("name of %s = %q not imported", coremask.MaskPhone(tc.phone), tc.name)
		}
	}

	// 状态映射原样保留：active/软删/rejected/paused_user。
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE status = 'active' AND deleted_at IS NULL"); n != 2 {
		t.Fatalf("live active accounts = %d, want 2", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE deleted_at IS NOT NULL"); n != 1 {
		t.Fatalf("soft-deleted accounts = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn,
		"SELECT count(*) FROM accounts WHERE status = 'rejected' AND reject_reason = 'fixture-reason-0000'"); n != 1 {
		t.Fatalf("rejected accounts = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE status = 'paused_user'"); n != 1 {
		t.Fatalf("paused_user accounts = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users WHERE email = $1 AND mail_notify = false", e2eUserEmail1); n != 1 {
		t.Fatalf("imported mail_notify=false rows = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users WHERE email = $1 AND mail_notify = true", e2eAdminEmail); n != 1 {
		t.Fatalf("imported mail_notify=true rows = %d, want 1", n)
	}

	// 凭据重封装可用新钥解回，device_code 为空串（解码前提）。
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: env.dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	newKey, err := base64.StdEncoding.DecodeString(e2eNewKeyB64)
	if err != nil {
		t.Fatalf("new key: %v", err)
	}
	cipher, err := appaccount.NewCipher(newKey)
	if err != nil {
		t.Fatalf("cipher: %v", err)
	}
	joined, err := db.QueryString(ctx,
		`SELECT c.kid || '|' || encode(c.nonce, 'hex') || '|' || encode(c.ciphertext, 'hex')
		     || '|' || a.school_id::text || '|' || a.id::text
		 FROM credentials c JOIN accounts a ON a.id = c.account_id
		 WHERE a.phone = $1`, e2ePhoneA)
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	parts := strings.Split(joined, "|")
	if len(parts) != 5 {
		t.Fatalf("credential row shape: %q", joined)
	}
	nonce, _ := hex.DecodeString(parts[1])
	ct, _ := hex.DecodeString(parts[2])
	payload, err := cipher.Open(appaccount.Credential{
		AccountID: parts[4], SchoolID: parts[3], KID: parts[0],
		AADVersion: 1, Nonce: nonce, Ciphertext: ct, Version: 1,
	})
	if err != nil {
		t.Fatalf("open re-sealed credential: %v", err)
	}
	if payload.Password != e2eCredPW || payload.DeviceCode != "" || payload.PhoneModel != e2eTruncatedPhoneModel {
		t.Fatalf("payload drifted: %+v", payload)
	}

	// 对账表与导出物逐字节一致（同序同哈希）。
	exportRecon, err := os.ReadFile(filepath.Join(dir, "reconciliation.jsonl"))
	if err != nil {
		t.Fatalf("read export recon: %v", err)
	}
	gotRecon, err := os.ReadFile(filepath.Join(out, "reconciliation.jsonl"))
	if err != nil {
		t.Fatalf("read import recon: %v", err)
	}
	if string(exportRecon) != string(gotRecon) {
		t.Fatalf("reconciliation drift:\nexport:\n%s\nimport:\n%s", exportRecon, gotRecon)
	}

	summary := e2eSummary(t, out)
	if got := summary["orphan_time_prefs"].(float64); got != 1 {
		t.Fatalf("orphan count = %v, want 1", summary["orphan_time_prefs"])
	}
	rowsImported := summary["rows_imported"].(map[string]any)
	if rowsImported["users"].(float64) != 3 || rowsImported["accounts"].(float64) != 5 {
		t.Fatalf("rows_imported drifted: %v", rowsImported)
	}
	owners := summary["owner_resolution"].(map[string]any)
	if owners["exact_email"].(float64) != 3 || owners["email_prefix"].(float64) != 1 ||
		owners["fallback"].(float64) != 1 || owners["unresolved"].(float64) != 0 ||
		owners["ambiguous"].(float64) != 0 {
		t.Fatalf("owner resolution drifted: %v", owners)
	}
	if summary["fallback_owner_rows"].(float64) != 1 {
		t.Fatalf("fallback_owner_rows = %v, want 1", summary["fallback_owner_rows"])
	}
	adminSchools := summary["admin_schools"].(map[string]any)
	if adminSchools["bound"].(float64) != 1 || adminSchools["created"].(float64) != 1 {
		t.Fatalf("admin_schools summary drifted: %v", adminSchools)
	}
	roles := summary["role_mapping"].(map[string]any)
	if roles["admin->admin"].(float64) != 1 || roles["user->user"].(float64) != 2 {
		t.Fatalf("role mapping drifted: %v", roles)
	}
	creds := summary["credentials"].(map[string]any)
	if creds["created"].(float64) != 3 || creds["missing"].(float64) != 2 || creds["phone_code_dropped"].(float64) != 1 {
		t.Fatalf("credential counts drifted: %v", creds)
	}
	if summary["time_prefs_imported"].(float64) != 0 {
		t.Fatalf("time_prefs must not be imported, got %v", summary["time_prefs_imported"])
	}
	columns := summary["columns_dropped"].(string)
	if !strings.Contains(columns, "sort_order") || strings.Contains(columns, "name") {
		t.Fatalf("columns_dropped must only name sort_order, got %q", columns)
	}

	// fallback 逐条报告：掩码手机号可见，owner 原文与完整邮箱零出现。
	if !strings.Contains(output, "fallback owner applied to account 3 (138****0003)") {
		t.Fatalf("fallback row must be reported per account, got:\n%s", output)
	}
	if !strings.Contains(output, "138****0003") {
		t.Fatalf("fallback report must carry the masked phone, got:\n%s", output)
	}

	// 报告与输出零明文/零 PII：完整邮箱、owner 原文、姓名、手机号、口令。
	forbidden := []string{
		e2eAdminEmail, e2eUserEmail1, e2eUserEmail2, e2eOwnerMissing,
		"acct-a", "acct-b", "acct-d", "acct-e",
		e2ePhoneA, e2ePhoneB, e2ePhoneC, e2ePhoneD, e2ePhoneE,
		e2eCredPW, e2ePlainPW,
	}
	for _, name := range []string{"reconciliation.jsonl", "summary.json"} {
		blob, err := os.ReadFile(filepath.Join(out, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		for _, secret := range forbidden {
			if strings.Contains(string(blob), secret) {
				t.Fatalf("%q leaked into %s", secret, name)
			}
		}
	}
	for _, secret := range forbidden {
		if strings.Contains(output, secret) {
			t.Fatalf("%q leaked into command output", secret)
		}
	}
}

// TestMigrateLegacyIdempotentE2E 判据：第二遍 0 新增，legacy_ids 全命中，
// admin_schools 重跑不重复，对账表与第一遍逐字节一致。
func TestMigrateLegacyIdempotentE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	out1 := filepath.Join(root, "report1")
	out2 := filepath.Join(root, "report2")
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID

	if code, output := e2eRun(t, env, dir, out1, args); code != 0 {
		t.Fatalf("first run exit %d:\n%s", code, output)
	}
	code, output := e2eRun(t, env, dir, out2, args)
	if code != 0 {
		t.Fatalf("second run exit %d:\n%s", code, output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 4 {
		t.Fatalf("users after rerun = %d, want 4", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 5 {
		t.Fatalf("accounts after rerun = %d, want 5", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 8 {
		t.Fatalf("legacy_ids after rerun = %d, want 8", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools"); n != 1 {
		t.Fatalf("admin_schools after rerun = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts WHERE owner_user_id = $1::uuid", env.fallbackID); n != 1 {
		t.Fatalf("fallback-owned accounts after rerun = %d, want 1", n)
	}
	summary := e2eSummary(t, out2)
	already := summary["already_imported"].(map[string]any)
	if already["users"].(float64) != 3 || already["accounts"].(float64) != 5 {
		t.Fatalf("already_imported drifted: %v", already)
	}
	adminSchools := summary["admin_schools"].(map[string]any)
	if adminSchools["bound"].(float64) != 1 || adminSchools["created"].(float64) != 0 {
		t.Fatalf("admin_schools rerun must be a no-op, got %v", adminSchools)
	}
	first, _ := os.ReadFile(filepath.Join(out1, "reconciliation.jsonl"))
	second, _ := os.ReadFile(filepath.Join(out2, "reconciliation.jsonl"))
	if string(first) != string(second) {
		t.Fatal("second-run reconciliation differs from first")
	}
}

// TestMigrateLegacyStrictOrphanE2E：--strict 在孤儿处失败且零写入。
func TestMigrateLegacyStrictOrphanE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	out := filepath.Join(root, "report")
	args := env.defaults()
	args.strict = true

	code, output := e2eRun(t, env, dir, out, args)
	if code != 1 {
		t.Fatalf("strict exit = %d, want 1:\n%s", code, output)
	}
	if !strings.Contains(output, "orphan") {
		t.Fatalf("output must name the orphan, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 1 {
		t.Fatalf("strict run must write no users, got %d (fallback master only)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("strict run must write no accounts, got %d", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools"); n != 0 {
		t.Fatalf("strict run must write no admin_schools, got %d", n)
	}
}

// TestMigrateLegacyDecryptWithNewKeyFailsLoudE2E：
// 旧密文用新 KEK 当旧钥解 → 显性报错（退出码 1＋计数），不静默。
func TestMigrateLegacyDecryptWithNewKeyFailsLoudE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	out := filepath.Join(root, "report")
	newKeyRaw, _ := base64.StdEncoding.DecodeString(e2eNewKeyB64)
	args := env.defaults()
	args.legacyKey = hex.EncodeToString(newKeyRaw)

	code, output := e2eRun(t, env, dir, out, args)
	if code != 1 {
		t.Fatalf("exit = %d, want 1:\n%s", code, output)
	}
	if !strings.Contains(output, "decrypt") {
		t.Fatalf("output must name decrypt failures, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 5 {
		t.Fatalf("accounts = %d, want 5 (created despite decrypt failure)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM credentials"); n != 0 {
		t.Fatalf("credentials = %d, want 0", n)
	}
	summary := e2eSummary(t, out)
	creds := summary["credentials"].(map[string]any)
	if creds["decrypt_failed"].(float64) != 3 {
		t.Fatalf("decrypt_failed = %v, want 3", creds["decrypt_failed"])
	}
}

// TestMigrateLegacyMissingConfigE2E：
// --school-id / --fallback-owner-user-id 缺失、不存在或已删都红；
// 对账表被篡改也红。
func TestMigrateLegacyMissingConfigE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	missingID := "00000000-0000-0000-0000-000000000000"

	schoolMissing := env.defaults()
	schoolMissing.schoolID = missingID
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r1"), schoolMissing); code != 2 {
		t.Fatalf("missing school exit = %d, want 2:\n%s", code, output)
	}

	noFlag := env.defaults()
	noFlag.fallbackID = ""
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r2"), noFlag); code != 2 {
		t.Fatalf("missing fallback flag exit = %d, want 2:\n%s", code, output)
	}

	fallbackMissing := env.defaults()
	fallbackMissing.fallbackID = missingID
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r3"), fallbackMissing); code != 2 {
		t.Fatalf("missing fallback user exit = %d, want 2:\n%s", code, output)
	}

	// 导出物对账表被篡改 → 导入拒绝（导出物完整性门）。
	tampered := e2eWriteExport(t, filepath.Join(root, "t2"), e2eFixture(t))
	reconPath := filepath.Join(tampered, "reconciliation.jsonl")
	raw, err := os.ReadFile(reconPath)
	if err != nil {
		t.Fatalf("read recon: %v", err)
	}
	bad := strings.Replace(string(raw), "138****0001", "138****0006", 1)
	if bad == string(raw) {
		t.Fatal("tamper no-op; fixture drift")
	}
	if err := os.WriteFile(reconPath, []byte(bad), 0o600); err != nil {
		t.Fatalf("write tampered recon: %v", err)
	}
	if code, output := e2eRun(t, env, tampered, filepath.Join(root, "r4"), env.defaults()); code != 2 {
		t.Fatalf("tampered recon exit = %d, want 2:\n%s", code, output)
	}

	// fallback 用户已软删 → exit 2，不接受已删用户兜底。
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: env.dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := db.Exec(ctx, `UPDATE users SET deleted_at = now() WHERE id = $1::uuid`, env.fallbackID); err != nil {
		t.Fatalf("soft delete fallback: %v", err)
	}
	db.Close()
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r5"), env.defaults()); code != 2 {
		t.Fatalf("deleted fallback user exit = %d, want 2:\n%s", code, output)
	}

	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("config failures must write no accounts, got %d", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 0 {
		t.Fatalf("config failures must write no legacy_ids, got %d", n)
	}
}

// TestMigrateLegacyMissingAdminSchoolE2E：--admin-school-id 指向不存在的学校
// → exit 2 零写入，错误点名该学校。
func TestMigrateLegacyMissingAdminSchoolE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	missingID := "00000000-0000-0000-0000-000000000000"
	args := env.defaults()
	args.adminSchoolID = missingID

	code, output := e2eRun(t, env, dir, filepath.Join(root, "report"), args)
	if code != 2 {
		t.Fatalf("missing admin school exit = %d, want 2:\n%s", code, output)
	}
	if !strings.Contains(output, "admin school "+missingID+" does not exist") {
		t.Fatalf("output must name the missing admin school, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 1 {
		t.Fatalf("config failure must write no users, got %d (fallback master only)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("config failure must write no accounts, got %d", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 0 {
		t.Fatalf("config failure must write no legacy_ids, got %d", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools"); n != 0 {
		t.Fatalf("config failure must write no admin_schools, got %d", n)
	}
}

// TestMigrateLegacyAdminSchoolDefaultE2E：--admin-school-id 缺省时绑定 --school-id。
func TestMigrateLegacyAdminSchoolDefaultE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	out := filepath.Join(root, "report")

	if code, output := e2eRun(t, env, dir, out, env.defaults()); code != 0 {
		t.Fatalf("exit %d:\n%s", code, output)
	}
	adminNew := e2eLegacyNewID(t, env.dsn, "users", 1)
	if n := e2eCount(t, env.dsn,
		"SELECT count(*) FROM admin_schools WHERE user_id = $1::uuid AND school_id = $2::uuid",
		adminNew, env.schoolID); n != 1 {
		t.Fatalf("default admin school binding = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools WHERE school_id = $1::uuid", env.adminSchoolID); n != 0 {
		t.Fatalf("default must not use the other school, got %d rows", n)
	}
}

// TestMigrateLegacyLoginOldPasswordE2E：
// 导入后旧口令登录成功，且登录路径当场把 scrypt 透明升级为 argon2id。
func TestMigrateLegacyLoginOldPasswordE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	out := filepath.Join(root, "report")

	if code, output := e2eRun(t, env, dir, out, env.defaults()); code != 0 {
		t.Fatalf("import exit %d:\n%s", code, output)
	}

	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: env.dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	audits, err := pg.NewAuditRepo(db, []byte(e2ePepper))
	if err != nil {
		t.Fatalf("audit repo: %v", err)
	}
	sessionRepo := pg.NewSessionRepo(db)
	issuer, err := pg.NewSessionIssuer(sessionRepo, audits, []byte(e2ePepper), coreclock.System{})
	if err != nil {
		t.Fatalf("issuer: %v", err)
	}
	svc, err := appauth.NewService(appauth.Deps{
		Users:         pg.NewUserRepo(db),
		Sessions:      issuer,
		OtherSessions: sessionRepo,
		Resets:        pg.NewPasswordResetRepo(db),
		Registrations: pg.NewSettingsRepo(db),
		Audits:        audits,
		Tx:            db,
		Clock:         coreclock.System{},
		Quota:         fixedQuota(10000),
		KDF:           coreauth.PlainHasher{},
	})
	if err != nil {
		t.Fatalf("auth service: %v", err)
	}

	issued, err := svc.Login(ctx, e2eAdminEmail, e2ePlainPW)
	if err != nil {
		t.Fatalf("login with legacy password: %v", err)
	}
	if issued.Token == "" {
		t.Fatal("login returned empty token")
	}

	stored, err := db.QueryString(ctx, `SELECT password_hash FROM users WHERE email = $1`, e2eAdminEmail)
	if err != nil {
		t.Fatalf("read hash: %v", err)
	}
	if !strings.HasPrefix(stored, "$argon2id$") {
		t.Fatalf("transparent upgrade did not happen, hash prefix %q", stored[:12])
	}
	if _, err := svc.Login(ctx, e2eAdminEmail, e2ePlainPW); err != nil {
		t.Fatalf("login after upgrade: %v", err)
	}
}

// TestMigrateLegacyTamperedReconZeroWritesE2E 守卫：
// strict 档导入中途致命错误（对账表被篡改）→ 退出码 2 且全部相关表 0 行。
// 契约是「exit 2 零写入」；比对必须发生在提交前。
func TestMigrateLegacyTamperedReconZeroWritesE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, filepath.Join(root, "tamp"), e2eFixture(t))
	// 去掉孤儿 time_prefs：strict 会在入口截停，到不了「导入中途」的致命错。
	// 同步从对账表里删掉该行的 recon 项，保持导出物自洽。
	for name, key := range map[string]string{
		"time_prefs.jsonl": e2ePhoneOrphan, "reconciliation.jsonl": coremask.MaskPhone(e2ePhoneOrphan),
	} {
		raw, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		var kept []string
		for _, line := range strings.Split(string(raw), "\n") {
			if line != "" && !strings.Contains(line, key) {
				kept = append(kept, line)
			}
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(strings.Join(kept, "\n")+"\n"), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	reconPath := filepath.Join(dir, "reconciliation.jsonl")
	raw, err := os.ReadFile(reconPath)
	if err != nil {
		t.Fatalf("read recon: %v", err)
	}
	bad := strings.Replace(string(raw), "138****0001", "138****0006", 1)
	if bad == string(raw) {
		t.Fatal("tamper no-op; fixture drift")
	}
	if err := os.WriteFile(reconPath, []byte(bad), 0o600); err != nil {
		t.Fatalf("write tampered recon: %v", err)
	}

	args := env.defaults()
	args.strict = true
	code, output := e2eRun(t, env, dir, filepath.Join(root, "report"), args)
	if code != 2 {
		t.Fatalf("exit = %d, want 2:\n%s", code, output)
	}
	for _, check := range []struct {
		table string
		want  int64
	}{
		{"users", 1}, // 仅 fallback 前置件
		{"accounts", 0},
		{"legacy_ids", 0},
		{"credentials", 0},
		{"admin_schools", 0},
	} {
		if n := e2eCount(t, env.dsn, "SELECT count(*) FROM "+check.table); n != check.want {
			t.Fatalf("%s = %d, want %d (exit 2 must write nothing)", check.table, n, check.want)
		}
	}
}

// TestMigrateLegacyRowErrorAtomicNoPhoneLeakE2E 守卫：
// 行级失败不污染其余行、失败行零残留、落地行必有 legacy_ids 映射、
// 行级错误输出零明文手机号/完整邮箱/owner 原文（撞唯一约束行故意夹带）。
func TestMigrateLegacyRowErrorAtomicNoPhoneLeakE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	// 追加两条必撞唯一约束的行（不入对账表；行级错误时比对跳过）：
	// 用户 id=4 撞 email、账号 id=6 撞 phone。
	dupUser := e2eUserRow(4, e2eAdminEmail, "user", 0, 1)
	dupAcct := e2eAccountRow(6, "acct-dup", e2ePhoneA, "user0001", "active", "", "", 0, 0)
	for name, row := range map[string]map[string]any{
		"users.jsonl": dupUser, "accounts.jsonl": dupAcct,
	} {
		f, err := os.OpenFile(filepath.Join(dir, name), os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			t.Fatalf("open %s: %v", name, err)
		}
		if _, err := f.WriteString(legacy.CanonicalLine(row) + "\n"); err != nil {
			t.Fatalf("append %s: %v", name, err)
		}
		if err := f.Close(); err != nil {
			t.Fatalf("close %s: %v", name, err)
		}
	}

	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID
	code, output := e2eRun(t, env, dir, filepath.Join(root, "report"), args)
	if code != 1 {
		t.Fatalf("exit = %d, want 1 (row errors):\n%s", code, output)
	}

	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 4 {
		t.Fatalf("users = %d, want 4 (3 imported + fallback; dup-email row must leave nothing)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 5 {
		t.Fatalf("accounts = %d, want 5 (dup-phone row must leave nothing)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 8 {
		t.Fatalf("legacy_ids = %d, want 8", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools"); n != 1 {
		t.Fatalf("admin_schools = %d, want 1", n)
	}
	e2eAssertLegacyInvariant(t, env.dsn)

	for _, phone := range []string{e2ePhoneA, e2ePhoneB, e2ePhoneC, e2ePhoneD, e2ePhoneE, e2ePhoneOrphan} {
		if strings.Contains(output, phone) {
			t.Fatalf("row error output leaks plaintext phone %s", coremask.MaskPhone(phone))
		}
	}
	for _, secret := range []string{e2eAdminEmail, e2eUserEmail1, e2eUserEmail2, e2eOwnerMissing} {
		if strings.Contains(output, secret) {
			t.Fatalf("row error output leaks %q", secret)
		}
	}
	if !strings.Contains(output, "138****0001") {
		t.Fatalf("row error output must carry the masked phone, got:\n%s", output)
	}
}

// TestMigrateLegacyAmbiguousOwnerRowErrorE2E：同一 owner 命中多个旧 user
// → 该账号行错误，不按行序猜；好用户照常导入。
func TestMigrateLegacyAmbiguousOwnerRowErrorE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eExport{
		Users: []map[string]any{
			e2eUserRow(1, e2eAdminEmail, "admin", 0, 1),
			e2eUserRow(2, "dup-owner@one.invalid", "user", 0, 1),
			e2eUserRow(3, "dup-owner@two.invalid", "user", 0, 1),
		},
		Accounts: []map[string]any{
			e2eAccountRow(1, "acct-amb", e2ePhoneA, "dup-owner", "active", "", "", 0, 0),
		},
	}
	dir := e2eWriteExport(t, root, ex)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, env, dir, out, env.defaults())
	if code != 1 {
		t.Fatalf("exit = %d, want 1 (row error):\n%s", code, output)
	}
	if !strings.Contains(output, "owner matches 2 legacy users") {
		t.Fatalf("output must report the ambiguity count, got:\n%s", output)
	}
	if strings.Contains(output, "dup-owner") {
		t.Fatalf("owner raw text must not be echoed, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 4 {
		t.Fatalf("users = %d, want 4 (3 imported + fallback)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("ambiguous account must not land, got %d", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 3 {
		t.Fatalf("legacy_ids = %d, want 3 (user rows only)", n)
	}
	summary := e2eSummary(t, out)
	owners := summary["owner_resolution"].(map[string]any)
	if owners["ambiguous"].(float64) != 1 || owners["fallback"].(float64) != 0 {
		t.Fatalf("owner resolution drifted: %v", owners)
	}
	if summary["recon_compare"] != "not compared: row errors present" {
		t.Fatalf("recon compare must be skipped on row errors, got %v", summary["recon_compare"])
	}
}

// TestMigrateLegacyExactOwnerAmbiguityE2E：两行归一后同邮箱（live＋deleted）
// 时 exact 级命中两个候选 → 账号行错误，不取第一条。
func TestMigrateLegacyExactOwnerAmbiguityE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	deleted := e2eUserRow(2, e2eAdminEmail, "user", 1, 1)
	deleted["deleted_at"] = "2026-01-02T00:00:00"
	ex := e2eExport{
		Users: []map[string]any{
			e2eUserRow(1, e2eAdminEmail, "user", 0, 1),
			deleted,
		},
		Accounts: []map[string]any{
			e2eAccountRow(1, "acct-exact-dup", e2ePhoneA, "  "+strings.ToUpper(e2eAdminEmail)+"  ",
				"active", "", "", 0, 0),
		},
	}
	dir := e2eWriteExport(t, root, ex)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, env, dir, out, env.defaults())
	if code != 1 {
		t.Fatalf("exit = %d, want 1 (row error):\n%s", code, output)
	}
	if !strings.Contains(output, "owner matches 2 legacy users") {
		t.Fatalf("output must report the exact-level ambiguity, got:\n%s", output)
	}
	if strings.Contains(output, e2eAdminEmail) {
		t.Fatalf("owner email must not be echoed, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 3 {
		t.Fatalf("users = %d, want 3 (live + deleted + fallback)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("exact-ambiguous account must not land, got %d", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 2 {
		t.Fatalf("legacy_ids = %d, want 2 (user rows only)", n)
	}
	e2eAssertLegacyInvariant(t, env.dsn)
	summary := e2eSummary(t, out)
	owners := summary["owner_resolution"].(map[string]any)
	if owners["ambiguous"].(float64) != 1 || owners["exact_email"].(float64) != 0 {
		t.Fatalf("exact-level ambiguity must count as ambiguous only: %v", owners)
	}
	if summary["recon_compare"] != "not compared: row errors present" {
		t.Fatalf("recon compare must be skipped on row errors, got %v", summary["recon_compare"])
	}
}

// TestMigrateLegacySpaceSeparatedTimestampsE2E 守卫旧生产时间形状：
// users.created_at、users.deleted_at 与 accounts.deleted_at 是
// `YYYY-MM-DD HH:MM:SS`（空格分隔、无时区）。导入必须按旧栈本地时区
// （+08:00）解读，不挡用户/账号落地、owner 解析、旧 admin 绑定与凭据重建。
func TestMigrateLegacySpaceSeparatedTimestampsE2E(t *testing.T) {
	const deletedEmail = "user0003@example.invalid"
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eFixture(t)
	deletedUser := e2eUserRow(4, deletedEmail, "user", 1, 1)
	deletedUser["deleted_at"] = "2026-01-02 00:00:00"
	ex.Users = append(ex.Users, deletedUser)
	for _, row := range ex.Users {
		row["created_at"] = "2026-01-01 00:00:00"
	}
	for _, row := range ex.Accounts {
		if row["phone"] == e2ePhoneD {
			row["deleted_at"] = "2026-01-02 00:00:00"
		}
	}
	dir := e2eWriteExport(t, root, ex)
	out := filepath.Join(root, "report")
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID

	code, output := e2eRun(t, env, dir, out, args)
	if code != 0 {
		t.Fatalf("exit %d (space-separated timestamps must import):\n%s", code, output)
	}
	if strings.Contains(output, "row error") {
		t.Fatalf("space-separated timestamps must not produce row errors:\n%s", output)
	}

	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 5 {
		t.Fatalf("users = %d, want 5 (4 imported + fallback master)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 5 {
		t.Fatalf("accounts = %d, want 5", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM credentials"); n != 3 {
		t.Fatalf("credentials = %d, want 3", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 9 {
		t.Fatalf("legacy_ids = %d, want 9", n)
	}
	e2eAssertLegacyInvariant(t, env.dsn)

	// owner 解析不被时间格式挡住：完整邮箱、@ 前缀、fallback 各走各的路。
	adminNew := e2eLegacyNewID(t, env.dsn, "users", 1)
	user1New := e2eLegacyNewID(t, env.dsn, "users", 2)
	user2New := e2eLegacyNewID(t, env.dsn, "users", 3)
	for _, tc := range []struct {
		phone string
		want  string
	}{
		{e2ePhoneA, adminNew}, {e2ePhoneB, user1New}, {e2ePhoneC, env.fallbackID},
		{e2ePhoneD, user2New}, {e2ePhoneE, user1New},
	} {
		got := e2eQueryString(t, env.dsn,
			`SELECT owner_user_id::text FROM accounts WHERE phone = $1`, tc.phone)
		if got != tc.want {
			t.Fatalf("owner of %s = %s, want %s", coremask.MaskPhone(tc.phone), got, tc.want)
		}
	}

	// 旧 admin 绑定与凭据路径同样不被时间格式挡住。
	if n := e2eCount(t, env.dsn,
		"SELECT count(*) FROM admin_schools WHERE user_id = $1::uuid AND school_id = $2::uuid",
		adminNew, env.adminSchoolID); n != 1 {
		t.Fatalf("admin_schools binding = %d, want 1", n)
	}

	// 空格串按旧栈本地时区（+08:00）解读：本地 2026-01-01 00:00:00 = UTC 前一天 16:00。
	if n := e2eCount(t, env.dsn,
		`SELECT count(*) FROM users WHERE created_at = TIMESTAMPTZ '2025-12-31 16:00:00+00'`); n != 4 {
		t.Fatalf("users with legacy-local created_at = %d, want 4", n)
	}
	if n := e2eCount(t, env.dsn,
		`SELECT count(*) FROM users WHERE email = $1 AND deleted_at = TIMESTAMPTZ '2026-01-01 16:00:00+00'`,
		deletedEmail); n != 1 {
		t.Fatalf("soft-deleted legacy user deleted_at = %d, want 1", n)
	}
	if n := e2eCount(t, env.dsn,
		`SELECT count(*) FROM accounts WHERE phone = $1 AND deleted_at = TIMESTAMPTZ '2026-01-01 16:00:00+00'`,
		e2ePhoneD); n != 1 {
		t.Fatalf("soft-deleted account deleted_at = %d, want 1", n)
	}

	summary := e2eSummary(t, out)
	rowErrors := summary["row_errors"].(map[string]any)
	if rowErrors["users"].(float64) != 0 || rowErrors["accounts"].(float64) != 0 {
		t.Fatalf("row_errors must be zero, got %v", rowErrors)
	}
	owners := summary["owner_resolution"].(map[string]any)
	if owners["exact_email"].(float64) != 3 || owners["email_prefix"].(float64) != 1 ||
		owners["fallback"].(float64) != 1 || owners["unresolved"].(float64) != 0 ||
		owners["ambiguous"].(float64) != 0 {
		t.Fatalf("owner resolution drifted: %v", owners)
	}
}

// TestMigrateLegacyInvalidTimestampRowErrorE2E 负向：空格格式不是任意字符串。
// 真正非法的 created_at 仍按行错误处理，坏行不落地，好行照常导入。
func TestMigrateLegacyInvalidTimestampRowErrorE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	good := e2eUserRow(1, e2eAdminEmail, "admin", 0, 1)
	good["created_at"] = "2026-01-01 00:00:00"
	bad := e2eUserRow(2, e2eUserEmail1, "user", 0, 1)
	bad["created_at"] = "2026-13-01 00:00:00"
	ex := e2eExport{Users: []map[string]any{good, bad}}
	dir := e2eWriteExport(t, root, ex)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, env, dir, out, env.defaults())
	if code != 1 {
		t.Fatalf("exit = %d, want 1 (invalid timestamp is a row error):\n%s", code, output)
	}
	if !strings.Contains(output, "row error in users (created_at)") {
		t.Fatalf("output must report the created_at row error, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 2 {
		t.Fatalf("users = %d, want 2 (valid row + fallback master)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids"); n != 1 {
		t.Fatalf("legacy_ids = %d, want 1 (invalid row must leave nothing)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM admin_schools"); n != 1 {
		t.Fatalf("admin_schools = %d, want 1 (valid admin row still binds)", n)
	}
	summary := e2eSummary(t, out)
	rowErrors := summary["row_errors"].(map[string]any)
	if rowErrors["users"].(float64) != 1 || rowErrors["accounts"].(float64) != 0 {
		t.Fatalf("row_errors drifted: %v", rowErrors)
	}
	if summary["recon_compare"] != "not compared: row errors present" {
		t.Fatalf("recon compare must be skipped on row errors, got %v", summary["recon_compare"])
	}
}

// TestMigrateLegacyOverlongNameRowErrorE2E 负向：备注名超过 50 字符是行级错误。
// 错误文本必须点名 remark 字段，运维才能从 stderr 直接定位，不靠猜。
func TestMigrateLegacyOverlongNameRowErrorE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eExport{
		Users: []map[string]any{
			e2eUserRow(1, e2eAdminEmail, "user", 0, 1),
		},
		Accounts: []map[string]any{
			e2eAccountRow(1, strings.Repeat("名", 51), e2ePhoneA, e2eAdminEmail,
				"active", "", "", 0, 0),
		},
	}
	dir := e2eWriteExport(t, root, ex)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, env, dir, out, env.defaults())
	if code != 1 {
		t.Fatalf("exit = %d, want 1 (overlong remark name is a row error):\n%s", code, output)
	}
	if !strings.Contains(output, "row error in accounts (insert)") {
		t.Fatalf("output must report the accounts insert row error, got:\n%s", output)
	}
	if !strings.Contains(output, "remark") {
		t.Fatalf("row error must name the remark field, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("accounts = %d, want 0 (overlong row must leave nothing)", n)
	}
	if n := e2eCount(t, env.dsn,
		"SELECT count(*) FROM legacy_ids WHERE old_table = 'accounts'"); n != 0 {
		t.Fatalf("account legacy_ids = %d, want 0 (overlong row must leave no mapping)", n)
	}
	summary := e2eSummary(t, out)
	rowErrors := summary["row_errors"].(map[string]any)
	if rowErrors["accounts"].(float64) != 1 {
		t.Fatalf("row_errors drifted: %v", rowErrors)
	}
}

// TestMigrateLegacyUnmatchedNonActiveOwnerE2E：owner 对不上的非 active
// 账号不落 fallback，显性行错误。
func TestMigrateLegacyUnmatchedNonActiveOwnerE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eExport{
		Users: []map[string]any{
			e2eUserRow(1, e2eAdminEmail, "user", 0, 1),
		},
		Accounts: []map[string]any{
			e2eAccountRow(1, "acct-ghost", e2ePhoneA, "ghost-owner", "rejected", "fixture-reason-0000", "", 0, 0),
		},
	}
	dir := e2eWriteExport(t, root, ex)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, env, dir, out, env.defaults())
	if code != 1 {
		t.Fatalf("exit = %d, want 1 (row error):\n%s", code, output)
	}
	if !strings.Contains(output, "fallback applies to active accounts only") {
		t.Fatalf("output must explain the fallback limit, got:\n%s", output)
	}
	if strings.Contains(output, "ghost-owner") {
		t.Fatalf("owner raw text must not be echoed, got:\n%s", output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users"); n != 2 {
		t.Fatalf("users = %d, want 2 (1 imported + fallback)", n)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("unmatched non-active account must not land, got %d", n)
	}
	summary := e2eSummary(t, out)
	owners := summary["owner_resolution"].(map[string]any)
	if owners["unresolved"].(float64) != 1 || owners["fallback"].(float64) != 0 {
		t.Fatalf("owner resolution drifted: %v", owners)
	}
}

// F-P2-7：purge 后重跑同导出物＝幂等跳过，**不** exit 1、**不**复活。
// legacy_ids 无级联：目标行被物理 purge 后映射行还在。旧实现把
// 「row is gone」记行错误（exit 1）；现口径：命中已 purge 行按幂等跳过
// 计数，重跑 0 新增、0 行错。
func TestMigrateLegacyPurgedTargetIdempotentSkipE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	dir := e2eWriteExport(t, root, e2eFixture(t))
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r1"), args); code != 0 {
		t.Fatalf("first run exit %d:\n%s", code, output)
	}
	// 模拟保留期 purge：按 user.Purge 同款顺序物理删除用户名下账号再删
	// 用户本身（owner FK 是 RESTRICT）；legacy_ids 行留在表里（无 FK 级联，
	// 正是病根形状）。
	userID := e2eLegacyNewID(t, env.dsn, "users", 2)
	e2eExec(t, env.dsn, "DELETE FROM accounts WHERE owner_user_id = $1::uuid", userID)
	e2eExec(t, env.dsn, "DELETE FROM users WHERE id = $1::uuid", userID)

	code, output := e2eRun(t, env, dir, filepath.Join(root, "r2"), args)
	if code != 0 {
		t.Fatalf("rerun after purge exit %d, want 0:\n%s", code, output)
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM users WHERE id = $1::uuid", userID); n != 0 {
		t.Fatal("已 purge 用户被导入复活")
	}
	summary := e2eSummary(t, filepath.Join(root, "r2"))
	rows := summary["rows_imported"].(map[string]any)
	if rows["users"].(float64) != 0 || rows["accounts"].(float64) != 0 {
		t.Fatalf("重跑新增 = %v, want 0/0", rows)
	}
	errs := summary["row_errors"].(map[string]any)
	if errs["users"].(float64) != 0 || errs["accounts"].(float64) != 0 {
		t.Fatalf("重跑行错 = %v, want 0/0（幂等跳过不是错误）", errs)
	}
	purged, ok := summary["purged_targets"].(map[string]any)
	if !ok {
		t.Fatalf("summary 缺 purged_targets 计数：%v", summary)
	}
	if purged["users"].(float64) != 1 || purged["accounts"].(float64) != 2 {
		t.Fatalf("purged_targets = %v, want users 1 / accounts 2（user 2 名下 acct-b/acct-e）", purged)
	}
	e2eAssertLegacyInvariant(t, env.dsn)
}

// F-P3-10：deleted_by 保留。旧行自删（deleted_by=属主邮箱）导入后
// deleted_by=属主新 id——自助恢复判据「只认 deleted_by=owner」不再丢；
// 管理员删除（deleted_by='admin'，旧线排除反悔的留痕形状）映射为 NULL。
// 旧 users 表没有 deleted_by 列，注销即本人（旧线唯一注销路径
// soft_delete_user_with_accounts），导入把本人 id 盖进 users.deleted_by。
func TestMigrateLegacyDeletedByKeptE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eFixture(t)
	// acct-a 改为「属主自删」：deleted=1、deleted_by=user0000（admin 本人邮箱）。
	selfDeleted := ex.Accounts[0]
	selfDeleted["deleted"] = json.Number("1")
	selfDeleted["deleted_at"] = "2026-01-03T00:00:00"
	selfDeleted["deleted_by"] = e2eAdminEmail
	// 加一个自删用户（id 4），名下无账号。
	ex.Users = append(ex.Users, map[string]any{
		"id": json.Number("4"), "email": "user0003@example.invalid", "password_hash": e2eScryptHash,
		"role": "user", "created_at": "2026-01-01T00:00:00", "pw_version": json.Number("1"),
		"deleted": json.Number("1"), "deleted_at": "2026-01-04T00:00:00", "mail_notify": json.Number("1"),
	})
	dir := e2eWriteExport(t, root, ex)
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r1"), args); code != 0 {
		t.Fatalf("run exit %d:\n%s", code, output)
	}

	ownerNewID := e2eLegacyNewID(t, env.dsn, "users", 1)
	acctNewID := e2eLegacyNewID(t, env.dsn, "accounts", 1)
	got := e2eQueryString(t, env.dsn,
		`SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid`, acctNewID)
	if got != ownerNewID {
		t.Fatalf("自删账号 deleted_by = %q, want 属主新 id %q（自助恢复前提）", got, ownerNewID)
	}
	// 旧 deleted_by='admin'：不指向任何注册者，映射 NULL——非本人、不自助复活。
	acctD := e2eLegacyNewID(t, env.dsn, "accounts", 4)
	got = e2eQueryString(t, env.dsn,
		`SELECT coalesce(deleted_by::text, '') FROM accounts WHERE id = $1::uuid`, acctD)
	if got != "" {
		t.Fatalf("admin-deleted 账号 deleted_by = %q, want NULL", got)
	}
	// 自删用户：deleted_by=本人 id（旧线注销路径唯一为本人）。
	userNewID := e2eLegacyNewID(t, env.dsn, "users", 4)
	got = e2eQueryString(t, env.dsn,
		`SELECT coalesce(deleted_by::text, '') FROM users WHERE id = $1::uuid`, userNewID)
	if got != userNewID {
		t.Fatalf("自删用户 deleted_by = %q, want 本人 id %q", got, userNewID)
	}
	// CHECK 形状：未删行不得留 deleted_by。
	if n := e2eCount(t, env.dsn,
		`SELECT count(*) FROM accounts WHERE deleted_at IS NULL AND deleted_by IS NOT NULL`); n != 0 {
		t.Fatalf("live 账号带 deleted_by = %d, want 0", n)
	}
	e2eAssertLegacyInvariant(t, env.dsn)
}

// F-P3-11：导入校验 ValidEmail——形状非法的旧邮箱按行错误处理，
// 不产生「无法登录的用户」。同族形状判据与注册面同源（core/user.ValidEmail）。
func TestMigrateLegacyInvalidEmailRowErrorE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eFixture(t)
	ex.Users = append(ex.Users, map[string]any{
		"id": json.Number("9"), "email": "not-an-email", "password_hash": e2eScryptHash,
		"role": "user", "created_at": "2026-01-01T00:00:00", "pw_version": json.Number("1"),
		"deleted": json.Number("0"), "deleted_at": "", "mail_notify": json.Number("1"),
	})
	dir := e2eWriteExport(t, root, ex)
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID
	code, output := e2eRun(t, env, dir, filepath.Join(root, "r1"), args)
	if code != 1 {
		t.Fatalf("exit = %d, want 1（行错误）:\n%s", code, output)
	}
	if !strings.Contains(output, "email") {
		t.Fatalf("行错误未点名 email:\n%s", output)
	}
	if n := e2eCount(t, env.dsn,
		`SELECT count(*) FROM users WHERE email = 'not-an-email'`); n != 0 {
		t.Fatal("非法邮箱用户落库")
	}
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM legacy_ids WHERE old_table = 'users' AND old_id = 9"); n != 0 {
		t.Fatal("行错误仍留下 legacy_ids 映射")
	}
	// 其余好行照常（行级原子，不整批回滚）：原夹具 3 用户 5 账号全在。
	if n := e2eCount(t, env.dsn, "SELECT count(*) FROM accounts"); n != 5 {
		t.Fatalf("好行账号 = %d, want 5", n)
	}
	e2eAssertLegacyInvariant(t, env.dsn)
}

// F-P3-12：已删用户名下的 live 账号——按旧线正本（users.soft_delete_user_with_accounts）
// 联动软删而非放行：deleted_at 取属主注销时刻、deleted_by 记属主，
// 状态词不动。「照签」被切断：软删行不进排期。
func TestMigrateLegacyDeletedOwnerLiveAccountE2E(t *testing.T) {
	env := e2eSetupDB(t)
	root := t.TempDir()
	ex := e2eFixture(t)
	// 用户 2 注销（deleted=1，时刻 2026-01-05）；其名下 acct-b 仍 live。
	ex.Users[1]["deleted"] = json.Number("1")
	ex.Users[1]["deleted_at"] = "2026-01-05T08:30:00"
	dir := e2eWriteExport(t, root, ex)
	args := env.defaults()
	args.adminSchoolID = env.adminSchoolID
	if code, output := e2eRun(t, env, dir, filepath.Join(root, "r1"), args); code != 0 {
		t.Fatalf("run exit %d:\n%s", code, output)
	}

	acctB := e2eLegacyNewID(t, env.dsn, "accounts", 2)
	row := e2eQueryString(t, env.dsn, `
		SELECT status || '|' || coalesce(deleted_at::timestamp::text, 'NULL') || '|' ||
		       coalesce(deleted_by::text, 'NULL')
		  FROM accounts WHERE id = $1::uuid`, acctB)
	// 旧时间戳无时区，按旧栈本地时区 +8 解读：08:30 CST = 00:30 UTC。
	if row != "paused_user|2026-01-05 00:30:00|"+e2eLegacyNewID(t, env.dsn, "users", 2) {
		t.Fatalf("已删用户名下 live 账号 = %q, want 联动软删（deleted_at=注销时刻, deleted_by=属主, 状态词不动）", row)
	}
	// 显性计数进 summary。
	summary := e2eSummary(t, filepath.Join(root, "r1"))
	if n, ok := summary["owner_deleted_cascaded"].(float64); !ok || n != 2 {
		t.Fatalf("owner_deleted_cascaded = %v, want 2（acct-b 与 acct-e 同属注销用户 2）", summary["owner_deleted_cascaded"])
	}
	// 本来就已软删的账号（acct-d）保持各自更早时刻（旧线保留期口径：
	// 「注销当时哪一行在生效」的唯一线索不能被覆盖）。
	acctD := e2eLegacyNewID(t, env.dsn, "accounts", 4)
	if got := e2eQueryString(t, env.dsn,
		`SELECT deleted_at::timestamp::text FROM accounts WHERE id = $1::uuid`, acctD); got != "2026-01-01 16:00:00" { // 01-02T00:00 +8 ⇒ UTC 前一日 16:00
		t.Fatalf("既有软删时刻被改写: %q", got)
	}
	e2eAssertLegacyInvariant(t, env.dsn)
}
