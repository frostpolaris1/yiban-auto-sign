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

	// StartupLockKey 是单实例启动锁的固定键。
	// 会话级，不经事务：持有到持锁连接关闭或显式解锁，
	// 不在上面的「容量在前，审计在后」事务锁序内。
	// 与事务锁共用单键命名空间，取值不重复，故不互撞。
	StartupLockKey int64 = 71010005

	// TestSerialLockKey 是跨包真库测试的串行键。
	// 启动锁按数据库生效，schema 隔离不了：pg、assembly、cmd/server
	// 三个包中会真持启动锁的用例，在持锁期间同时持本键互斥，
	// 否则 go test 并行跑包时会互抢同一把启动锁。
	// **仅供跨包测试串行化，生产代码不得使用**。
	TestSerialLockKey int64 = 71019999
)
