package main

// migrate-legacy 的真库 e2e（P8-B 判据，派单 2026-10-27）。
//
// 模拟的真实用户动作：运维在迁移窗口对旧栈导出物目录跑一次
// `yiban-cli migrate-legacy --dir 导出目录 --school-id … --owner-user-id …`。
// 断言的用户可见结果：行数与掩码哈希对账一致、重复导入零新增、
// 孤儿按档位处理、旧口令能登录且透明升级、错钥显性红、必填旗缺失红。
//
// 夹具全合成值：手机号取 tests/synthetic-fixtures.json 登记值
// （13800000001–13800000004、13899999999）；信封十六进制为随机测试段；
// 旧口令哈希为 werkzeug scrypt 形状（口令 pw-fixture-0000，合成盐）。
//
// 缺 YIBAN_TEST_DATABASE_URL 时显性 SKIP：跳过不是绿（AGENTS §30）。

import (
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
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

const (
	e2eLegacyKeyHex = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
	// werkzeug scrypt 形状的旧口令哈希（口令 pw-fixture-0000，N=32768 r=8 p=1，
	// 盐 0123456789abcdef，dklen=64；2026-10-27 由 Python hashlib 现算）。
	e2eScryptHash  = "scrypt:32768:8:1$0123456789abcdef$9b9e287f0e026b4d76b48016ec8706d7a64ff1609eab098ee2a7bd1fa187af5908b54a1578b206483511c7d7225a838e51aad45e91e96c4f361b1682db887dd3"
	e2ePlainPW     = "pw-fixture-0000"
	e2eCredPW      = "pw-fixture-0001"
	e2ePepper      = "0123456789abcdef0123456789abcdef"
	e2eSchoolCode  = "migrfix0000"
	e2eOwnerEmail  = "owner-fixture-0000@example.invalid"
	e2eLiveEmail   = "user0000@example.invalid"
	e2eDeletedMail = "user0001@example.invalid"
)

var (
	e2ePhoneA      = "13800000001" // 活跃账号，带凭据与自选片
	e2ePhoneB      = "13800000002" // 软删账号，带凭据
	e2ePhoneC      = "13800000003" // rejected，凭据明文驻留（导出后为空串）
	e2ePhoneD      = "13800000004" // 活跃 + user_paused，phone_code 有信封
	e2ePhoneOrphan = "13899999999" // time_prefs 孤儿
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
	env := map[string]any{
		"v":     json.Number("2"),
		"kid":   legacy.LegacyKID(key),
		"nonce": hex.EncodeToString(nonce),
		"ct":    hex.EncodeToString(out[:len(out)-16]),
		"tag":   hex.EncodeToString(out[len(out)-16:]),
	}
	return env
}

// e2eWriteExport 造一份合成导出物目录，返回 (目录, 各表行)。
// 行键序与 P8-A 导出器一致（jsonl 每行键排序）。
func e2eWriteExport(t *testing.T, root string) (string, map[string][]map[string]any) {
	t.Helper()
	dir := filepath.Join(root, "export")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	legacyKey, _ := hex.DecodeString(e2eLegacyKeyHex)

	users := []map[string]any{
		{"id": json.Number("1"), "email": e2eLiveEmail, "password_hash": e2eScryptHash,
			"role": "admin", "created_at": "2026-01-01T00:00:00", "pw_version": json.Number("1"),
			"deleted": json.Number("0"), "deleted_at": "", "mail_notify": json.Number("1")},
		{"id": json.Number("2"), "email": e2eDeletedMail, "password_hash": "scrypt:32768:8:1$0123456789abcdef$ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff",
			"role": "user", "created_at": "2026-01-01T00:00:00", "pw_version": json.Number("1"),
			"deleted": json.Number("1"), "deleted_at": "2026-01-02T00:00:00", "mail_notify": json.Number("0")},
	}
	envA := e2eSealOld(t, legacyKey, e2ePhoneA, e2eCredPW)
	envB := e2eSealOld(t, legacyKey, e2ePhoneB, e2eCredPW)
	envD := e2eSealOld(t, legacyKey, e2ePhoneD, e2eCredPW)
	envCode := e2eSealOld(t, legacyKey, e2ePhoneD, "code-fixture-0001")
	accounts := []map[string]any{
		{"id": json.Number("1"), "sort_order": json.Number("0"), "name": "acct-a", "phone": e2ePhoneA,
			"password": envA, "phone_model": "model-0000", "phone_code": "", "owner": "admin",
			"status": "active", "reject_reason": "", "deleted": json.Number("0"),
			"deleted_at": "", "deleted_by": "", "user_paused": json.Number("0")},
		{"id": json.Number("2"), "sort_order": json.Number("1"), "name": "acct-b", "phone": e2ePhoneB,
			"password": envB, "phone_model": "", "phone_code": "", "owner": "admin",
			"status": "active", "reject_reason": "", "deleted": json.Number("1"),
			"deleted_at": "2026-01-02T00:00:00", "deleted_by": "admin", "user_paused": json.Number("0")},
		{"id": json.Number("3"), "sort_order": json.Number("2"), "name": "acct-c", "phone": e2ePhoneC,
			"password": "", "phone_model": "", "phone_code": "", "owner": "boss",
			"status": "rejected", "reject_reason": "fixture-reason-0000", "deleted": json.Number("0"),
			"deleted_at": "", "deleted_by": "", "user_paused": json.Number("0")},
		{"id": json.Number("4"), "sort_order": json.Number("3"), "name": "acct-d", "phone": e2ePhoneD,
			"password": envD, "phone_model": "model-0004", "phone_code": envCode, "owner": "admin",
			"status": "active", "reject_reason": "", "deleted": json.Number("0"),
			"deleted_at": "", "deleted_by": "", "user_paused": json.Number("1")},
	}
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
	rows := map[string][]map[string]any{
		"users": users, "schools": {}, "accounts": accounts,
		"time_prefs": prefs, "audit": audits,
	}
	files := map[string][]map[string]any{
		"users.jsonl": users, "schools.jsonl": {}, "accounts.jsonl": accounts,
		"time_prefs.jsonl": prefs, "audit.jsonl": audits,
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
	// 对账表：与 P8-A 同一口径（users/accounts/audit 按行 id，time_prefs 按掩码手机号）。
	var recon strings.Builder
	for _, table := range []string{"users", "accounts", "time_prefs", "audit"} {
		keyField := "id"
		if table == "time_prefs" {
			keyField = "phone"
		}
		for _, row := range rows[table] {
			key := fmt.Sprint(row[keyField])
			if keyField == "phone" {
				key = legacy.MaskPhone(key)
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
	return dir, rows
}

// e2eSetupDB 建独立 schema，迁移到最新（含 legacy_ids），建校与 owner，
// 返回 (dsn, schoolID, ownerID)。
func e2eSetupDB(t *testing.T) (string, string, string) {
	t.Helper()
	base := strings.TrimSpace(os.Getenv("YIBAN_TEST_DATABASE_URL"))
	if base == "" {
		t.Skip("SKIP: YIBAN_TEST_DATABASE_URL is not set. migrate-legacy e2e did NOT run.")
	}
	dsn := userTestSchema(t) // 已迁移到最新版本
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(db.Close)
	var schoolID, ownerID string
	schoolID, err = db.QueryString(ctx,
		`INSERT INTO schools (code, name) VALUES ($1, $2) RETURNING id::text`,
		e2eSchoolCode, "Fixture School "+e2eSchoolCode)
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	ownerID, err = db.QueryString(ctx,
		`INSERT INTO users (email, password_hash, role) VALUES ($1, 'argon2id$test', 'master') RETURNING id::text`,
		e2eOwnerEmail)
	if err != nil {
		t.Fatalf("create owner: %v", err)
	}
	return dsn, schoolID, ownerID
}

// e2eRun 用给定旗跑一次 migrate-legacy，返回退出码与合并输出。
func e2eRun(t *testing.T, dsn, dir, out string, schoolID, ownerID, legacyKey string, strict bool) (int, string) {
	t.Helper()
	t.Setenv("YIBAN_CREDENTIAL_KEY", e2eNewKeyB64)
	args := []string{
		"--dir", dir, "--out", out, "--dsn", dsn,
		"--school-id", schoolID, "--owner-user-id", ownerID,
	}
	if legacyKey != "" {
		args = append(args, "--legacy-credential-key", legacyKey)
	}
	if strict {
		args = append(args, "--strict")
	}
	var sb strings.Builder
	code := runMigrateLegacy(args, &sb, &sb)
	return code, sb.String()
}

func e2eCount(t *testing.T, dsn, sql string) int64 {
	t.Helper()
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	n, err := db.QueryInt64(ctx, sql)
	if err != nil {
		t.Fatalf("count %s: %v", sql, err)
	}
	return n
}

// TestMigrateLegacyImportReconcileE2E 判据 1（含判据 3 的默认档）：
// 导入成功、行数与哈希对账、孤儿默认跳过且计数可见、明文零落盘。
func TestMigrateLegacyImportReconcileE2E(t *testing.T) {
	dsn, schoolID, ownerID := e2eSetupDB(t)
	root := t.TempDir()
	dir, _ := e2eWriteExport(t, root)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, dsn, dir, out, schoolID, ownerID, e2eLegacyKeyHex, false)
	if code != 0 {
		t.Fatalf("exit %d, output:\n%s", code, output)
	}

	// 行数：users 2（+owner 1），accounts 4，credentials 2，legacy_ids 6。
	if n := e2eCount(t, dsn, "SELECT count(*) FROM users"); n != 3 {
		t.Fatalf("users = %d, want 3 (2 imported + 1 owner)", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts"); n != 4 {
		t.Fatalf("accounts = %d, want 4", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM credentials"); n != 2 {
		t.Fatalf("credentials = %d, want 2", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM legacy_ids"); n != 6 {
		t.Fatalf("legacy_ids = %d, want 6", n)
	}

	// 状态映射：active→active、软删→deleted_at、rejected 带理由、user_paused→paused_user。
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts WHERE status = 'active' AND deleted_at IS NULL"); n != 1 {
		t.Fatalf("live active accounts = %d, want 1", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts WHERE deleted_at IS NOT NULL"); n != 1 {
		t.Fatalf("soft-deleted accounts = %d, want 1", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts WHERE status = 'rejected' AND reject_reason = 'fixture-reason-0000'"); n != 1 {
		t.Fatalf("rejected accounts = %d, want 1", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts WHERE status = 'paused_user'"); n != 1 {
		t.Fatalf("paused_user accounts = %d, want 1", n)
	}
	// 角色映射：admin→master、user→user；不产生 admin_schools 行。
	if n := e2eCount(t, dsn, "SELECT count(*) FROM users WHERE role = 'master' AND email = '"+e2eLiveEmail+"'"); n != 1 {
		t.Fatalf("imported admin as master: %d, want 1", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM users WHERE role = 'user'"); n != 1 {
		t.Fatalf("imported user rows = %d, want 1", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM admin_schools"); n != 0 {
		t.Fatalf("admin_schools rows = %d, want 0", n)
	}

	// 凭据重封装可用新钥解回，device_code 为空串（裁决 7 的解码前提）。
	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 1, StatementTimeout: 30 * time.Second})
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
	// 单值查询拼接多列：hex 与 uuid 都不含 '|'。
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
	if payload.Password != e2eCredPW || payload.DeviceCode != "" || payload.PhoneModel != "model-0000" {
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

	// summary 计数：孤儿、凭据缺行、phone_code 弃置、owner 分布、time_prefs 不导入。
	summaryRaw, err := os.ReadFile(filepath.Join(out, "summary.json"))
	if err != nil {
		t.Fatalf("read summary: %v", err)
	}
	var summary map[string]any
	if err := json.Unmarshal(summaryRaw, &summary); err != nil {
		t.Fatalf("parse summary: %v", err)
	}
	if got := summary["orphan_time_prefs"].(float64); got != 1 {
		t.Fatalf("orphan count = %v, want 1", summary["orphan_time_prefs"])
	}
	creds := summary["credentials"].(map[string]any)
	if creds["created"].(float64) != 2 || creds["missing"].(float64) != 1 || creds["phone_code_dropped"].(float64) != 1 {
		t.Fatalf("credential counts drifted: %v", creds)
	}
	if summary["time_prefs_imported"].(float64) != 0 {
		t.Fatalf("time_prefs must not be imported, got %v", summary["time_prefs_imported"])
	}
	owners := summary["owner_values"].(map[string]any)
	if owners["admin"].(float64) != 3 || owners["boss"].(float64) != 1 {
		t.Fatalf("owner distribution drifted: %v", owners)
	}

	// 明文零落盘：口令明文不出现在任何输出物。
	for _, name := range []string{"reconciliation.jsonl", "summary.json"} {
		blob, err := os.ReadFile(filepath.Join(out, name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		if strings.Contains(string(blob), e2eCredPW) || strings.Contains(string(blob), e2ePlainPW) {
			t.Fatalf("plaintext leaked into %s", name)
		}
	}
}

// TestMigrateLegacyIdempotentE2E 判据 2：第二遍 0 新增，legacy_ids 全命中，
// 对账表与第一遍逐字节一致。
func TestMigrateLegacyIdempotentE2E(t *testing.T) {
	dsn, schoolID, ownerID := e2eSetupDB(t)
	root := t.TempDir()
	dir, _ := e2eWriteExport(t, root)
	out1 := filepath.Join(root, "report1")
	out2 := filepath.Join(root, "report2")

	if code, output := e2eRun(t, dsn, dir, out1, schoolID, ownerID, e2eLegacyKeyHex, false); code != 0 {
		t.Fatalf("first run exit %d:\n%s", code, output)
	}
	code, output := e2eRun(t, dsn, dir, out2, schoolID, ownerID, e2eLegacyKeyHex, false)
	if code != 0 {
		t.Fatalf("second run exit %d:\n%s", code, output)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM users"); n != 3 {
		t.Fatalf("users after rerun = %d, want 3", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts"); n != 4 {
		t.Fatalf("accounts after rerun = %d, want 4", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM legacy_ids"); n != 6 {
		t.Fatalf("legacy_ids after rerun = %d, want 6", n)
	}
	summaryRaw, err := os.ReadFile(filepath.Join(out2, "summary.json"))
	if err != nil {
		t.Fatalf("read summary: %v", err)
	}
	var summary map[string]any
	if err := json.Unmarshal(summaryRaw, &summary); err != nil {
		t.Fatalf("parse summary: %v", err)
	}
	already := summary["already_imported"].(map[string]any)
	if already["users"].(float64) != 2 || already["accounts"].(float64) != 4 {
		t.Fatalf("already_imported drifted: %v", already)
	}
	first, _ := os.ReadFile(filepath.Join(out1, "reconciliation.jsonl"))
	second, _ := os.ReadFile(filepath.Join(out2, "reconciliation.jsonl"))
	if string(first) != string(second) {
		t.Fatal("second-run reconciliation differs from first")
	}
}

// TestMigrateLegacyStrictOrphanE2E 判据 3：--strict 在孤儿处失败且零写入。
func TestMigrateLegacyStrictOrphanE2E(t *testing.T) {
	dsn, schoolID, ownerID := e2eSetupDB(t)
	root := t.TempDir()
	dir, _ := e2eWriteExport(t, root)
	out := filepath.Join(root, "report")

	code, output := e2eRun(t, dsn, dir, out, schoolID, ownerID, e2eLegacyKeyHex, true)
	if code != 1 {
		t.Fatalf("strict exit = %d, want 1:\n%s", code, output)
	}
	if !strings.Contains(output, "orphan") {
		t.Fatalf("output must name the orphan, got:\n%s", output)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM users"); n != 1 {
		t.Fatalf("strict run must write no users, got %d (owner only)", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts"); n != 0 {
		t.Fatalf("strict run must write no accounts, got %d", n)
	}
}

// TestMigrateLegacyDecryptWithNewKeyFailsLoudE2E 判据 5：
// 旧密文用新 KEK 当旧钥解 → 显性报错（退出码 1＋计数），不静默。
func TestMigrateLegacyDecryptWithNewKeyFailsLoudE2E(t *testing.T) {
	dsn, schoolID, ownerID := e2eSetupDB(t)
	root := t.TempDir()
	dir, _ := e2eWriteExport(t, root)
	out := filepath.Join(root, "report")

	newKeyRaw, _ := base64.StdEncoding.DecodeString(e2eNewKeyB64)
	code, output := e2eRun(t, dsn, dir, out, schoolID, ownerID, hex.EncodeToString(newKeyRaw), false)
	if code != 1 {
		t.Fatalf("exit = %d, want 1:\n%s", code, output)
	}
	if !strings.Contains(output, "decrypt") {
		t.Fatalf("output must name decrypt failures, got:\n%s", output)
	}
	// 账号照建（裁决 5），凭据行缺省。
	if n := e2eCount(t, dsn, "SELECT count(*) FROM accounts"); n != 4 {
		t.Fatalf("accounts = %d, want 4 (created despite decrypt failure)", n)
	}
	if n := e2eCount(t, dsn, "SELECT count(*) FROM credentials"); n != 0 {
		t.Fatalf("credentials = %d, want 0", n)
	}
	summaryRaw, err := os.ReadFile(filepath.Join(out, "summary.json"))
	if err != nil {
		t.Fatalf("read summary: %v", err)
	}
	var summary map[string]any
	if err := json.Unmarshal(summaryRaw, &summary); err != nil {
		t.Fatalf("parse summary: %v", err)
	}
	creds := summary["credentials"].(map[string]any)
	if creds["decrypt_failed"].(float64) != 2 {
		t.Fatalf("decrypt_failed = %v, want 2", creds["decrypt_failed"])
	}
}

// TestMigrateLegacyMissingSchoolOwnerE2E 判据 6：
// --school-id / --owner-user-id 不存在即红；对账表被篡改也红。
func TestMigrateLegacyMissingSchoolOwnerE2E(t *testing.T) {
	dsn, schoolID, ownerID := e2eSetupDB(t)
	root := t.TempDir()
	dir, _ := e2eWriteExport(t, root)

	if code, output := e2eRun(t, dsn, dir, filepath.Join(root, "r1"),
		"00000000-0000-0000-0000-000000000000", ownerID, e2eLegacyKeyHex, false); code != 2 {
		t.Fatalf("missing school exit = %d, want 2:\n%s", code, output)
	}
	if code, output := e2eRun(t, dsn, dir, filepath.Join(root, "r2"),
		schoolID, "00000000-0000-0000-0000-000000000000", e2eLegacyKeyHex, false); code != 2 {
		t.Fatalf("missing owner exit = %d, want 2:\n%s", code, output)
	}

	// 导出物对账表被篡改 → 导入拒绝（导出物完整性门）。
	tampered, _ := e2eWriteExport(t, filepath.Join(root, "t2"))
	reconPath := filepath.Join(tampered, "reconciliation.jsonl")
	raw, err := os.ReadFile(reconPath)
	if err != nil {
		t.Fatalf("read recon: %v", err)
	}
	bad := strings.Replace(string(raw), "138****0001", "138****0002", 1)
	if bad == string(raw) {
		t.Fatal("tamper no-op; fixture drift")
	}
	if err := os.WriteFile(reconPath, []byte(bad), 0o600); err != nil {
		t.Fatalf("write tampered recon: %v", err)
	}
	if code, output := e2eRun(t, dsn, tampered, filepath.Join(root, "r3"),
		schoolID, ownerID, e2eLegacyKeyHex, false); code != 2 {
		t.Fatalf("tampered recon exit = %d, want 2:\n%s", code, output)
	}
}

// TestMigrateLegacyLoginOldPasswordE2E 判据 4：
// 导入后旧口令登录成功，且登录路径当场把 scrypt 透明升级为 argon2id。
func TestMigrateLegacyLoginOldPasswordE2E(t *testing.T) {
	dsn, schoolID, ownerID := e2eSetupDB(t)
	root := t.TempDir()
	dir, _ := e2eWriteExport(t, root)
	out := filepath.Join(root, "report")

	if code, output := e2eRun(t, dsn, dir, out, schoolID, ownerID, e2eLegacyKeyHex, false); code != 0 {
		t.Fatalf("import exit %d:\n%s", code, output)
	}

	ctx := context.Background()
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()
	audits, err := pg.NewAuditRepo(db, []byte(e2ePepper))
	if err != nil {
		t.Fatalf("audit repo: %v", err)
	}
	issuer, err := pg.NewSessionIssuer(pg.NewSessionRepo(db), audits, []byte(e2ePepper), coreclock.System{})
	if err != nil {
		t.Fatalf("issuer: %v", err)
	}
	svc, err := appauth.NewService(appauth.Deps{
		Users:    pg.NewUserRepo(db),
		Sessions: issuer,
		Resets:   pg.NewPasswordResetRepo(db),
		Audits:   audits,
		Tx:       db,
		Clock:    coreclock.System{},
	})
	if err != nil {
		t.Fatalf("auth service: %v", err)
	}

	// 真实用户动作：迁移过来的用户用旧口令登录。
	issued, err := svc.Login(ctx, e2eLiveEmail, e2ePlainPW)
	if err != nil {
		t.Fatalf("login with legacy password: %v", err)
	}
	if issued.Token == "" {
		t.Fatal("login returned empty token")
	}

	// 透明升级：登录后库内哈希已是 argon2id，且换新口令校验仍通过。
	stored, err := db.QueryString(ctx,
		`SELECT password_hash FROM users WHERE email = $1`, e2eLiveEmail)
	if err != nil {
		t.Fatalf("read hash: %v", err)
	}
	if !strings.HasPrefix(stored, "$argon2id$") {
		t.Fatalf("transparent upgrade did not happen, hash prefix %q", stored[:12])
	}
	if _, err := svc.Login(ctx, e2eLiveEmail, e2ePlainPW); err != nil {
		t.Fatalf("login after upgrade: %v", err)
	}
}
