package legacy

// 导入编排：读导出物 → 校验 → 单事务写入 → 提交前对账 → 报告输出。
//
// 退出码语义（由 cmd/cli 落到进程退出码）：
//   - 2：用法/校验失败（缺文件、school/owner 不存在、schools 非空、
//     导出物对账表对不上）。对账比对在事务提交**前**执行，
//     不一致即回滚，exit 2 零写入。
//   - 1：--strict 下有孤儿，或存在行级错误。行级写入是原子单元
//     （主行＋legacy_ids 映射＋凭据同存亡）：单元失败整行回滚，
//     好行照常提交、坏行显性计数。
//   - 0：全部成功（默认档孤儿跳过也算成功，计数可见）。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// CredentialPayload 是重封装前的明文负载。三键全部出现；
// device_code 由导入侧置空串（裁决 7；解码侧允许空串已核）。
type CredentialPayload struct {
	DeviceCode string
	Password   string
	PhoneModel string
}

// CredentialOut 是封装完成的密文行（待写 credentials 表）。
type CredentialOut struct {
	Alg        string
	KID        string
	AADVersion int16
	Nonce      []byte
	Ciphertext []byte
}

// Sealer 是新栈凭据封装的缝。实现在 app/account 的 Cipher；
// 由 cmd/cli 装配注入——boundary 门禁止 adapters 导入 app，
// 本包只见接口，不见实现。
type Sealer interface {
	Seal(schoolID, accountID string, payload CredentialPayload) (CredentialOut, error)
}

// Error 携带进程退出码的致命错误。
type Error struct {
	Code int
	Msg  string
}

func (e *Error) Error() string { return e.Msg }

func fatal(code int, format string, args ...any) *Error {
	return &Error{Code: code, Msg: "migrate-legacy: " + fmt.Sprintf(format, args...)}
}

// Options 是一次导入的全部输入。
type Options struct {
	// Dir 是 P8-A 导出物目录（五个 .jsonl + reconciliation.jsonl）。
	Dir string
	// OutDir 是报告输出目录（reconciliation.jsonl + summary.json）。
	OutDir string
	// DB 是已打开的新栈连接（schema 须已迁移到含 legacy_ids 的版本）。
	DB *pg.DB
	// SchoolID 是全部账号的目标学校（须已存在，不造学校）。
	SchoolID string
	// OwnerUserID 是全部账号的归属用户（须已存在且未删除）。
	OwnerUserID string
	// Strict 为真时存在孤儿即失败。
	Strict bool
	// LegacyKey 是旧栈 YIBAN_ACCOUNTS_KEY（32 字节）。
	LegacyKey []byte
	// Sealer 是新栈凭据封装器（cmd/cli 用 app/account 的 Cipher 装配注入）。
	Sealer Sealer
	// Now 是迁移时刻（deleted_at 缺失的软删行与 created_at 空值用它）。
	Now time.Time
	// Err 是行级错误的输出端（nil 时用 os.Stderr）。
	// cmd/cli 注入进程 stderr；e2e 注入缓冲以断言输出内容。
	Err io.Writer
}

