package legacy

// time_prefs 补录：旧库按手机号的自选 → 新库按 user_id 的自选（2gk）。
//
// # 功能
// 读 P8-A 导出物里的 accounts.jsonl 与 time_prefs.jsonl，
// 沿 legacy_ids('accounts', 旧账号 id) 找到新账号行，
// 再取该行的 owner_user_id 作为写入目标。
// 默认 dry-run：只输出计划，不发写调用；--apply 才在单事务内 UPSERT。
//
// # 归属
// 一次性迁移件，只服务 `cmd/cli backfill-time-prefs`。
// SQL 直写与导入器同属本包（adapters/pg 的包注释登记的唯一例外）。
//
// # 输入
// 只读导出物，不读 SQLite：导出物已含补录全部列。
// Go 侧因此不引入 SQLite 驱动，零新依赖。
//
// # 口径
// 映射是 1:1：旧账号 → 新账号 → 归属用户。
// 同一用户多账号取值一致才写；不一致整用户跳过，报告列出掩码手机号。
// 旧账号或归属用户已软删、旧账号无 legacy_ids 映射，均跳过并计数。
// offset 取值按 core/schedule 的边界：0 <= offset < 1440 且为 5 的倍数。
// 写入幂等：同值重跑零变更（条件 UPSERT 的 WHERE 判据）。
// 报告零明文手机号，一律 core/mask 掩码。

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// 跳过原因。报告逐条点名，值稳定（调用方按串分支）。
const (
	// SkipMissingPhone：旧行 phone 为空串。
	SkipMissingPhone = "missing_phone"
	// SkipSlotNotInteger：slot_min 缺失或不是整数。
	SkipSlotNotInteger = "slot_min_not_integer"
	// SkipSlotOutOfRange：slot_min 不在 [0, 1440)。
	SkipSlotOutOfRange = "slot_min_out_of_range"
	// SkipSlotGrain：slot_min 不是 5 的倍数。
	SkipSlotGrain = "slot_min_not_multiple_of_5"
	// SkipDuplicateInput：同一 phone 在 time_prefs.jsonl 出现多次。
	SkipDuplicateInput = "duplicate_input_phone"
	// SkipNoAccount：time_prefs.phone 无对应 accounts 行（旧库孤儿）。
	SkipNoAccount = "phone_without_account"
	// SkipNoMapping：旧账号无 legacy_ids 映射（未导入过）。
	SkipNoMapping = "account_without_legacy_mapping"
	// SkipInactive：落地账号或归属用户已软删，或账号行缺失。
	SkipInactive = "account_or_user_inactive"
)

// ResolveStatus 是旧账号归属解析结果。
type ResolveStatus string

const (
	// ResolveOK：解析到未删除的归属用户。
	ResolveOK ResolveStatus = "ok"
	// ResolveUnmapped：旧账号没有 legacy_ids 映射。
	ResolveUnmapped ResolveStatus = "unmapped"
	// ResolveInactive：映射存在，但落地账号或归属用户已软删（或已不存在）。
	ResolveInactive ResolveStatus = "inactive"
)

// OwnerRef 是一次归属解析的结果。
type OwnerRef struct {
	// UserID 仅在 Status 为 ResolveOK 时有值。
	UserID string
	// Status 说明解析结果。
	Status ResolveStatus
}

// PrefResolver 把旧账号 id 解析成新栈归属用户。
type PrefResolver interface {
	// ResolveOwner 解析一个旧账号 id。失败返回 error；无归属用 Status 表达。
	ResolveOwner(ctx context.Context, oldAccountID int64) (OwnerRef, error)
}

// PrefWrite 是一条待写记录。
type PrefWrite struct {
	// UserID 是新栈 users.id（uuid 文本）。
	UserID string `json:"user_id"`
	// OffsetMin 是写入值：相对有效窗口起点的分钟数。
	OffsetMin int `json:"offset_min"`
	// Accounts 是该用户本次取值的来源账号（掩码手机号，供人核对）。
	Accounts []string `json:"accounts"`
}

