//go:build unix

// doctor 的 Unix 平台件：磁盘水位、文件属主、执行位。
// Windows 侧的对应件在 doctor_platform_windows.go。
package main

import (
	"fmt"
	"os"
	"os/user"
	"syscall"
)

// statfsFree 返回目录所在卷的剩余字节。
func statfsFree(dir string) (uint64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, fmt.Errorf("statfs %s: %w", dir, err)
	}
	return st.Bavail * uint64(st.Bsize), nil
}

// dirStatOf 返回目录的属主名、组名与权限位。ok=false 表示拿不到。
func dirStatOf(dir string) (dirInfo, bool) {
	st, err := os.Stat(dir)
	if err != nil {
		return dirInfo{}, false
	}
	sys, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return dirInfo{}, false
	}
	owner := fmt.Sprint(sys.Uid)
	if u, err := user.LookupId(owner); err == nil {
		owner = u.Username
	}
	group := fmt.Sprint(sys.Gid)
	if g, err := user.LookupGroupId(group); err == nil {
		group = g.Name
	}
	return dirInfo{owner: owner, group: group, perm: st.Mode().Perm()}, true
}

// userGroupNames 返回用户所属的组名。ok=false 表示拿不到。
func userGroupNames(name string) ([]string, bool) {
	u, err := user.Lookup(name)
	if err != nil {
		return nil, false
	}
	ids, err := u.GroupIds()
	if err != nil {
		return nil, false
	}
	names := make([]string, 0, len(ids))
	for _, id := range ids {
		if g, err := user.LookupGroupId(id); err == nil {
			names = append(names, g.Name)
		}
	}
	return names, true
}

// isExecutable 判 POSIX 执行位（属主/组/其他任一即可）。
func isExecutable(path string) (bool, error) {
	st, err := os.Stat(path)
	if err != nil {
		return false, err
	}
	return st.Mode().Perm()&0o111 != 0, nil
}