// Run 执行一次导入。err 非 nil 为致命错误（退出码在 Error.Code）；
// err 为 nil 时 exitCode 给出结果退出码，summary 是计数汇总。
func Run(ctx context.Context, opts Options) (summary map[string]any, exitCode int, err error) {
	if opts.Dir == "" || opts.OutDir == "" {
		return nil, 2, fatal(2, "--dir and --out are required")
	}
	rows, err := readExport(opts.Dir)
	if err != nil {
		return nil, 2, err
	}

	// 前置校验：学校与归属用户必须已存在（裁决 1、4）。不造学校、不猜归属。
	n, err := opts.DB.QueryInt64(ctx,
		`SELECT count(*) FROM schools WHERE id = $1::uuid`, opts.SchoolID)
	if err != nil {
		return nil, 2, fatal(2, "check school: %v", err)
	}
	if n == 0 {
		return nil, 2, fatal(2, "school %s does not exist in the new database; create it first", opts.SchoolID)
	}
	n, err = opts.DB.QueryInt64(ctx,
		`SELECT count(*) FROM users WHERE id = $1::uuid AND deleted_at IS NULL`, opts.OwnerUserID)
	if err != nil {
		return nil, 2, fatal(2, "check owner user: %v", err)
	}
	if n == 0 {
		return nil, 2, fatal(2, "owner user %s does not exist in the new database; create it first", opts.OwnerUserID)
	}

	// 孤儿口径（裁决 2）：time_prefs.phone 无对应账号。strict 即失败；默认跳过＋计数。
	accountPhones := map[string]bool{}
	for _, row := range rows["accounts"] {
		accountPhones[valueString(row["phone"])] = true
	}
	orphanRows := 0
	for _, row := range rows["time_prefs"] {
		if !accountPhones[valueString(row["phone"])] {
			orphanRows++
		}
	}
	if opts.Strict && orphanRows > 0 {
		return nil, 1, fatal(1, "%d orphan time_prefs rows (phone without account); refusing in --strict", orphanRows)
	}

	if opts.Sealer == nil {
		return nil, 2, fatal(2, "credential sealer is not wired")
	}

	state := newState(orphanRows)
	state.sealer = opts.Sealer
	state.err = opts.Err
	if state.err == nil {
		state.err = os.Stderr
	}
	// 全部写与对账比对同在一个事务内：提交前比对不一致即回滚，
	// 「exit 2 零写入」由事务保证，不靠事后补偿。
	txErr := opts.DB.InTx(ctx, func(ctx context.Context) error {
		for _, row := range rows["users"] {
			state.importUser(ctx, opts, row)
		}
		for _, row := range rows["accounts"] {
			state.importAccount(ctx, opts, row)
		}
		// time_prefs / audit 不入库（裁决见包注释），但行进对账表：
		// 与导出物对账表逐行比对才能钉住导出物完整性。
		for _, row := range rows["time_prefs"] {
			state.appendRecon("time_prefs", row, coremask.MaskPhone(valueString(row["phone"])))
		}
		for _, row := range rows["audit"] {
			state.appendRecon("audit", row, valueString(row["id"]))
		}
		// 行级错误行不入对账表；此时与导出物的全量比对不成立，
		// 跳过比对并在 summary 留痕（row_errors 已是显性计数）。
		if state.rowErrors == 0 {
			return compareRecon(opts.Dir, state.reconLines)
		}
		state.summary["recon_compare"] = "not compared: row errors present"
		return nil
	})
	if txErr != nil {
		var fatalErr *Error
		if errors.As(txErr, &fatalErr) {
			return nil, fatalErr.Code, fatalErr
		}
		return nil, 1, fatal(1, "import transaction failed: %v", txErr)
	}

	if err := writeReports(opts.OutDir, state); err != nil {
		return nil, 2, err
	}
	exitCode = 0
	if state.rowErrors > 0 {
		exitCode = 1
	}
	return state.summary, exitCode, nil
}

// newState 建计数状态。嵌套 map 的键集合就是 summary 的输出形状。
func newState(orphanRows int) *importState {
	return &importState{
		summary: map[string]any{
			"format":              "yiban-legacy-import/1",
			"mask_rule":           maskRuleText,
			"recon_hash":          reconHashText,
			"rows_read":           map[string]any{"users": 0, "accounts": 0, "time_prefs": 0, "audit": 0},
			"rows_imported":       map[string]any{"users": 0, "accounts": 0},
			"already_imported":    map[string]any{"users": 0, "accounts": 0},
			"row_errors":          map[string]any{"users": 0, "accounts": 0},
			"role_mapping":        map[string]any{},
			"status_mapping":      map[string]any{},
			"owner_values":        map[string]any{},
			"credentials":         map[string]any{"created": 0, "missing": 0, "decrypt_failed": 0, "phone_code_dropped": 0},
			"orphan_time_prefs":   orphanRows,
			"time_prefs_imported": 0,
			"time_prefs_note": "not imported: legacy prefs attach to phone (per account); the new stack " +
				"attaches one offset per user; no trusted phone->user map exists, rows would collapse onto one user. " +
				"Value semantics are equivalent (slot_min == offset_min, 5-minute grain); the attach point is not.",
			"audit_imported":  0,
			"audit_note":      "legacy audit chain is not appended to the new append-only chain; evidence stays in the export (audit.jsonl)",
			"columns_dropped": "accounts.name, accounts.sort_order: the new schema has no counterpart columns",
		},
	}
}

// importState 是一次导入内的可变状态。全部写入发生在单事务闭包内。
type importState struct {
	sealer     Sealer
	err        io.Writer // 行级错误输出端（Run 里从 Options.Err 装配）
	summary    map[string]any
	reconLines []string
	rowErrors  int
}

