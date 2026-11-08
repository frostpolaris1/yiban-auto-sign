// Package user 声明用户仓储端口，并实现用户管理用例。
//
// # 功能
// 声明用户仓储接口与名下账号计数端口。建号见 create.go；
// 读面见 list.go 与 lookup.go；改角色见 update.go；
// 重置口令见 password_reset.go；软删见 delete.go；恢复见 restore.go。
// 本包**不**实现 SQL、**不**做 HTTP 编码、**不**签发会话。
//
// # 归属
// app 层端口与用例。Record 与 New 留在 core/user，
// 这样 PostgreSQL 适配器无需导入本包（canonical.tsv 的 boundary.adapters）。
// 别名让端口可读，并保证两侧命名同一类型。
//
// # 复用
// Repo、OwnerAccountCounter；类型别名 Record、New；
// create.go 的 Service、Deps、CreateInput。
//
// # 口径
// 用户上限数是活跃用户数（deleted_at IS NULL）。
// 邮箱比对一律小写去空白，实现取自 core/user。
package user

import (
	"context"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// Record 是一行 users 记录。
type Record = coreuser.Record

// New 是创建用户的输入。
type New = coreuser.New

// Repo 是用户仓储端口。
//
// Create 执行活跃用户上限：**必须在事务内运行**，
// 先取用户容量 advisory lock 再计数。
// limit 是配置的有效用户上限。
// List 只返回未删除的行，并按 (created_at DESC, id DESC) 分页；
// includeDeleted 置真时软删行也入选。
// Restore 只恢复软删行，并在锁下重数活跃上限：**必须在事务内运行**。
// CountLiveMasters 在锁下统计 active 未删 master（status='active'）：
// **必须在事务内运行**，与 Create、Restore、Delete 同锁族，
// 串行化并发的建号、降级、删除与恢复。停用 master 不能登录，
// 不构成恢复路径，不计入。
// Lookup 按已归一邮箱前缀取未删行，按 email 升序取前 limit 条。
// exact 置真时 query 是完整邮箱：只回全等的那一条（F1，
// 归属选择器按 id 绑定必须用它——掩码行彼此不可分辨）。
//
// GetDeleted 锁定并读取一个用户行（软删与活行都返回），
// **必须在事务内运行**：FOR UPDATE 与恢复互斥。
// 零行分类：不存在报 not_found；活行报 conflict。
// 物理清除用它拿目标行，不能用排除软删行的 GetByID。
//
// FindDeletedByEmail 返回该邮箱最近一条软删行（deleted_at DESC, id DESC），
// 不加锁、不要求事务：恢复入口先用它取哈希与 deleted_at 做宽限判断，
// 付 KDF 成本之前不持行锁。零行报 not_found。
// 同邮箱可同时存在活跃行（uq_users_email_live 只约束活跃行），
// 调用方把「活跃行占用邮箱」「不存在」「超窗」同判统一失败，不泄露存在性。
//
// LockEmail 取邮箱维度的 advisory lock，**必须在事务内运行**。
// 公开注册的自助注销宽限复核与 SoftDeleteSelf 共用同一把锁，
// 锁在事务结束才释放，故「注销未提交时注册」只能等待。
// HasSelfDeletedEmailWithin 在锁内用 EXISTS 扫**全部**同邮箱软删行，
// 判断是否存在 deleted_by=本人且 deleted_at >= notBefore 的行；
// 不能只看最近一条（最近一条可能是管理员删除）。**必须在事务内运行**，
// 且调用方先 LockEmail。
//
// SoftDeleteSelf 在同一事务内先取邮箱锁，再软删用户与其名下全部未删账号，
// 返回连带软删的账号数。用户行零行报 not_found。
// 会话不在本方法内撤销：调用方在同一事务里调用会话端口。
//
// RestoreSelfUser 在宽限期与活跃用户容量判据下恢复一条软删用户行。
// **必须在事务内运行**，且调用方先用 GetDeleted 锁行并复核 deleted_at；
// 零行（宽限期外或已恢复）报 not_found，容量满报 capacity。
//
// RestoreSelfAccount 恢复该用户注销时刻仍生效的那一行账号：
// 只认 deleted_by=ownerID（管理员删除的行不复活）且 deleted_at 与用户行
// 同一时间戳，故至多一行；并同样受活跃账号容量约束。
// 无候选返回 (false, nil)；有候选但容量满返回 capacity。
// **必须在事务内运行**。
//
// Purge 物理删除用户行与名下账号及从属行，**必须在事务内运行**；
// 调用方先用 GetDeleted 锁行并校验软删状态。
type Repo interface {
	Create(ctx context.Context, in New, limit int) (Record, error)
	GetByID(ctx context.Context, id string) (Record, error)
	GetDeleted(ctx context.Context, id string) (Record, error)
	GetByEmail(ctx context.Context, email string) (Record, error)
	FindDeletedByEmail(ctx context.Context, email string) (Record, error)
	// LockEmail 取邮箱维度 advisory lock；**必须在事务内运行**。
	LockEmail(ctx context.Context, email string) error
	// HasSelfDeletedEmailWithin 在锁内扫全部同邮箱软删行；
	// **必须在事务内运行**，调用方先 LockEmail。
	HasSelfDeletedEmailWithin(ctx context.Context, email string, notBefore time.Time) (bool, error)
	List(ctx context.Context, role coreauth.Role, status coreauth.Status, query string, group coreuser.Group, cursor string, limit int, includeDeleted bool) ([]Record, string, error)
	// Counts 返回四个分组的全局计数；不随筛选或分页变化。
	Counts(ctx context.Context) (coreuser.Counts, error)
	// OwnerCounts 按行聚合某用户名下未软删账号的三计数，
	// 口径与列表的账号聚合列一致。无账号返回零值。
	// Update 与 Restore 的行扫描不含聚合列，用例层用它补齐响应。
	OwnerCounts(ctx context.Context, id string) (coreuser.OwnerCounts, error)
	UpdatePassword(ctx context.Context, id, passwordHash string, mustChange bool, at time.Time) error
	// RehashPassword 只替换哈希，**不改 password_changed_at**。
	// 登录时的透明升级走本方法：口令没有变，改密时刻不得前移。
	RehashPassword(ctx context.Context, id, passwordHash string) error
	UpdateRole(ctx context.Context, id string, role coreauth.Role, at time.Time) error
	SetStatus(ctx context.Context, id string, status coreauth.Status, at time.Time) error
	MarkLogin(ctx context.Context, id string, at time.Time) error
	// MailNotify 读未删用户的签到失败邮件开关；默认 true。
	// SetMailNotify 写开关；未删行才更新，零行报 not_found。
	MailNotify(ctx context.Context, id string) (bool, error)
	SetMailNotify(ctx context.Context, id string, enabled bool) error
	SoftDelete(ctx context.Context, id, by string, at time.Time) error
	// SoftDeleteSelf 软删用户与名下全部未删账号；**必须在事务内运行**。
	SoftDeleteSelf(ctx context.Context, id string, at time.Time) (int, error)
	// RestoreSelfUser 在宽限与容量判据下恢复软删用户；**必须在事务内运行**。
	RestoreSelfUser(ctx context.Context, id string, notBefore time.Time, limit int) (Record, error)
	// RestoreSelfAccount 恢复注销时刻仍生效的那一行账号；
	// **必须在事务内运行**。无候选返回 false。
	RestoreSelfAccount(ctx context.Context, ownerID string, deletedAt time.Time, limit int) (bool, error)
	Restore(ctx context.Context, id string, limit int) (Record, error)
	// Purge 物理删除用户行与名下账号及从属行
	// （任务行先分块删，其余随外键级联或成对置空）。
	// 只处理已软删且已锁定的行；审计行不动。
	Purge(ctx context.Context, id string) error
	CountLiveMasters(ctx context.Context, excludeID string) (int, error)
	Lookup(ctx context.Context, query string, limit int, exact bool) ([]Record, error)
}

// OwnerAccountCounter 是名下活跃账号计数端口。
//
// 软删用户的判据需要数 accounts 表；账号用例的类型留在 account 包，
// 故这里只声明一个窄端口，由 assembly 注入实现。
// 用户包不 import 账号包。
type OwnerAccountCounter interface {
	CountLiveAccountsByOwner(ctx context.Context, userID string) (int, error)
}
