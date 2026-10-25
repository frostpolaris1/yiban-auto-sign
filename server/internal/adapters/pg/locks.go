package pg

// Advisory lock keys. A fixed number is stable across restarts and across
// PostgreSQL versions; hashtext() is not.
//
// Lock order is fixed: capacity first, audit second. The reverse order can
// deadlock two transactions that each hold one lock and wait for the other.
const (
	// AccountCapacityLock guards the live account count.
	AccountCapacityLock int64 = 71010001
	// AuditChainLock serializes chain-head reads and appends.
	AuditChainLock int64 = 71010002
	// UserCapacityLock guards the live user count.
	UserCapacityLock int64 = 71010003
)