func (s *importState) bump(group, key string) {
	m := s.summary[group].(map[string]any)
	if n, ok := m[key].(int); ok {
		m[key] = n + 1
		return
	}
	m[key] = 1
}

func (s *importState) bumpRead(table string) {
	s.summary["rows_read"].(map[string]any)[table] =
		s.summary["rows_read"].(map[string]any)[table].(int) + 1
}

func (s *importState) addRowError(group, what string, err error) {
	s.rowErrors++
	s.bump("row_errors", group)
	fmt.Fprintf(s.err, "migrate-legacy: row error in %s (%s): %v\n", group, what, err)
}

func (s *importState) appendRecon(table string, row map[string]any, key string) {
	s.reconLines = append(s.reconLines, CanonicalLine(map[string]any{
		"table": table, "key": key, "hash": CanonicalHash(table, key, row),
	}))
}

// importUser 重建一行旧 users。幂等判据＝legacy_ids。
// 写段（主行＋映射）在保存点内同存亡：任一失败整行回滚，
// 落地行必有 legacy_ids 映射。
func (s *importState) importUser(ctx context.Context, opts Options, row map[string]any) {
	s.bumpRead("users")
	oldID, ok := valueInt(row["id"])
	if !ok {
		s.addRowError("users", "id", fmt.Errorf("missing or non-numeric id"))
		return
	}
	newID, hit, err := legacyIDHit(ctx, opts.DB, "users", oldID)
	if err != nil {
		s.addRowError("users", "legacy_ids lookup", err)
		return
	}
	if hit {
		exists, err := opts.DB.QueryInt64(ctx, `SELECT count(*) FROM users WHERE id = $1::uuid`, newID)
		if err != nil {
			s.addRowError("users", "already-imported check", err)
			return
		}
		if exists == 0 {
			s.addRowError("users", "legacy_ids target", fmt.Errorf("user %d maps to %s but the row is gone", oldID, newID))
			return
		}
		s.bump("already_imported", "users")
		s.appendRecon("users", row, fmt.Sprint(oldID))
		return
	}

	oldRole := valueString(row["role"])
	role, ok := mapLegacyRole(oldRole)
	if !ok {
		s.addRowError("users", "role", fmt.Errorf("user %d has unknown role %q; not guessing", oldID, oldRole))
		return
	}
	s.bump("role_mapping", oldRole+"->"+role)

	email := coreuser.NormalizeEmail(valueString(row["email"]))
	createdAt, err := parseLegacyTime(valueString(row["created_at"]), opts.Now)
	if err != nil {
		s.addRowError("users", "created_at", fmt.Errorf("user %d: %v", oldID, err))
		return
	}
	var deletedAt *time.Time
	if valueIntOr(row["deleted"]) == 1 {
		at, err := parseLegacyTime(valueString(row["deleted_at"]), opts.Now)
		if err != nil {
			s.addRowError("users", "deleted_at", fmt.Errorf("user %d: %v", oldID, err))
			return
		}
		deletedAt = &at
	}

	txErr := opts.DB.InSavepoint(ctx, func(ctx context.Context) error {
		createdID, err := opts.DB.QueryString(ctx,
			`INSERT INTO users (email, password_hash, role, created_at, deleted_at)
			 VALUES ($1, $2, $3, $4, $5) RETURNING id::text`,
			email, valueString(row["password_hash"]), role, createdAt, deletedAt)
		if err != nil {
			return err
		}
		if _, err := opts.DB.Exec(ctx,
			`INSERT INTO legacy_ids (old_table, old_id, new_id) VALUES ('users', $1, $2::uuid)`,
			oldID, createdID); err != nil {
			return err
		}
		return nil
	})
	if txErr != nil {
		s.addRowError("users", "insert", fmt.Errorf("user %d (%s): %v", oldID, email, txErr))
		return
	}
	s.bump("rows_imported", "users")
	s.appendRecon("users", row, fmt.Sprint(oldID))
}

// mapLegacyRole 是角色映射表（裁决 3）：admin→master、user→user。
// 旧库无 school 级 admin，不产生 admin_schools 行。
func mapLegacyRole(old string) (string, bool) {
	switch old {
	case "admin":
		return "master", true
	case "user":
		return "user", true
	default:
		return "", false
	}
}

