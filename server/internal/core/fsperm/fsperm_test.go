package fsperm

// POSIX 位判据：类优先级、执行位、ID 与名字两种身份。
//
// # 口径
// 目录读写都要执行位；属主类不因其他位可写而变可写。

import (
	"os"
	"testing"
)

func TestOwnerClassBits(t *testing.T) {
	owner := Owner{OwnerName: "yiban", GroupName: "yiban", Mode: 0o700}
	id := Identity{Name: "yiban", GroupNames: []string{"yiban"}}
	for _, want := range []Access{Read, Write, Execute, Read | Write | Execute} {
		if !Allowed(owner, id, want) {
			t.Fatalf("owner 0700 should allow %v", want)
		}
	}
	owner.Mode = 0o500
	if Allowed(owner, id, Write) {
		t.Fatal("owner 0500 must not allow write")
	}
	if !Allowed(owner, id, Read|Execute) {
		t.Fatal("owner 0500 should allow read+execute")
	}
}

func TestGroupAndOtherClass(t *testing.T) {
	owner := Owner{OwnerName: "root", GroupName: "yiban", Mode: 0o070}
	groupMember := Identity{Name: "yiban", GroupNames: []string{"yiban"}}
	if !Allowed(owner, groupMember, Read|Write|Execute) {
		t.Fatal("group 0070 should allow rwx")
	}
	other := Identity{Name: "nobody", GroupNames: []string{"nobody"}}
	if Allowed(owner, other, Read) {
		t.Fatal("other 0000 must not allow read")
	}
	owner.Mode = 0o007
	if !Allowed(owner, other, Read|Write|Execute) {
		t.Fatal("other 0007 should allow rwx")
	}
}

func TestClassPrecedence(t *testing.T) {
	// 属主类优先：属主无写位，其他位有写位也不放行。
	owner := Owner{OwnerName: "yiban", GroupName: "yiban", Mode: 0o400 | 0o002}
	id := Identity{Name: "yiban", GroupNames: []string{"yiban"}}
	if Allowed(owner, id, Write) {
		t.Fatal("owner class must take precedence over other bits")
	}
}

func TestExecuteRequiredForDirectoryAccess(t *testing.T) {
	owner := Owner{OwnerName: "yiban", GroupName: "yiban", Mode: 0o600}
	id := Identity{Name: "yiban", GroupNames: []string{"yiban"}}
	if Allowed(owner, id, Read|Execute) {
		t.Fatal("0600 lacks execute; directory read must fail")
	}
	if !Allowed(owner, id, Read) {
		t.Fatal("0600 should allow the read bit alone")
	}
}

func TestNumericIDsTakePrecedence(t *testing.T) {
	owner := Owner{UID: 1000, GID: 1000, OwnerName: "old", GroupName: "oldg", Mode: 0o700, HasIDs: true}
	id := Identity{UID: 1000, GID: 1000, Name: "yiban", HasIDs: true}
	if !Allowed(owner, id, Read|Write|Execute) {
		t.Fatal("matching uid must select owner class")
	}
}

func TestGroupIDsMatch(t *testing.T) {
	owner := Owner{UID: 1000, GID: 2000, Mode: 0o070, HasIDs: true}
	id := Identity{UID: 1001, GID: 3000, GroupIDs: []uint32{2000}, HasIDs: true}
	if !Allowed(owner, id, Read|Write) {
		t.Fatal("supplementary gid must select group class")
	}
}

func TestNumericOwnerNameWithoutIDsIsNotUserMatch(t *testing.T) {
	// 名字回退成 "1000" 且没有 ID 时，不应把服务用户 yiban 当属主。
	owner := Owner{OwnerName: "1000", GroupName: "1000", Mode: 0o700}
	id := Identity{Name: "yiban", GroupNames: []string{"yiban"}}
	if Allowed(owner, id, Read) {
		t.Fatal("numeric owner name must not match a named user without IDs")
	}
}

func TestZeroMode(t *testing.T) {
	owner := Owner{OwnerName: "yiban", GroupName: "yiban", Mode: os.FileMode(0)}
	id := Identity{Name: "yiban", GroupNames: []string{"yiban"}}
	if Allowed(owner, id, Read) {
		t.Fatal("mode 000 must deny read")
	}
}