// PrefSkip 是一条未写入的旧行及原因。
type PrefSkip struct {
	// Phone 是掩码手机号；源行 phone 为空串时为空串。
	Phone string `json:"phone"`
	// Reason 是 Skip* 常量之一。
	Reason string `json:"reason"`
	// Detail 是可供核对的补充文本（如非法的 slot 原值）。不含个人数据。
	Detail string `json:"detail,omitempty"`
}

// PrefConflict 是同一用户多个旧账号取值不一致的记录。
type PrefConflict struct {
	// UserID 是冲突的归属用户。
	UserID string `json:"user_id"`
	// Offsets 是全部不同取值，升序。
	Offsets []int `json:"offsets"`
	// Accounts 是全部来源账号，掩码手机号，升序。
	Accounts []string `json:"accounts"`
}

// BackfillReport 是一次补录的报告。dry-run 与 --apply 同形。
type BackfillReport struct {
	// Format 是报告版本标识。
	Format string `json:"format"`
	// Apply 为真表示本次真正写库。
	Apply bool `json:"apply"`
	// InputRows 是 time_prefs.jsonl 的行数。
	InputRows int `json:"input_rows"`
	// AccountsRead 是 accounts.jsonl 的行数。
	AccountsRead int `json:"accounts_read"`
	// Writes 是写入计划（dry-run）或已提交的写入项（--apply）。
	Writes []PrefWrite `json:"writes"`
	// Conflicts 是被跳过的冲突用户。
	Conflicts []PrefConflict `json:"conflicts"`
	// Skips 是被跳过的旧行。
	Skips []PrefSkip `json:"skips"`
	// RowsWritten 是数据库实际变更的行数；dry-run 恒为 0。
	RowsWritten int64 `json:"rows_written"`
}

// reportFormat 是报告版本标识的值。
const reportFormat = "yiban-time-pref-backfill/1"

// PrefWriter 幂等写用户自选。
type PrefWriter interface {
	// UpsertOffsets 在一个事务内写全部条目，返回实际变更行数。
	// 任一失败必须回滚全部，不留半写。
	UpsertOffsets(ctx context.Context, writes []PrefWrite, now time.Time) (int64, error)
}

// BackfillOptions 是一次补录的全部输入。
type BackfillOptions struct {
	// Dir 是 P8-A 导出物目录（须含 accounts.jsonl 与 time_prefs.jsonl）。
	Dir string
	// Apply 为真才写库；假时只出计划。
	Apply bool
	// Resolver 解析旧账号的归属用户。
	Resolver PrefResolver
	// Writer 写新库。
	Writer PrefWriter
	// Now 是写入时间戳。零值即用法错误。
	Now time.Time
}

// fatalBackfill 构造带退出码的致命错误。前缀与子命令同名。
func fatalBackfill(code int, format string, args ...any) *Error {
	return &Error{Code: code, Msg: "backfill-time-prefs: " + fmt.Sprintf(format, args...)}
}