// importAccount 重建一行旧 accounts 与其凭据。幂等判据＝legacy_ids。
// 写段（主行＋凭据＋映射）在保存点内同存亡：任一失败整行回滚，
// 落地行必有 legacy_ids 映射，不留无凭据的半行（凭据缺行/解密失败除外，
// 见 importCredential——那是裁决 5 的「账号照建」语义）。
func (s *importState) importAccount(ctx context.Context, opts Options, row map[string]any) {
	s.bumpRead("accounts")
	oldID, ok := valueInt(row["id"])
	if !ok {
		s.addRowError("accounts", "id", fmt.Errorf("missing or non-numeric id"))
		return
	}
	// 旧 owner 是自由文本（多数为 'admin'），不做可信映射，只计数（裁决 4）。
	s.bump("owner_values", orEmpty(valueString(row["owner"])))

	newID, hit, err := legacyIDHit(ctx, opts.DB, "accounts", oldID)
	if err != nil {
		s.addRowError("accounts", "legacy_ids lookup", err)
		return
	}
	if hit {
		exists, err := opts.DB.QueryInt64(ctx, `SELECT count(*) FROM accounts WHERE id = $1::uuid`, newID)
		if err != nil {
			s.addRowError("accounts", "already-imported check", err)
			return
		}
		if exists == 0 {
			s.addRowError("accounts", "legacy_ids target", fmt.Errorf("account %d maps to %s but the row is gone", oldID, newID))
			return
		}
		s.bump("already_imported", "accounts")
		s.appendRecon("accounts", row, fmt.Sprint(oldID))
		return
	}

	phone := valueString(row["phone"])
	newStatus, ok := mapLegacyStatus(row)
	if !ok {
		s.addRowError("accounts", "status", fmt.Errorf("account %d has unknown status %q; not guessing", oldID, valueString(row["status"])))
		return
	}
	var deletedAt *time.Time
	if valueIntOr(row["deleted"]) == 1 {
		at, err := parseLegacyTime(valueString(row["deleted_at"]), opts.Now)
		if err != nil {
			s.addRowError("accounts", "deleted_at", fmt.Errorf("account %d: %v", oldID, err))
			return
		}
		deletedAt = &at
	}

	txErr := opts.DB.InSavepoint(ctx, func(ctx context.Context) error {
		createdID, err := opts.DB.QueryString(ctx,
			`INSERT INTO accounts (school_id, owner_user_id, phone, status, reject_reason, deleted_at)
			 VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6) RETURNING id::text`,
			opts.SchoolID, opts.OwnerUserID, phone, newStatus,
			valueString(row["reject_reason"]), deletedAt)
		if err != nil {
			return err
		}
		if err := s.importCredential(ctx, opts, row, phone, createdID); err != nil {
			return err
		}
		if _, err := opts.DB.Exec(ctx,
			`INSERT INTO legacy_ids (old_table, old_id, new_id) VALUES ('accounts', $1, $2::uuid)`,
			oldID, createdID); err != nil {
			return err
		}
		return nil
	})
	if txErr != nil {
		s.addRowError("accounts", "insert", fmt.Errorf("account %d (%s): %v", oldID, coremask.MaskPhone(phone), txErr))
		return
	}
	s.bump("rows_imported", "accounts")
	s.appendRecon("accounts", row, fmt.Sprint(oldID))
}

// mapLegacyStatus 是状态映射表。
//
// 考古（2026-10-27，web/services/accounts_data.py 与 yiban/adapters/persistence/
// accounts.py）：旧 status ∈ {pending, active, rejected}，另 user_paused 0/1 是
// 独立列（用户自暂停）。新栈词表（core/account）：pending/active/rejected/
// paused_admin/paused_user/credential_error。
//
// 映射：
//
//	pending  → pending            （两侧同词；经由 coreaccount 常量比对）
//	active   → active
//	active   → paused_user        （user_paused=1：用户自暂停）
//	rejected → rejected           （reject_reason 随行平移）
//	deleted=1 → 同时写 deleted_at （软删是独立维度，不改状态词）
//
// 未知 status 不猜：显性行错误。pending_user_paused 不折叠：pending 本就不进
// 候选集，user_paused 信息在旧栈亦不改变 pending 的行为。
func mapLegacyStatus(row map[string]any) (string, bool) {
	old := coreaccount.Status(valueString(row["status"]))
	switch old {
	case coreaccount.StatusPending:
		return string(coreaccount.StatusPending), true
	case coreaccount.StatusRejected:
		return string(coreaccount.StatusRejected), true
	case coreaccount.StatusActive:
		if valueIntOr(row["user_paused"]) == 1 {
			return string(coreaccount.StatusPausedUser), true
		}
		return string(coreaccount.StatusActive), true
	default:
		return "", false
	}
}

