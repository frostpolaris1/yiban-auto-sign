package main

// 自检文件系统端口的 OS 实现。
//
// # 归属
// cmd/cli 装配层。app/selfcheck 只保留 FileSystem 接口；
// OS 实现放在唯一消费者（selfcheck 子命令）旁边，
// canonical 的 fs.os.impl 规则禁止 app 层出现 CreateTemp。
//
// # 口径
// WriteFileAtomic 用同目录临时文件 + chmod + rename：
// 文件从创建那一刻就是 0600；父目录不存在时按 0700 创建，
// 已存在的父目录不改权限（/var/lib/yiban 还承载照片与页面目录）。

import (
	"os"
	"path/filepath"
)

// osFileSystem 是 selfcheck.FileSystem 的进程实现。
type osFileSystem struct{}

// Stat 实现 selfcheck.FileSystem。
func (osFileSystem) Stat(name string) (os.FileInfo, error) { return os.Stat(name) }

// ReadFile 实现 selfcheck.FileSystem。
func (osFileSystem) ReadFile(name string) ([]byte, error) { return os.ReadFile(name) }

// ReadDir 实现 selfcheck.FileSystem。
func (osFileSystem) ReadDir(name string) ([]os.DirEntry, error) { return os.ReadDir(name) }

// WriteFileAtomic 在同一目录写临时文件，chmod 后 rename 覆盖目标。
// 父目录不存在时按 0700 创建；已存在的父目录不动权限。
func (osFileSystem) WriteFileAtomic(name string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(name)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".update-state-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	cleanup := func() {
		_ = f.Close()
		_ = os.Remove(tmp)
	}
	if _, err := f.Write(data); err != nil {
		cleanup()
		return err
	}
	if err := f.Chmod(perm); err != nil {
		cleanup()
		return err
	}
	if err := f.Sync(); err != nil {
		cleanup()
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, name); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}
