package legacy

// 旧 accounts.owner → 旧 users 行的解析表。
//
// 旧 owner 是自由文本，生产形状是完整邮箱或邮箱前缀（@ 之前）。
// 解析只做两段，顺序固定：先 lower(trim(email)) 精确命中，
// 再按邮箱 @ 前缀命中。同一段命中多行即歧义，调用方必须报行错误，
// 不按行序、不按新库 id、不猜。
//
// 邮箱归一只有 core/user.NormalizeEmail 一份实现。

import (
	"strings"

	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// 解析方式词。同时是 summary 里 owner_resolution 的键。
const (
	ownerViaExact  = "exact_email"
	ownerViaPrefix = "email_prefix"
)

// ownerIndex 是旧 users 导出行上的两级查找表。
// 两级都按旧行顺序追加，重复键保留全部候选：歧义要可见，不能先到先得。
type ownerIndex struct {
	byEmail map[string][]int64
	byLocal map[string][]int64
}

// newOwnerIndex 从旧 users 导出行建表。缺 id 或空邮箱的行不参与解析。
func newOwnerIndex(users []map[string]any) *ownerIndex {
	idx := &ownerIndex{
		byEmail: map[string][]int64{},
		byLocal: map[string][]int64{},
	}
	for _, row := range users {
		id, ok := valueInt(row["id"])
		if !ok {
			continue
		}
		email := coreuser.NormalizeEmail(valueString(row["email"]))
		if email == "" {
			continue
		}
		idx.byEmail[email] = append(idx.byEmail[email], id)
		if at := strings.IndexByte(email, '@'); at > 0 {
			idx.byLocal[email[:at]] = append(idx.byLocal[email[:at]], id)
		}
	}
	return idx
}

// ownerMatch 是一次解析的结果。
//
// found 为真时 oldID 与 via 有效；candidates > 1 表示该级命中多行，
// 调用方按歧义处理。found 为假且 candidates 为 0 表示无命中。
type ownerMatch struct {
	oldID      int64
	via        string
	candidates int
	found      bool
}

// resolve 解析一个旧 owner 值。
func (idx *ownerIndex) resolve(owner string) ownerMatch {
	key := coreuser.NormalizeEmail(owner)
	if key == "" {
		return ownerMatch{}
	}
	if ids := idx.byEmail[key]; len(ids) > 0 {
		if len(ids) == 1 {
			return ownerMatch{oldID: ids[0], via: ownerViaExact, candidates: 1, found: true}
		}
		return ownerMatch{candidates: len(ids)}
	}
	// 邮箱形状的 owner 未精确命中时不退到前缀：@ 之后是域，
	// 拿整串去比前缀只会误命中。
	if strings.IndexByte(key, '@') >= 0 {
		return ownerMatch{}
	}
	if ids := idx.byLocal[key]; len(ids) > 0 {
		if len(ids) == 1 {
			return ownerMatch{oldID: ids[0], via: ownerViaPrefix, candidates: 1, found: true}
		}
		return ownerMatch{candidates: len(ids)}
	}
	return ownerMatch{}
}

// oldIDForEmail 按精确邮箱返回唯一旧 user id。
// 无命中或多行同邮箱（歧义）都返回 false：deleted_by 的归属不可猜。
func (idx *ownerIndex) oldIDForEmail(email string) (int64, bool) {
	ids := idx.byEmail[coreuser.NormalizeEmail(email)]
	if len(ids) != 1 {
		return 0, false
	}
	return ids[0], true
}
