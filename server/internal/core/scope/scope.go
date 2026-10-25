// Package scope defines which school-domain rows a principal may read or
// write.
//
// The zero value is invalid and is rejected at runtime. Every repository
// method for a school-domain table takes a Scope, so a forgotten filter is a
// compile error, not a silent data leak.
package scope

// Kind is the authorization shape of a principal.
type Kind uint8

const (
	// Invalid is the zero kind. Repositories reject it.
	Invalid Kind = iota
	// KindMaster sees every school.
	KindMaster
	// KindAdmin sees the schools bound in admin_schools.
	KindAdmin
	// KindUser sees only rows it owns.
	KindUser
)

// Scope is an authorization scope. Build one with Master, Admin or User.
type Scope struct {
	kind    Kind
	userID  string
	schools []string
}

// Master returns the unrestricted scope. Only a master principal may use it.
func Master() Scope { return Scope{kind: KindMaster} }

// Admin returns a scope limited to schoolIDs. An empty slice sees nothing:
// an empty set is never treated as "all schools". The copy is non-nil even
// when no school is given, so the query sends an empty array, not NULL.
func Admin(schoolIDs []string) Scope {
	copied := make([]string, len(schoolIDs))
	copy(copied, schoolIDs)
	return Scope{kind: KindAdmin, schools: copied}
}

// User returns a scope limited to rows owned by userID.
func User(userID string) Scope { return Scope{kind: KindUser, userID: userID} }

// Kind reports the authorization shape.
func (s Scope) Kind() Kind { return s.kind }

// Valid reports whether the scope can be used in a query.
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

// UserID returns the owning user. It is empty for master and admin scopes.
func (s Scope) UserID() string { return s.userID }

// SchoolIDs returns a copy of the bound schools. It is empty for master and
// user scopes.
func (s Scope) SchoolIDs() []string { return append([]string{}, s.schools...) }
