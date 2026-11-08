package pg

// Advisory lock 键。
// 固定数字跨重启、跨 PostgreSQL 版本稳定；hashtext() 不稳定。
//
// **锁序固定：容量在前，审计在后**。
// 反序会让两个各持一把锁的事务互相等待，形成死锁。
const (
	// AccountCapacityLock 守卫活跃账号计数。
	AccountCapacityLock int64 = 71010001
	// AuditChainLock 串行化链头读取与追加。
	AuditChainLock int64 = 71010002
	// UserCapacityLock 守卫活跃用户计数。
	UserCapacityLock int64 = 71010003
	// UserEmailLock 是邮箱维度的两参数 advisory lock 命名空间。
	// 第一键固定为本值，第二键是 hashtext(归一邮箱)。它与上面的
	// int8 锁处于不同命名空间，不会与容量锁互撞；同邮箱的
	// 自助注销与公开注册共用这把锁，堵住「注销未提交时抢先注册」。
	// hashtext 的跨版本稳定性不影响正确性：两侧在同一服务端内
	// 用同一函数计算第二键。
	UserEmailLock int32 = 71010004
)