// Backfill 读导出物、算计划，并在 Apply 为真时写入。
// 返回的 error 为致命错误：*Error 携带退出码，其他为运行期失败。
func Backfill(ctx context.Context, opts BackfillOptions) (BackfillReport, error) {
	report := BackfillReport{
		Format: reportFormat, Apply: opts.Apply,
		Writes: []PrefWrite{}, Conflicts: []PrefConflict{}, Skips: []PrefSkip{},
	}
	if opts.Dir == "" {
		return report, fatalBackfill(2, "--dir is required")
	}
	if opts.Resolver == nil || opts.Writer == nil {
		return report, fatalBackfill(2, "resolver and writer are required")
	}
	if opts.Now.IsZero() {
		return report, fatalBackfill(2, "Now is required")
	}
	accounts, prefs, err := readBackfillInput(opts.Dir)
	if err != nil {
		return report, err
	}
	report.InputRows = len(prefs)
	report.AccountsRead = len(accounts)

	// 旧 phone → 旧账号 id。旧库 phone 是 UNIQUE，重复即导出物异常。
	accountByPhone := make(map[string]int64, len(accounts))
	duplicatePhones := map[string]bool{}
	for _, row := range accounts {
		phone := valueString(row["phone"])
		if phone == "" {
			continue
		}
		if _, seen := accountByPhone[phone]; seen {
			duplicatePhones[phone] = true
			continue
		}
		oldID, ok := valueInt(row["id"])
		if !ok {
			// 无 id 的账号行无法参与映射。放过去会把它的自选误报成孤儿。
			return report, fatalBackfill(2,
				"accounts.jsonl has a row without a numeric id; the export is malformed")
		}
		accountByPhone[phone] = oldID
	}
	// 旧库 phone 是 UNIQUE：重复即导出物违约，挑一行就是猜。
	if len(duplicatePhones) > 0 {
		return report, fatalBackfill(2,
			"accounts.jsonl has %d duplicate phone values; the legacy schema declares phone UNIQUE",
			len(duplicatePhones))
	}

	// 归属用户 → 取值 → 来源账号（掩码）。
	byUser := map[string]map[int][]string{}
	seenPhones := make(map[string]bool, len(prefs))
	for _, row := range prefs {
		phone := valueString(row["phone"])
		masked := ""
		if phone != "" {
			masked = coremask.MaskPhone(phone)
		}
		if phone == "" {
			report.Skips = append(report.Skips, PrefSkip{Reason: SkipMissingPhone})
			continue
		}
		if seenPhones[phone] {
			report.Skips = append(report.Skips, PrefSkip{Phone: masked, Reason: SkipDuplicateInput})
			continue
		}
		seenPhones[phone] = true
		offset, ok := valueInt(row["slot_min"])
		if !ok {
			report.Skips = append(report.Skips, PrefSkip{
				Phone: masked, Reason: SkipSlotNotInteger, Detail: valueString(row["slot_min"]),
			})
			continue
		}
		switch {
		case offset < 0 || offset >= int64(coreschedule.MaxOffsetMin):
			report.Skips = append(report.Skips, PrefSkip{
				Phone: masked, Reason: SkipSlotOutOfRange, Detail: fmt.Sprint(offset),
			})
			continue
		case offset%int64(coreschedule.GranularityMin) != 0:
			report.Skips = append(report.Skips, PrefSkip{
				Phone: masked, Reason: SkipSlotGrain, Detail: fmt.Sprint(offset),
			})
			continue
		}
		oldID, ok := accountByPhone[phone]
		if !ok {
			report.Skips = append(report.Skips, PrefSkip{Phone: masked, Reason: SkipNoAccount})
			continue
		}
		owner, err := opts.Resolver.ResolveOwner(ctx, oldID)
		if err != nil {
			return report, fmt.Errorf("resolve legacy account %d (%s): %w", oldID, masked, err)
		}
		switch owner.Status {
		case ResolveOK:
			offsets, ok := byUser[owner.UserID]
			if !ok {
				offsets = map[int][]string{}
				byUser[owner.UserID] = offsets
			}
			offsets[int(offset)] = append(offsets[int(offset)], masked)
		case ResolveUnmapped:
			report.Skips = append(report.Skips, PrefSkip{Phone: masked, Reason: SkipNoMapping})
		default:
			report.Skips = append(report.Skips, PrefSkip{Phone: masked, Reason: SkipInactive})
		}
	}

	userIDs := make([]string, 0, len(byUser))
	for userID := range byUser {
		userIDs = append(userIDs, userID)
	}
	sort.Strings(userIDs)
	for _, userID := range userIDs {
		offsets := byUser[userID]
		if len(offsets) > 1 {
			conflict := PrefConflict{UserID: userID}
			for offset, accounts := range offsets {
				conflict.Offsets = append(conflict.Offsets, offset)
				conflict.Accounts = append(conflict.Accounts, accounts...)
			}
			sort.Ints(conflict.Offsets)
			sort.Strings(conflict.Accounts)
			report.Conflicts = append(report.Conflicts, conflict)
			continue
		}
		for offset, accounts := range offsets {
			sort.Strings(accounts)
			report.Writes = append(report.Writes, PrefWrite{
				UserID: userID, OffsetMin: offset, Accounts: accounts,
			})
		}
	}

	if !opts.Apply || len(report.Writes) == 0 {
		return report, nil
	}
	written, err := opts.Writer.UpsertOffsets(ctx, report.Writes, opts.Now)
	if err != nil {
		return report, fmt.Errorf("write time_prefs: %w", err)
	}
	report.RowsWritten = written
	return report, nil
}