// importCredential 重建一行凭据：旧信封解密（AAD=手机号）→ 新负载（三键
// 全出现，device_code 置空串）→ 新信封（AAD=school_id|account_id）。
// 明文只在内存。
//
// 返回 nil：凭据缺行（口令空）与解密失败（裁决 5）——账号照建、
// 凭据行缺省、显性计数，不撤行。
// 返回非 nil：信封形状违约（裸串、意外类型）与封装/插入失败——
// 调用方整行回滚，不留无凭据的半行。
// 错误文本一律用掩码手机号，不透出明文。
func (s *importState) importCredential(ctx context.Context, opts Options, row map[string]any, phone, accountID string) error {
	masked := coremask.MaskPhone(phone)
	switch pw := row["password"].(type) {
	case nil:
		s.bump("credentials", "missing")
	case string:
		if pw == "" {
			s.bump("credentials", "missing")
			return nil
		}
		// 导出器契约保证非空必为信封对象；裸串是契约违约，整行拒绝。
		return fmt.Errorf("account %s: password field is a bare string (contract violation)", masked)
	case map[string]any:
		plain, err := LegacyOpenEnvelope(pw, phone, opts.LegacyKey)
		if err != nil {
			s.addRowError("accounts", "credential decrypt", fmt.Errorf("account %s: %v", masked, err))
			s.bump("credentials", "decrypt_failed")
			return nil
		}
		cred, err := s.sealer.Seal(opts.SchoolID, accountID, CredentialPayload{
			DeviceCode: "",
			Password:   plain,
			PhoneModel: valueString(row["phone_model"]),
		})
		if err != nil {
			return fmt.Errorf("account %s: seal credential: %v", masked, err)
		}
		if _, err := opts.DB.Exec(ctx,
			`INSERT INTO credentials (account_id, school_id, alg, kid, aad_version, nonce, ciphertext, version)
			 VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, 1)`,
			accountID, opts.SchoolID, cred.Alg, cred.KID, cred.AADVersion, cred.Nonce, cred.Ciphertext); err != nil {
			return fmt.Errorf("account %s: insert credential: %v", masked, err)
		}
		s.bump("credentials", "created")
	default:
		return fmt.Errorf("account %s: password field has unexpected type %T", masked, row["password"])
	}
	// 旧 phone_code（验证码信封）在新负载无对应键，弃置并显性计数（裁决 7）。
	if _, isEnvelope := row["phone_code"].(map[string]any); isEnvelope {
		s.bump("credentials", "phone_code_dropped")
	}
	return nil
}

// legacyIDHit 查 legacy_ids；命中返回新 id。查无此行是「未导入过」，不是错误。
func legacyIDHit(ctx context.Context, db *pg.DB, table string, oldID int64) (string, bool, error) {
	id, err := db.QueryString(ctx,
		`SELECT new_id::text FROM legacy_ids WHERE old_table = $1 AND old_id = $2`,
		table, oldID)
	if err != nil {
		if errs.From(err).Code == errs.CodeNotFound {
			return "", false, nil
		}
		return "", false, err
	}
	return id, id != "", nil
}

// legacyTZ 是旧库朴素时间戳的解读时区。旧栈把本地墙钟写进 TEXT 列，
// 服务器默认时区 Asia/Shanghai（1991 年后无夏令时，固定 +8 等价）。
var legacyTZ = time.FixedZone("legacy-0800", 8*3600)

// parseLegacyTime 解旧库时间戳：RFC3339 原样；无时区的朴素串按旧栈本地时区
// 解读后转 UTC；空串按迁移时刻。
func parseLegacyTime(s string, now time.Time) (time.Time, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return now, nil
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t.UTC(), nil
	}
	if t, err := time.ParseInLocation("2006-01-02T15:04:05", s, legacyTZ); err == nil {
		return t.UTC(), nil
	}
	return time.Time{}, fmt.Errorf("unparsable timestamp %q", s)
}

