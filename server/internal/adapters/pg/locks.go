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
)
