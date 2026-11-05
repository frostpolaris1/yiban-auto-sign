// Package fsperm 按 POSIX 权限位判断目录访问。
//
// # 功能
// 纯函数：给定目录属主/组/模式与目标用户身份，判断读、写、执行。
// 本包**不**读文件、**不**查用户、**不**打印路径、**不**落盘。
//
// # 归属
// core 层纯领域包。doctor 与 selfcheck 共用同一份判据，
// 避免「调用者能写就算服务能写」的第二套口径。
//
// # 口径
// 类选择按 POSIX：属主优先，其次属组，否则其他。
// 目录的读/写还需要执行位（进入目录），调用方传 Read|Execute 或 Write|Execute。
// HasIDs=true 时按 uid/gid 判，否则按名字判；身份与属主都无信息时按其他位。
package fsperm

import "os"

// Access 是访问请求位。
type Access uint8

// 访问位，可组合。
const (
	Read Access = 1 << iota
	Write
	Execute
)

// Owner 是目录的属主、组与权限位。
type Owner struct {
	UID, GID             uint32
	OwnerName, GroupName string
	Mode                 os.FileMode
	// HasIDs 表示 UID/GID 可用。false 时只用名字判。
	HasIDs bool
}

// Identity 是目标用户的身份。
type Identity struct {
	UID, GID   uint32
	Name       string
	GroupIDs   []uint32
	GroupNames []string
	// HasIDs 表示 UID/GID 与 GroupIDs 可用。false 时只用名字判。
	HasIDs bool
}

// Allowed 判断身份 id 对 owner 的目录是否具备 want 的全部访问位。
func Allowed(owner Owner, id Identity, want Access) bool {
	bits := classBits(owner, id)
	return accessBitsOK(bits, want)
}

// RootAllows 判 root（uid 0）对目录的访问。
// root 有 CAP_DAC_OVERRIDE，模式位并不真正拦住它；这里只用位识别
// 「明显不可写/不可进入」的目录（三类都没有该位），供以 root 运行的
// 更新器判自己的备份目录。非 root 身份请用 Allowed。
func RootAllows(owner Owner, want Access) bool {
	mode := owner.Mode.Perm()
	if want&Read != 0 && mode&0o444 == 0 {
		return false
	}
	if want&Write != 0 && mode&0o222 == 0 {
		return false
	}
	if want&Execute != 0 && mode&0o111 == 0 {
		return false
	}
	return true
}

// accessBitsOK 检查请求位是否都在该类权限里。
func accessBitsOK(bits os.FileMode, want Access) bool {
	if want&Read != 0 && bits&0o4 == 0 {
		return false
	}
	if want&Write != 0 && bits&0o2 == 0 {
		return false
	}
	if want&Execute != 0 && bits&0o1 == 0 {
		return false
	}
	return true
}

// classBits 返回 POSIX 选中的三类权限位（0o7）。
func classBits(owner Owner, id Identity) os.FileMode {
	switch {
	case sameUser(owner, id):
		return (owner.Mode >> 6) & 0o7
	case inGroup(owner, id):
		return (owner.Mode >> 3) & 0o7
	default:
		return owner.Mode & 0o7
	}
}

// sameUser 判属主类。双方都有 ID 时按 uid；否则按名字。
func sameUser(owner Owner, id Identity) bool {
	if owner.HasIDs && id.HasIDs {
		return owner.UID == id.UID
	}
	return owner.OwnerName != "" && owner.OwnerName == id.Name
}

// inGroup 判属组类。双方都有 ID 时按 gid 集；否则按组名集。
func inGroup(owner Owner, id Identity) bool {
	if owner.HasIDs && id.HasIDs {
		if owner.GID == id.GID {
			return true
		}
		for _, gid := range id.GroupIDs {
			if gid == owner.GID {
				return true
			}
		}
		return false
	}
	if owner.GroupName == "" {
		return false
	}
	for _, group := range id.GroupNames {
		if group == owner.GroupName {
			return true
		}
	}
	return false
}