func valueIntOr(v any) int64 {
	n, ok := valueInt(v)
	if !ok {
		return 0
	}
	return n
}

func orEmpty(s string) string {
	if s == "" {
		return "<empty>"
	}
	return s
}

// readExport 读入五个 .jsonl；schools 必须为空（旧库无学校表，非空即契约违约）。
func readExport(dir string) (map[string][]map[string]any, error) {
	rows := map[string][]map[string]any{}
	for _, table := range []string{"users", "accounts", "time_prefs", "audit", "schools"} {
		path := filepath.Join(dir, table+".jsonl")
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, fatal(2, "read export: %v", err)
		}
		parsed, err := parseJSONL(raw)
		if err != nil {
			return nil, fatal(2, "parse %s.jsonl: %v", table, err)
		}
		if table == "schools" && len(parsed) > 0 {
			return nil, fatal(2, "schools.jsonl must be empty (the legacy schema has no schools table); got %d rows", len(parsed))
		}
		rows[table] = parsed
	}
	return rows, nil
}

func parseJSONL(raw []byte) ([]map[string]any, error) {
	var out []map[string]any
	for i, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		dec := json.NewDecoder(strings.NewReader(line))
		dec.UseNumber()
		var row map[string]any
		if err := dec.Decode(&row); err != nil {
			return nil, fmt.Errorf("line %d: %w", i+1, err)
		}
		out = append(out, row)
	}
	return out, nil
}

// compareRecon 把导入器算出的对账表与导出物里的那份逐行比对。
// 两表同口径（canonical.go），全量一致才放行；不一致即导出物被改或残缺。
func compareRecon(dir string, computed []string) error {
	raw, err := os.ReadFile(filepath.Join(dir, "reconciliation.jsonl"))
	if err != nil {
		return fatal(2, "read export reconciliation: %v", err)
	}
	want := map[string]string{}
	for i, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		dec := json.NewDecoder(strings.NewReader(line))
		dec.UseNumber()
		var row map[string]any
		if err := dec.Decode(&row); err != nil {
			return fatal(2, "export reconciliation line %d: %v", i+1, err)
		}
		want[valueString(row["table"])+unitSep+valueString(row["key"])] = valueString(row["hash"])
	}
	got := map[string]string{}
	for _, line := range computed {
		dec := json.NewDecoder(strings.NewReader(line))
		dec.UseNumber()
		var row map[string]any
		if err := dec.Decode(&row); err != nil {
			return fatal(2, "computed reconciliation line: %v", err)
		}
		got[valueString(row["table"])+unitSep+valueString(row["key"])] = valueString(row["hash"])
	}
	if len(want) != len(got) {
		return fatal(2, "reconciliation row count mismatch: export %d, imported %d", len(want), len(got))
	}
	keys := make([]string, 0, len(want))
	for k := range want {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		g, ok := got[k]
		if !ok {
			return fatal(2, "reconciliation key %q missing from import", strings.ReplaceAll(k, unitSep, "/"))
		}
		if g != want[k] {
			return fatal(2, "reconciliation hash mismatch for key %q", strings.ReplaceAll(k, unitSep, "/"))
		}
	}
	return nil
}

// writeReports 把对账表与 summary 写进输出目录（0600）。
func writeReports(outDir string, state *importState) error {
	if err := os.MkdirAll(outDir, 0o700); err != nil {
		return fatal(2, "create output dir: %v", err)
	}
	var sb strings.Builder
	for _, line := range state.reconLines {
		sb.WriteString(line)
		sb.WriteString("\n")
	}
	if err := os.WriteFile(filepath.Join(outDir, "reconciliation.jsonl"), []byte(sb.String()), 0o600); err != nil {
		return fatal(2, "write reconciliation: %v", err)
	}
	if err := os.WriteFile(filepath.Join(outDir, "summary.json"), []byte(CanonicalLine(state.summary)+"\n"), 0o600); err != nil {
		return fatal(2, "write summary: %v", err)
	}
	return nil
}

const (
	maskRuleText = "mask_phone: 11-digit CN mobile -> first3 + '****' + last4; " +
		"already-masked or non-phone values unchanged"
	reconHashText = "sha256 hex of canonical string: table + 0x1f + key + 0x1f " +
		"+ 0x1e-joined 'k=v' over all exported fields sorted by key; " +
		"phone values masked before hashing; string values verbatim, " +
		"non-string values as sort_keys compact JSON"
)