// readBackfillInput 只读补录需要的两张表。缺文件即用法错误（退出码 2）。
func readBackfillInput(dir string) (accounts, prefs []map[string]any, err error) {
	read := func(table string) ([]map[string]any, error) {
		raw, err := os.ReadFile(filepath.Join(dir, table+".jsonl"))
		if err != nil {
			return nil, fatalBackfill(2, "read export: %v", err)
		}
		rows, err := parseJSONL(raw)
		if err != nil {
			return nil, fatalBackfill(2, "parse %s.jsonl: %v", table, err)
		}
		return rows, nil
	}
	if accounts, err = read("accounts"); err != nil {
		return nil, nil, err
	}
	if prefs, err = read("time_prefs"); err != nil {
		return nil, nil, err
	}
	return accounts, prefs, nil
}

// OwnerResolver 用 legacy_ids 与 accounts 解析归属用户。
type OwnerResolver struct {
	db *pg.DB
}

// NewOwnerResolver 构造解析器。
func NewOwnerResolver(db *pg.DB) OwnerResolver { return OwnerResolver{db: db} }

// ResolveOwner 查 legacy_ids，再取未删账号的未删归属用户。
func (r OwnerResolver) ResolveOwner(ctx context.Context, oldAccountID int64) (OwnerRef, error) {
	newID, hit, err := legacyIDHit(ctx, r.db, "accounts", oldAccountID)
	if err != nil {
		return OwnerRef{}, err
	}
	if !hit {
		return OwnerRef{Status: ResolveUnmapped}, nil
	}
	userID, err := r.db.QueryString(ctx,
		`SELECT a.owner_user_id::text
		 FROM accounts a JOIN users u ON u.id = a.owner_user_id
		 WHERE a.id = $1::uuid AND a.deleted_at IS NULL AND u.deleted_at IS NULL`,
		newID)
	if err != nil {
		if errs.From(err).Code == errs.CodeNotFound {
			return OwnerRef{Status: ResolveInactive}, nil
		}
		return OwnerRef{}, err
	}
	return OwnerRef{UserID: userID, Status: ResolveOK}, nil
}

// TimePrefWriter 把补录值写进新栈 time_prefs。
type TimePrefWriter struct {
	db *pg.DB
}

// NewTimePrefWriter 构造写入器。
func NewTimePrefWriter(db *pg.DB) TimePrefWriter { return TimePrefWriter{db: db} }

// UpsertOffsets 在单事务内幂等写全部条目，返回实际变更行数。
// 同值重写不更新 updated_at：重跑零副作用，也不触发保存冷却。
func (w TimePrefWriter) UpsertOffsets(ctx context.Context, writes []PrefWrite, now time.Time) (int64, error) {
	if len(writes) == 0 {
		return 0, nil
	}
	var changed int64
	err := w.db.InTx(ctx, func(ctx context.Context) error {
		for _, item := range writes {
			n, err := w.db.Exec(ctx,
				`INSERT INTO time_prefs (user_id, offset_min, updated_at)
				 VALUES ($1::uuid, $2, $3)
				 ON CONFLICT (user_id) DO UPDATE
				 SET offset_min = EXCLUDED.offset_min, updated_at = EXCLUDED.updated_at
				 WHERE time_prefs.offset_min IS DISTINCT FROM EXCLUDED.offset_min`,
				item.UserID, item.OffsetMin, now)
			if err != nil {
				return fmt.Errorf("user %s: %w", item.UserID, err)
			}
			changed += n
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return changed, nil
}
