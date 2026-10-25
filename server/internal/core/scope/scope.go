// Package scope 定义主体可读写的院校范围。
//
// # 功能
// 定义三种授权形状：master、admin、user，并提供查询用的范围值。
// 本包**不**查库、**不**解析令牌、**不**决定谁能登录。
// 范围到 SQL 的翻译见 adapters/pg 的 scopePredicate。
//
// # 归属
// core 层纯领域包。院校域表的每个仓储方法都收 Scope，
// 忘记加过滤会变成编译错误，而不是静默越权。
//
// # 复用
// Kind 及 Invalid、KindMaster、KindAdmin、KindUser；
// Scope 及 Master、Admin、User、Kind、Valid、UserID、SchoolIDs。
//
// # 口径
// 零值无效，运行时拒绝。
// **空集合不等于全部院校**：admin 无绑定即看不到任何行。
package scope

// Kind 是主体的授权形状。
type Kind uint8

const (
	// Invalid 是零值形状。仓储一律拒绝。
	Invalid Kind = iota
	// KindMaster 可见全部院校。
	KindMaster
	// KindAdmin 可见 admin_schools 中绑定的院校。
	KindAdmin
	// KindUser 只见自己拥有的行。
	KindUser
)

// Scope 是授权范围。请用 Master、Admin 或 User 构造。
type Scope struct {
	kind    Kind
	userID  string
	schools []string
}

// Master 返回不受限范围。**只有 master 主体可用**。
func Master() Scope { return Scope{kind: KindMaster} }

// Admin 返回限于 schoolIDs 的范围。
// 空切片**看不到任何行**：空集合永不当作「全部院校」。
// 即使未给院校，副本也非 nil，查询发出空数组而不是 NULL。
func Admin(schoolIDs []string) Scope {
	copied := make([]string, len(schoolIDs))
	copy(copied, schoolIDs)
	return Scope{kind: KindAdmin, schools: copied}
}

// User 返回限于 userID 拥有的行。
func User(userID string) Scope { return Scope{kind: KindUser, userID: userID} }

// Kind 返回授权形状。
func (s Scope) Kind() Kind { return s.kind }

// Valid 报告范围能否用于查询。
// user 范围必须带非空 userID，否则拒绝。
func (s Scope) Valid() bool {
	switch s.kind {
	case KindMaster, KindAdmin:
		return true
	case KindUser:
		return s.userID != ""
	default:
		return false
	}
}

// UserID 返回所属用户。master 与 admin 范围为空。
func (s Scope) UserID() string { return s.userID }

// SchoolIDs 返回绑定院校的副本。master 与 user 范围为 nil。
func (s Scope) SchoolIDs() []string { return append([]string{}, s.schools...) }
